"""Bounded private-ROM Z180 reset diagnostics, not a runnable phone profile."""

import argparse
import hashlib
import json
from pathlib import Path
import resource
import selectors
import socket
import subprocess
import sys
import tempfile
import time

from millennium_rom_inventory import EXECUTABLE_FIRMWARE, IMAGES, InventoryError, read_image
import millennium_voice


PROFILES = ('rom-only', 'lab-ram-40000', 'lab-board-startup')
PANEL_PROFILES = ('unconnected', 'cu20026-t23a-lab')
TELEPHONY_PROFILES = ('unconnected', 'standin')
COIN_PROFILES = ('unconnected', 'standin')
MODEM_PROFILES = ('unconnected', 'control')
SPEECH_PROFILES = ('unconnected', 'silent', 'chip')
VOICE_COUNTERS = ('voice_starts', 'voice_stops', 'voice_cuts', 'voice_ignored', 'voice_beyond',
                  'voice_errors', 'int0_interrupts')
VOICE_OPS = {0: 'stop', 1: 'start', 2: 'cut'}
MODEM_COUNTERS = ('modem_reads', 'modem_writes', 'modem_resets', 'modem_transmitted_bytes')
# Scheduled inputs, by kind: h = handset (1 lifted, 0 hung up), x = a raw
# opcode from the telephony board, c = a coin on channel 1..16, s = the
# port 80 contacts become 0..15 (bit 1 is 80.5: low unlocks the housing,
# inferred). Values are bounds, inclusive. k = a key, one of KEYS.
# w = swipe card N (an index into `cards`), b = any byte from the coin
# mechanism (a fault letter, say).
EVENT_KINDS = {'h': (0, 1), 'x': (0, 255), 'c': (1, 16), 's': (0, 15), 'm': (0, 255),
               'w': (0, 7), 'b': (0, 255)}
HEX_EVENTS = ('x', 'm', 'b')
KEYS = '1234567890*#'
# Every key on the set, by name, and the CSI/O opcode the telephony board
# sends for it: the keypad step's echo table (09:567D) names each one
# [code]. The dial pad's twelve keep their single characters; the rest
# reach the worker as raw opcodes.
KEY_OPCODES = dict({key: 0x22 + 2 * i for i, key in enumerate(KEYS)},
                   **{'A%d' % n: 0x3e + 2 * n for n in range(1, 11)},
                   **{'V-': 0x54, 'V+': 0x56, 'next': 0x58, 'lang': 0x5a})
# How a swipe reads: both strokes, only going in, only coming out (going
# in it reads damaged), or no stripe at all.
CARD_READS = {'both': 'b', 'in': 'i', 'out': 'o', 'none': 'n'}
MAX_CARDS = 8
COIN_COUNTERS = ('coin_status_replies', 'coin_query_replies', 'coin_inserted',
                 'coin_bytes_sent', 'coin_commands')
STREAM_COUNTERS = ('stream', 'stream_rx_bytes', 'stream_overruns', 'stream_messages_in',
                   'stream_lag_ms')
CARD_COUNTERS = ('card_count', 'card_swipes', 'card_strobes', 'card_latched', 'card_lost',
                 'card_overruns', 'card_busy', 'card_chip_reads', 'dma0_interrupts')
MAX_PEEKS = 8
MAX_EVENTS = 64
TELEPHONY_COUNTERS = ('telephony_resets', 'telephony_announces', 'telephony_commands',
                      'telephony_replies', 'telephony_frames', 'telephony_frame_errors',
                      'telephony_keepalives', 'telephony_status_queries',
                      'telephony_unarmed_strobes', 'telephony_single_commands')
MAX_INSTRUCTIONS = 100000
# 128M attempts is about 170 s of modelled time. It was 16M (21 s) until the
# coin stand-in: the coin-jam alarm it exists to prevent takes ten 2.5 s
# timeouts to appear, so a run has to outlast 28 s to show it absent. 64M
# then held until the modem: an install typed at the keypad reaches the
# manager's first packet at 66 s and gives up on it at 97 s, which is 74M
# attempts, and the headroom is for a slower host rather than a longer run.
MAX_STARTUP_INSTRUCTIONS = 128000000
MAX_OUTPUT = 32768
WORKER_TIMEOUT = 40
# A streamed run (a live far end on ASCI1) is paced to the wall clock, so
# its limits are minutes, not seconds, on purpose: an install reaches the
# manager about a minute in and then downloads some 20 kB at 1200 Bd, which
# is three minutes of line time before any retry. 1G attempts is about 21
# modelled minutes. Paced, the worker sleeps most of the time, so its CPU
# limit is well under its wall limit: a run that burns CPU flat out for
# that long is not being paced and should die.
MAX_STREAM_INSTRUCTIONS = 1000000000
STREAM_WALL_SECONDS = 1500
STREAM_CPU_SECONDS = 600
# Tags in the stream (z180/millennium_z180_worker.c), with each message's
# length after the tag.
# 'V' is sent only with speech_profile='chip': op, bank, sample, ms (see
# voice_event), so a host that never asks for the chip never sees it.
STREAM_LENGTHS = {b'D': 1, b'W': 2, b'C': 1, b'S': 44, b'Z': 4, b'H': 1, b'V': 7}


class DiagnosticError(ValueError):
    pass


def worker_limits(cpu_seconds=32):
    # Each run is a fresh process; private snapshots and temporary RAM disappear
    # on exit. CPU/output limits complement the parent wall-clock timeout.
    # 76M attempts took eleven CPU seconds on this host with every stand-in
    # on, so 128M is about nineteen; the CPU limit and the parent timeout
    # keep that and a margin in hand.
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def stream_worker_limits():
    # A hard limit only ever goes down, so the streamed one is set once,
    # not lowered to the batch limit and then raised.
    worker_limits(STREAM_CPU_SECONDS)


class StreamLink(object):
    """The parent's end of a streamed run: what the far end may do.

    `receive(data)` queues bytes for the firmware's ASCI1 receiver, which
    takes them one frame time apart; `detect(value)` sets the 73K212's
    detect register (DR), which is how the firmware learns of answer tone
    and carrier. A far end is any object with `on_message(link, tag,
    payload)` and, optionally, `selectables()` -> [(file object, callback)]
    and `poll(link)`, called at least every 50 ms.
    """

    def __init__(self, sock):
        self.sock = sock

    def receive(self, data):
        for byte in bytes(data):
            self.sock.sendall(b'R' + bytes([byte]))

    def detect(self, value):
        if type(value) is not int or not 0 <= value <= 255:
            raise DiagnosticError('invalid detect register value')
        self.sock.sendall(b'M' + bytes([value]))

    def event(self, kind, value):
        """A scheduled-event kind applied now, as a person reacting to the
        panel would: ('k', name) a key, ('h', 0/1), ('s', contacts),
        ('c', channel), ('w', card index), ('b', a coin-mechanism byte)."""
        kind, value = event_wire(kind, value)
        if kind == 'x':
            self.telephony(value)
            return
        self.sock.sendall(b'E' + kind.encode() + bytes([value]))

    def telephony(self, opcode):
        """A raw opcode from the telephony board, as an `x` event sends."""
        if type(opcode) is not int or not 0 <= opcode <= 255:
            raise DiagnosticError('invalid telephony opcode')
        self.sock.sendall(b'X' + bytes([opcode]))

    def finish(self):
        """Ask the worker to stop and report: it sees the stream close."""
        self.sock.shutdown(socket.SHUT_WR)


def event_wire(kind, value):
    """(kind, value) as the worker takes it: a named key becomes its raw
    opcode, a dial-pad key its character."""
    if kind == 'k':
        if value not in KEY_OPCODES:
            raise DiagnosticError('invalid key')
        return ('k', ord(value)) if len(value) == 1 else ('x', KEY_OPCODES[value])
    if kind not in EVENT_KINDS or type(value) is not int or not EVENT_KINDS[kind][0] <= value <= EVENT_KINDS[kind][1]:
        raise DiagnosticError('invalid event')
    return kind, value


def card_spec(card):
    """A card as the worker takes it: how it reads, then the track-2 body
    (digits and '=' between the sentinels, 1..37 of them)."""
    how, body = ('both', card) if isinstance(card, str) else tuple(card)
    if (how not in CARD_READS or not isinstance(body, str) or not 1 <= len(body) <= 37
            or set(body) - set('0123456789=')):
        raise DiagnosticError('invalid card')
    return CARD_READS[how] + body


def split_stream(buffer):
    """Whole messages from the front of `buffer` -> ([(tag, payload)], rest)."""
    messages = []
    while buffer:
        tag = buffer[:1]
        if tag not in STREAM_LENGTHS:
            raise DiagnosticError('invalid worker stream')
        end = 1 + STREAM_LENGTHS[tag]
        if len(buffer) < end:
            break
        messages.append((tag.decode(), buffer[1:end]))
        buffer = buffer[end:]
    return messages, buffer


def voice_event(payload):
    """A 'V' message as (what, bank, sample, ms): what is 'start' when the
    chip takes a sample number, 'stop' when the sample has played out (BUSY
    drops and INT0 asks for the next) and 'cut' when the firmware resets the
    chip mid-sample. A host plays millennium_voice.decode(voiceware, bank,
    sample) from a start, and stops it early on a cut."""
    if len(payload) != 7 or payload[0] not in VOICE_OPS:
        raise DiagnosticError('invalid voice event')
    return VOICE_OPS[payload[0]], payload[1], payload[2], int.from_bytes(payload[3:7], 'little')


def _stream(command, far_end, output, errors):
    """Run the worker with a live far end. Returns its exit status."""
    parent, child = socket.socketpair()
    try:
        command = command + ['stream=%d' % child.fileno()]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                   preexec_fn=stream_worker_limits, close_fds=True,
                                   pass_fds=(child.fileno(),))
    except BaseException:
        parent.close()
        raise
    finally:
        child.close()
    link = StreamLink(parent)
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + STREAM_WALL_SECONDS
    buffer = b''
    try:
        parent.setblocking(True)
        selector.register(parent, selectors.EVENT_READ, None)
        registered = set()
        open_ = True
        while open_:
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(command, STREAM_WALL_SECONDS)
            wanted = dict(far_end.selectables()) if hasattr(far_end, 'selectables') else {}
            for fileobj in registered - set(wanted):
                selector.unregister(fileobj)
            for fileobj in set(wanted) - registered:
                selector.register(fileobj, selectors.EVENT_READ, None)
            registered = set(wanted)
            for key, _ in selector.select(timeout=0.05):
                if key.fileobj is parent:
                    data = parent.recv(65536)
                    if not data:
                        open_ = False
                        break
                    messages, buffer = split_stream(buffer + data)
                    for tag, payload in messages:
                        far_end.on_message(link, tag, payload)
                else:
                    wanted[key.fileobj](link)
            if hasattr(far_end, 'poll'):
                far_end.poll(link)
        return process.wait(timeout=max(1, deadline - time.monotonic()))
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        parent.close()


def run_diagnostic(firmware, worker, *, profile='rom-only', instructions=10000,
                   status80_contacts=None, int2_period_clocks=None, status_e0_fixture=None,
                   panel_profile='unconnected', asci0_pin_fixture=None,
                   telephony_profile='unconnected', events=(), coin_profile='unconnected', peek=(),
                   modem_profile='unconnected', far_end=None, cards=(), coin_hello='A',
                   speech_profile='unconnected', voiceware=None):
    """One bounded worker run. With `far_end`, ASCI1 is live while it runs
    (see StreamLink); without, the run is the batch diagnostic."""
    maximum = MAX_STARTUP_INSTRUCTIONS if profile == 'lab-board-startup' else MAX_INSTRUCTIONS
    if far_end is not None:
        if modem_profile == 'unconnected' or not hasattr(far_end, 'on_message'):
            raise DiagnosticError('a far end needs the modem stand-in')
        maximum = MAX_STREAM_INSTRUCTIONS
    if profile not in PROFILES or type(instructions) is not int or not 1 <= instructions <= maximum:
        raise DiagnosticError('invalid diagnostic budget or profile')
    if status80_contacts is not None and (profile != 'lab-board-startup'
            or type(status80_contacts) is not int or not 0 <= status80_contacts <= 15):
        raise DiagnosticError('invalid startup input fixture')
    if int2_period_clocks is not None and (profile != 'lab-board-startup'
            or type(int2_period_clocks) is not int or not 20 <= int2_period_clocks <= 6144000):
        raise DiagnosticError('invalid startup clock fixture')
    if status_e0_fixture is not None and (profile != 'lab-board-startup'
            or type(status_e0_fixture) is not int or not 0 <= status_e0_fixture <= 255):
        raise DiagnosticError('invalid startup status fixture')
    if (panel_profile not in PANEL_PROFILES
            or (panel_profile != 'unconnected' and profile != 'lab-board-startup')):
        raise DiagnosticError('invalid startup panel profile')
    if asci0_pin_fixture is not None and (profile != 'lab-board-startup'
            or type(asci0_pin_fixture) is not int or not 0 <= asci0_pin_fixture <= 3):
        raise DiagnosticError('invalid ASCI0 pin fixture')
    if (telephony_profile not in TELEPHONY_PROFILES
            or (telephony_profile != 'unconnected' and (profile != 'lab-board-startup'
                or status_e0_fixture is None or int2_period_clocks is None))):
        # The stand-in owns E0 bit 6 only, and counts time in INT2 ticks.
        raise DiagnosticError('invalid telephony stand-in')
    if (coin_profile not in COIN_PROFILES
            or (coin_profile != 'unconnected' and (telephony_profile == 'unconnected'
                or asci0_pin_fixture is None or asci0_pin_fixture & 2))):
        # The coin stand-in is ASCI0's far end: DCD must be low to receive.
        raise DiagnosticError('invalid coin stand-in')
    if (modem_profile not in MODEM_PROFILES
            or (modem_profile != 'unconnected' and telephony_profile == 'unconnected')):
        # The modem's read data is E0 bit 4, so it needs the E0 fixture the
        # telephony stand-in already requires.
        raise DiagnosticError('invalid modem stand-in')
    cards = tuple(card_spec(card) for card in cards)
    if cards and (len(cards) > MAX_CARDS or telephony_profile == 'unconnected' or status80_contacts is None):
        # The reader's bits share port 80 with the contacts fixture.
        raise DiagnosticError('invalid card reader')
    if coin_hello is not None and (not isinstance(coin_hello, str) or len(coin_hello) != 1
                                   or not 0 <= ord(coin_hello) <= 255):
        raise DiagnosticError('invalid coin hello')
    if coin_hello != 'A' and coin_profile == 'unconnected':
        raise DiagnosticError('invalid coin hello')
    if speech_profile not in SPEECH_PROFILES or (speech_profile != 'unconnected'
                                                  and telephony_profile == 'unconnected'):
        raise DiagnosticError('invalid speech sink')
    if (voiceware is None) != (speech_profile != 'chip'):
        # The chip's BUSY is as long as the sample it plays: it needs the ROM.
        raise DiagnosticError('the voice chip needs its voiceware, and only it takes one')
    voice_data = None
    if voiceware is not None:
        try:
            voice_data = read_image(voiceware)
            if len(voice_data) > 8 * millennium_voice.BANK_SIZE:
                raise DiagnosticError('invalid voiceware')
            millennium_voice.banks(voice_data)
        except (InventoryError, millennium_voice.VoiceError):
            raise DiagnosticError('invalid voiceware') from None
    peek = tuple(peek)
    if (len(peek) > MAX_PEEKS or any(type(address) is not int or not 0 <= address <= 0xffff for address in peek)
            or (peek and telephony_profile == 'unconnected')):
        raise DiagnosticError('invalid RAM peek')
    events = tuple(events)
    if events and telephony_profile == 'unconnected':
        raise DiagnosticError('events need the telephony stand-in')
    if len(events) > MAX_EVENTS:
        raise DiagnosticError('too many events')
    last = 0
    for event in events:
        if (not isinstance(event, tuple) or len(event) != 3 or type(event[0]) is not int
                or not last <= event[0] <= 3600000):
            raise DiagnosticError('invalid event')
        if event[1] == 'k':
            if not isinstance(event[2], str) or event[2] not in KEY_OPCODES:
                raise DiagnosticError('invalid event')
        elif (event[1] not in EVENT_KINDS or type(event[2]) is not int
                or not EVENT_KINDS[event[1]][0] <= event[2] <= EVENT_KINDS[event[1]][1]):
            raise DiagnosticError('invalid event')
        if event[1] == 'w' and event[2] >= len(cards):
            raise DiagnosticError('a swipe needs its card')
        if event[1] == 's' and status80_contacts is None:
            raise DiagnosticError('a contact event needs the contact fixture')
        last = event[0]
        if event[1] in 'cb' and coin_profile == 'unconnected':
            raise DiagnosticError('coin events need the coin stand-in')
        if event[1] == 'm' and modem_profile == 'unconnected':
            raise DiagnosticError('modem events need the modem stand-in')
    data = read_image(firmware)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != 262144 or digest not in EXECUTABLE_FIRMWARE:
        raise DiagnosticError('diagnostics require the recognized firmware')
    executable = str(Path(worker).resolve())
    with tempfile.TemporaryDirectory(prefix='millennium-reset-') as private:
        snapshot = Path(private) / 'firmware.bin'
        snapshot.write_bytes(data)
        snapshot.chmod(0o400)
        if voice_data is not None:
            voice_snapshot = Path(private) / 'voiceware.bin'
            voice_snapshot.write_bytes(voice_data)
            voice_snapshot.chmod(0o400)
        with (Path(private) / 'stdout').open('w+b') as output, (Path(private) / 'stderr').open('w+b') as errors:
            try:
                command = [executable, str(snapshot), str(instructions), profile]
                if telephony_profile != 'unconnected':
                    command.extend([str(-1 if status80_contacts is None else status80_contacts),
                                    str(int2_period_clocks), str(status_e0_fixture), panel_profile,
                                    str(-1 if asci0_pin_fixture is None else asci0_pin_fixture),
                                    'telephony=' + telephony_profile])
                    if coin_profile != 'unconnected':
                        command.append('coin=' + coin_profile)
                    if modem_profile != 'unconnected':
                        command.append('modem=' + modem_profile)
                    if cards:
                        command.append('cards=' + ','.join(cards))
                    if speech_profile != 'unconnected':
                        command.append('speech=' + speech_profile)
                    if voice_data is not None:
                        command.append('voice=' + str(voice_snapshot))
                    if coin_hello != 'A':
                        command.append('coinhello=' + ('-' if coin_hello is None else '%02x' % ord(coin_hello)))
                    if peek:
                        command.append('peek=' + ','.join('%x' % address for address in peek))
                    if events:
                        command.append('events=' + ','.join(
                            '%d:%s%s' % ((ms,) + _event_argument(kind, value)) for ms, kind, value in events))
                elif panel_profile != 'unconnected' or asci0_pin_fixture is not None:
                    command.extend([str(-1 if status80_contacts is None else status80_contacts),
                                    str(int2_period_clocks or 0),
                                    str(-1 if status_e0_fixture is None else status_e0_fixture), panel_profile])
                    if asci0_pin_fixture is not None:
                        command.append(str(asci0_pin_fixture))
                elif status_e0_fixture is not None:
                    command.extend([str(-1 if status80_contacts is None else status80_contacts),
                                    str(int2_period_clocks or 0), str(status_e0_fixture)])
                elif int2_period_clocks is not None:
                    command.extend([str(-1 if status80_contacts is None else status80_contacts),
                                    str(int2_period_clocks)])
                elif status80_contacts is not None:
                    command.append(str(status80_contacts))
                if far_end is None:
                    returncode = subprocess.run(command,
                                                stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                                timeout=WORKER_TIMEOUT, preexec_fn=worker_limits,
                                                close_fds=True).returncode
                else:
                    returncode = _stream(command, far_end, output, errors)
            except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
                raise DiagnosticError('Z180 worker failed or exceeded its budget') from None
            output.seek(0)
            raw = output.read(MAX_OUTPUT + 1)
            errors.seek(0)
            if returncode or errors.read(1) or len(raw) > MAX_OUTPUT:
                raise DiagnosticError('Z180 worker failed or produced unexpected output')
        try:
            report = json.loads(raw)
            required = {'schema_version', 'stop_reason', 'attempted_instructions', 'fault_address',
                        'last_instruction_pc', 'pc', 'sp', 'cbr', 'bbr', 'cbar', 'iocr',
                        'rcr', 'dcntl', 'omcr', 'tcr', 'cntr', 'itc', 'il', 'dstat', 'trace_pc',
                        'trace_tail_first_attempt', 'trace_tail_pc', 'board_reads', 'board_writes', 'eeprom_reads',
                        'status80_contacts', 'eeprom_writes', 'modeled_clocks',
                        'timer0_interrupts', 'timer1_interrupts', 'iff1',
                        'int2_period_clocks', 'clock_input_events', 'external2_interrupts', 'status_e0_fixture',
                        'panel_profile', 'panel_writes', 'panel_data_writes', 'panel_command_writes',
                        'panel_ignored_writes', 'panel_cursor', 'panel_cursor_visible', 'panel_pending_bytes',
                        'panel_cooldown_clocks', 'panel_cells', 'panel_custom_codes',
                        'asci0_pin_fixture', 'asci0_transmitted_bytes', 'asci0_last_transmitted_byte',
                        'asci0_interrupts', 'telephony_profile', 'telephony_command_log',
                        'events_applied', 'panel_history_dropped', 'panel_history', *TELEPHONY_COUNTERS,
                        'coin_profile', 'coin_command_log', 'ram_peek', *COIN_COUNTERS,
                        'modem_profile', 'modem_registers', 'modem_write_log', 'modem_transmit_log',
                        *MODEM_COUNTERS, *STREAM_COUNTERS, 'card_profile', *CARD_COUNTERS,
                        'coin_hello_replies', 'coin_raw_bytes', 'panel_brightness', 'panel_status_reads',
                        'speech_profile', 'speech_writes', *VOICE_COUNTERS, 'voice_busy', 'voice_log'}
            if set(report) != required or type(report['schema_version']) is not int or report['schema_version'] != 13:
                raise ValueError
            for key in ('trace_pc', 'trace_tail_pc'):
                if (not isinstance(report[key], list) or len(report[key]) > 64
                        or any(type(pc) is not int or not 0 <= pc <= 65535 for pc in report[key])):
                    raise ValueError
            bounds = {'attempted_instructions': instructions, 'fault_address': 1048575,
                      'last_instruction_pc': 65535, 'pc': 65535, 'sp': 65535,
                      'cbr': 255, 'bbr': 255, 'cbar': 255, 'iocr': 255,
                      'rcr': 255, 'dcntl': 255, 'omcr': 255, 'tcr': 255, 'cntr': 255,
                      'itc': 255, 'il': 255, 'dstat': 255,
                      'board_reads': instructions, 'board_writes': instructions,
                      'eeprom_reads': instructions, 'eeprom_writes': instructions,
                      'modeled_clocks': instructions * 128,
                      'timer0_interrupts': instructions, 'timer1_interrupts': instructions, 'iff1': 1,
                      'int2_period_clocks': 6144000, 'clock_input_events': instructions * 128 // 20,
                      'external2_interrupts': instructions,
                      'panel_writes': instructions, 'panel_data_writes': instructions,
                      'panel_command_writes': instructions, 'panel_ignored_writes': instructions,
                      'panel_cursor': 39, 'panel_cursor_visible': 1, 'panel_pending_bytes': 6,
                      'panel_cooldown_clocks': 921600,
                      'asci0_transmitted_bytes': instructions, 'asci0_interrupts': instructions}
            if any(type(report[key]) is not int or not 0 <= report[key] <= high for key, high in bounds.items()):
                raise ValueError
            if (report['int2_period_clocks'] != (int2_period_clocks or 0)
                    or (int2_period_clocks is None and (report['clock_input_events'] or report['external2_interrupts']))):
                raise ValueError
            if int2_period_clocks and report['clock_input_events'] != report['modeled_clocks'] // int2_period_clocks:
                raise ValueError
            if (report['board_reads'] + report['board_writes'] > instructions
                    or report['eeprom_reads'] > report['board_writes']
                    or report['eeprom_writes'] > report['board_writes']
                    or (profile != 'lab-board-startup' and (report['board_reads'] or report['board_writes']
                                                         or report['eeprom_reads'] or report['eeprom_writes']))):
                raise ValueError
            if (type(report['status80_contacts']) is not int
                    or report['status80_contacts'] != (-1 if status80_contacts is None else status80_contacts)):
                raise ValueError
            if (type(report['status_e0_fixture']) is not int
                    or report['status_e0_fixture'] != (-1 if status_e0_fixture is None else status_e0_fixture)):
                raise ValueError
            if report['panel_profile'] != panel_profile:
                raise ValueError
            if (type(report['asci0_pin_fixture']) is not int
                    or report['asci0_pin_fixture'] != (-1 if asci0_pin_fixture is None else asci0_pin_fixture)
                    or type(report['asci0_last_transmitted_byte']) is not int
                    or not -1 <= report['asci0_last_transmitted_byte'] <= 255
                    or (report['asci0_transmitted_bytes'] == 0) != (report['asci0_last_transmitted_byte'] == -1)
                    or (asci0_pin_fixture is None and (report['asci0_transmitted_bytes'] or report['asci0_interrupts']))):
                raise ValueError
            for key, length in (('panel_cells', 40 if panel_profile != 'unconnected' else 0),
                                ('panel_custom_codes', None)):
                values = report[key]
                if (not isinstance(values, list) or (length is not None and len(values) != length)
                        or (length is None and len(values) > 2)
                        or any(type(value) is not int or not 0 <= value <= 255 for value in values)):
                    raise ValueError
            if (len(set(report['panel_custom_codes'])) != len(report['panel_custom_codes'])
                    or report['panel_writes'] != report['panel_data_writes'] + report['panel_command_writes']
                    or report['panel_writes'] + report['panel_ignored_writes'] > report['board_writes']
                    or len(report['panel_custom_codes']) * 7 > report['panel_data_writes']
                    or (report['panel_pending_bytes'] and not report['panel_data_writes'])
                    or (panel_profile == 'unconnected' and (report['panel_custom_codes']
                        or any(report[key] for key in ('panel_writes', 'panel_data_writes',
                            'panel_command_writes', 'panel_ignored_writes', 'panel_cursor',
                            'panel_cursor_visible', 'panel_pending_bytes', 'panel_cooldown_clocks'))))):
                raise ValueError
            if (report['telephony_profile'] != telephony_profile
                    or any(type(report[key]) is not int or not 0 <= report[key] <= instructions
                           for key in TELEPHONY_COUNTERS)
                    or type(report['events_applied']) is not int
                    or not 0 <= report['events_applied'] <= len(events)
                    or type(report['panel_history_dropped']) is not int
                    or not 0 <= report['panel_history_dropped'] <= instructions):
                raise ValueError
            log = report['telephony_command_log']
            if (not isinstance(log, list) or len(log) != min(report['telephony_single_commands'], 64)
                    or any(type(byte) is not int or not 0 <= byte <= 255 for byte in log)
                    or (telephony_profile == 'unconnected'
                        and (log or any(report[key] for key in TELEPHONY_COUNTERS)))):
                raise ValueError
            log = report['coin_command_log']
            if (report['coin_profile'] != coin_profile
                    or any(type(report[key]) is not int or not 0 <= report[key] <= instructions
                           for key in COIN_COUNTERS)
                    or not isinstance(log, list) or len(log) != min(report['coin_commands'], 64)
                    or any(type(byte) is not int or not 0 <= byte <= 255 for byte in log)
                    or (coin_profile == 'unconnected' and (log or any(report[key] for key in COIN_COUNTERS)))):
                raise ValueError
            registers, writes, sent = (report['modem_registers'], report['modem_write_log'],
                                       report['modem_transmit_log'])
            if (report['modem_profile'] != modem_profile
                    or any(type(report[key]) is not int or not 0 <= report[key] <= instructions
                           for key in MODEM_COUNTERS)
                    or not isinstance(registers, list) or len(registers) != 4
                    or any(type(value) is not int or not 0 <= value <= 255 for value in registers)
                    or not isinstance(writes, list) or len(writes) != min(report['modem_writes'], 64)
                    or any(type(value) is not int or not 0 <= value <= 0x3ff for value in writes)
                    or not isinstance(sent, list) or len(sent) != min(report['modem_transmitted_bytes'], 64)
                    or any(type(byte) is not int or not 0 <= byte <= 255 for byte in sent)
                    or (modem_profile == 'unconnected' and (writes or sent or any(registers)
                        or any(report[key] for key in MODEM_COUNTERS)))):
                raise ValueError
            if (any(type(report[key]) is not int or report[key] < 0 for key in STREAM_COUNTERS)
                    or report['stream'] != (far_end is not None)
                    or report['stream_overruns'] > report['stream_rx_bytes']
                    or (far_end is None and any(report[key] for key in STREAM_COUNTERS))):
                raise ValueError
            if (report['card_profile'] != ('standin' if cards else 'unconnected')
                    or any(type(report[key]) is not int or not 0 <= report[key] <= instructions
                           for key in CARD_COUNTERS)
                    or report['card_count'] != len(cards)
                    or report['card_latched'] + report['card_lost'] + report['card_overruns'] != report['card_strobes']
                    or (not cards and any(report[key] for key in CARD_COUNTERS))
                    or any(type(report[key]) is not int or not 0 <= report[key] <= instructions
                           for key in ('coin_hello_replies', 'coin_raw_bytes'))
                    or (coin_profile == 'unconnected' and (report['coin_hello_replies'] or report['coin_raw_bytes']))
                    or report['panel_brightness'] not in (-1, 0, 0x10, 0x20, 0x30)
                    or type(report['panel_status_reads']) is not int
                    or not 0 <= report['panel_status_reads'] <= instructions
                    or (panel_profile == 'unconnected' and report['panel_status_reads'])
                    or report['speech_profile'] != speech_profile
                    or type(report['speech_writes']) is not int
                    or not 0 <= report['speech_writes'] <= instructions
                    or (speech_profile == 'unconnected' and report['speech_writes'])
                    or (panel_profile == 'unconnected' and report['panel_brightness'] != -1)):
                raise ValueError
            log = report['voice_log']
            if (any(type(report[key]) is not int or not 0 <= report[key] <= instructions for key in VOICE_COUNTERS)
                    or report['voice_busy'] not in (0, 1)
                    or report['voice_stops'] + report['voice_cuts'] + report['voice_busy'] != report['voice_starts']
                    or report['voice_beyond'] > report['voice_starts']
                    or not isinstance(log, list)
                    or len(log) != min(report['voice_starts'] + report['voice_stops'] + report['voice_cuts'], 64)
                    or any(not isinstance(entry, list) or len(entry) != 4
                           or any(type(value) is not int for value in entry)
                           or entry[1] not in VOICE_OPS or not 0 <= entry[2] <= 7 or not 0 <= entry[3] <= 255
                           for entry in log)
                    or [entry[0] for entry in log] != sorted(entry[0] for entry in log)
                    or report['voice_starts'] > report['speech_writes']
                    or (speech_profile != 'chip' and (log or report['voice_busy']
                        or any(report[key] for key in VOICE_COUNTERS)))):
                raise ValueError
            peeked = report['ram_peek']
            if (not isinstance(peeked, list) or [entry[0] if isinstance(entry, list) else None
                                                 for entry in peeked] != list(peek)
                    or any(len(entry) != 2 or type(entry[1]) is not int or not -1 <= entry[1] <= 255
                           for entry in peeked)):
                raise ValueError
            history = report['panel_history']
            if (not isinstance(history, list) or len(history) > 40
                    or (panel_profile == 'unconnected' and (history or report['panel_history_dropped']))):
                raise ValueError
            previous = 0
            for entry in history:
                if (not isinstance(entry, list) or len(entry) != 2 or type(entry[0]) is not int
                        or not previous <= entry[0] or not isinstance(entry[1], str)
                        or len(entry[1]) != 80 or set(entry[1]) - set('0123456789abcdef')):
                    raise ValueError
                previous = entry[0]
            attempts = report['attempted_instructions']
            if (attempts < 1 or type(report['trace_tail_first_attempt']) is not int
                    or report['trace_tail_first_attempt'] != max(1, attempts - 63)
                    or len(report['trace_tail_pc']) != min(attempts, 64)
                    or len(report['trace_pc']) != min(attempts, 64)
                    or report['trace_tail_pc'][-1] != report['last_instruction_pc']
                    or (attempts <= 64 and report['trace_tail_pc'] != report['trace_pc'])):
                raise ValueError
            if report['stop_reason'] not in {
                'budget', 'halt_or_sleep', 'invalid_physical_address', 'unmapped_memory_read',
                'unmapped_memory_write', 'rom_write_requires_board_decode',
                'unsupported_external_io_read', 'unsupported_external_io_write',
                'unsupported_internal_io_read', 'unsupported_internal_io_write',
                'unsupported_serial_transmit',
                'unsupported_asci_enable', 'unsupported_asci_pin_read',
                'unsupported_asci_interrupt_enable', 'unsupported_asci_data_access',
                'unsupported_asci_transmit_mode', 'unsupported_asci_data_timing',
                'unsupported_refresh_enable', 'unsupported_m1_temporary_enable', 'unsupported_iostop',
                'unsupported_timer_configuration', 'unsupported_csio_transfer',
                'unsupported_csio_pin_mux', 'unsupported_csio_data_timing',
                'unsupported_external_interrupt_enable', 'unsupported_dma_configuration',
                'unresolved_board_input', 'unsupported_ppi_mode',
                'unsupported_panel_read', 'unsupported_panel_write_timing', 'unsupported_panel_protocol',
                'stream_closed', 'stream_protocol', 'stream_receive_overflow',
            }:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise DiagnosticError('invalid Z180 worker report') from None
    return dict(report, family='millennium', profile=profile, firmware_sha256=digest,
                firmware_identifier=IMAGES.get(digest, {}).get('identifier', 'synthetic'),
                board_verified=False, boot_verified=False, implemented=False,
                clock_verified=False, peripheral_timing_verified=False,
                trace_truncated=report['attempted_instructions'] > 64,
                backend_revision='32592c760b169bbd71f6a3b41f2e2fb08769b145')


def _event_argument(kind, value):
    kind, value = event_wire(kind, value)
    if kind == 'k':
        return kind, chr(value)
    return kind, format(value, 'x') if kind in HEX_EVENTS else value


def parse_event(text):
    ms, _, action = text.partition(':')
    if ms.isdigit() and action[:1] == 'k' and action[1:] in KEY_OPCODES:
        return int(ms), 'k', action[1:]
    if not ms.isdigit() or len(action) < 2 or action[0] not in EVENT_KINDS:
        raise DiagnosticError('invalid event')
    try:
        value = int(action[1:], 16 if action[0] in HEX_EVENTS else 10)
    except ValueError:
        raise DiagnosticError('invalid event') from None
    return int(ms), action[0], value


def panel_text(cells):
    """The panel as text: ASCII as itself, every other code as '~'."""
    return ''.join(chr(c) if 0x20 <= c <= 0x7e else '~' for c in cells)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--firmware', required=True)
    parser.add_argument('--worker', required=True)
    parser.add_argument('--profile', choices=PROFILES, default='rom-only')
    parser.add_argument('--instructions', type=int, default=10000)
    parser.add_argument('--status80-contacts', type=int, choices=range(16),
                        help='explicit lab fixture for port 80 bits 7..4; unset stops on unresolved contacts')
    parser.add_argument('--int2-period-clocks', type=int,
                        help='explicit lab periodic INT2 fixture in modeled phi clocks; disabled by default')
    parser.add_argument('--status-e0-fixture', type=int,
                        help='explicit whole-byte E0 input hypothesis (0..255); unset keeps the hardware gate')
    parser.add_argument('--panel-profile', choices=PANEL_PROFILES, default='unconnected',
                        help='opt-in provisional A0/PPI-B custom-panel write subset; no status response')
    parser.add_argument('--asci0-pin-fixture', type=int, choices=range(4),
                        help='explicit ASCI0 physical levels: bit 0 CTS, bit 1 DCD; TX monitor only, no RX')
    parser.add_argument('--telephony', choices=TELEPHONY_PROFILES, default='unconnected',
                        help='opt-in inferred telephony-board stand-in on the CSI/O; needs E0 and INT2 fixtures')
    parser.add_argument('--coin', choices=COIN_PROFILES, default='unconnected',
                        help='opt-in inferred coin-mechanism stand-in on ASCI0; needs the telephony stand-in and DCD low')
    parser.add_argument('--modem', choices=MODEM_PROFILES, default='unconnected',
                        help='opt-in inferred 73K212 control bus on PPI port A and its data on ASCI1; '
                             'needs the telephony stand-in. Nothing answers in a batch run: bytes sent are only '
                             'recorded (run_diagnostic(far_end=...) gives ASCI1 a live far end)')
    parser.add_argument('--card', action='append', default=[], metavar='[HOW:]TRACK',
                        help='opt-in inferred mag-stripe reader: a card whose track 2 is TRACK (digits and =); '
                             'HOW is both (default), in, out or none. Needs the contacts fixture')
    parser.add_argument('--coin-hello', default='A', metavar='CHAR',
                        help="the coin stand-in's answer to g (default A; empty for silence)")
    parser.add_argument('--speech', choices=SPEECH_PROFILES, default='unconnected',
                        help='silent: an inferred sink for the sample numbers the firmware writes to port C0; '
                             'it never asks for the next. chip: the uPD7759 [inferred], timed from --voiceware; '
                             'the report lists which samples played (millennium_voice.py decodes them)')
    parser.add_argument('--voiceware', help='the voice ROM, for --speech chip')
    parser.add_argument('--event', action='append', default=[], metavar='MS:KV',
                        help='scheduled input: 5000:h1 handset lifted at 5 s, 6000:k# a key, 7000:c3 a coin on '
                             'channel 3, 8000:s13 contacts become 13, 9000:x22 a raw telephony opcode (hex), '
                             '60000:m16 the 73K212 detect register (hex), 70000:w0 swipe card 0, 71000:b45 the '
                             'coin mechanism sends E (hex), 72000:kV+ any named key (A1..A10 V- V+ next lang)')
    parser.add_argument('--peek', action='append', default=[], metavar='HEX',
                        help='logical RAM address whose byte the report shows at the end (up to 8)')
    args = parser.parse_args(argv)
    try:
        events = tuple(parse_event(text) for text in args.event)
        if any(not all(c in '0123456789abcdefABCDEF' for c in a) or not a for a in args.peek):
            raise DiagnosticError('invalid RAM peek')
        report = run_diagnostic(args.firmware, args.worker, profile=args.profile,
                                instructions=args.instructions, status80_contacts=args.status80_contacts,
                                int2_period_clocks=args.int2_period_clocks,
                                status_e0_fixture=args.status_e0_fixture, panel_profile=args.panel_profile,
                                asci0_pin_fixture=args.asci0_pin_fixture,
                                telephony_profile=args.telephony, events=events,
                                coin_profile=args.coin, peek=tuple(int(a, 16) for a in args.peek),
                                modem_profile=args.modem,
                                cards=tuple(tuple(card.split(':', 1)) if ':' in card else card
                                            for card in args.card),
                                coin_hello=args.coin_hello or None, speech_profile=args.speech,
                                voiceware=args.voiceware)
    except (OSError, InventoryError, DiagnosticError):
        print('Millennium reset diagnostic refused: check firmware, worker and budget.', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
