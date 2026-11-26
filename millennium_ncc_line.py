"""The NCC's line, for an emulated Millennium: dialled by the firmware,
answered by the real mm_manager through atshim.

A streamed worker run (`millennium_z180.run_diagnostic(..., far_end=...)`)
hands this object everything the emulated control board does that the
line can see: DTMF digits written to the 73K212, the bytes it puts on
ASCI1, its panel. This plays the telephone network and the two modems in
between, and nothing else:

- A call is placed when the 73K212 has sent the NCC number as DTMF (TR
  `9d` per digit), as a TCP
  connection to atshim's wire port, exactly as the relay makes on the
  KL5 server. The shim RINGs, mm_manager answers. INSTALL
  dials the NCC twice, two different ways, and each is answered the way
  the firmware listens for it:

  * the answer-detect check tells the telephony board the number (`3E`,
    then `10|d` per digit) and sends the digits as DTMF from the 73K212.
    Its answer is the board's `68`, a second after the last digit; sent
    before the last digit it is ignored and the check fails (`51`). The
    call ends when the handset goes back on hook -- the craftsperson's
    hand, a scheduled event;
  * the download sends the DTMF alone, with no `3E`. Its answer is
    the 73K212's detect register: answer tone (`16`), then a second later
    answer tone with carrier and marks (`3E`). The call ends a moment
    after the terminal sends a packet with the disconnect flag.

  Those answers are the ones the batch run found the firmware waiting for
  (TELEPHONY_PROTOCOL §11). That `68` is the board's answer report, and
  what the real chip reads at each moment of a 212A handshake, are
  inferred.
- Bytes then pass straight through, both ways: no audio, no modulation.
  The worker delivers the manager's bytes one 1200 Bd frame apart.
- When the terminal sends a packet with the disconnect flag, the call is
  hung up a moment later: the wire closes, which is atshim's NO CARRIER to
  the manager, and the detect register drops to nothing.

Everything observed is kept: each panel screen with its modelled time,
every packet each way (split on mm_manager's framing), and the table IDs
the manager sent. Standard library only.
"""

import socket
import time

STX, ETX = 0x02, 0x03
FLAG_DISCONNECT = 0x20
FLAG_ACK = 0x08
TABLE_UPD_ACK = 0x0E
ANSWER_REPORT = 0x68       # telephony board: the far end answered [inferred]
DIAL = 0x3E                # telephony board: dial what follows (10|d each)
ANSWER_TONE = 0x16
CONNECTED = 0x3E
ANSWER_TO_CARRIER = 1.0     # seconds of answer tone before the handshake
HANGUP_AFTER = 1.0          # seconds after a disconnect packet
RING_TO_ANSWER = 1.0        # seconds from placing a board call to its answer


def packets(data):
    """Split mm_manager framing (STX, flags, length, ..., CRC, ETX) out of
    a byte stream -> (packets, leftover). Bytes before an STX are dropped,
    as the manager's own receiver drops them."""
    found = []
    data = bytes(data)
    while data:
        start = data.find(bytes([STX]))
        if start < 0:
            return found, b''
        data = data[start:]
        if len(data) < 3 or len(data) < data[2] + 1:
            return found, data
        found.append(data[:data[2] + 1])
        data = data[data[2] + 1:]
    return found, b''


def crc16(data):
    """mm_manager's CRC-16: reflected polynomial A001, from zero."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = crc >> 1 ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def packet_ok(packet):
    return (len(packet) >= 6 and packet[0] == STX and packet[-1] == ETX
            and int.from_bytes(packet[-3:-1], 'little') == crc16(packet[:-3]))


class ManagerLine(object):

    def __init__(self, wire, ncc_number, *, finish_after_calls=None, finish_screen=None,
                 hold_after=5.0, log=None):
        """`wire` is atshim's (host, port). The run is finished (the worker
        told to stop and report) `hold_after` seconds after the
        `finish_after_calls`-th call ends, or after a screen containing
        `finish_screen` has been seen, whichever comes first."""
        self.wire_address = wire
        self.ncc = [int(d) for d in str(ncc_number)]
        self.finish_after_calls = finish_after_calls
        self.finish_screen = finish_screen
        self.hold_after = hold_after
        self.log = log or (lambda text: None)
        self.ms = 0
        self.digits = []           # DTMF from the 73K212
        self.board_digits = None   # digits after the board's 3E, or None
        self.board_armed = False   # the board was told to dial the NCC
        self.wire = None
        self.kind = None           # 'board' or 'modem' while a call is up
        self.calls = 0
        self.screens = []              # (modelled ms, 40 cells)
        self.telephony = []            # (modelled ms, opcode)
        self.modem_writes = []         # (modelled ms, address, value)
        self.packets = []              # (modelled ms, 'phone' | 'manager', bytes)
        self._from_phone = b''
        self._from_manager = b''
        self._carrier_at = None
        self._answer_at = None
        self._hangup_at = None
        self._finish_at = None
        self.finished = False

    # -- what the worker reports ------------------------------------------
    def on_message(self, link, tag, payload):
        if tag == 'Z':
            self.ms = int.from_bytes(payload, 'little')
        elif tag == 'S':
            self.ms = max(self.ms, int.from_bytes(payload[:4], 'little'))
            payload = payload[4:]
            self.screens.append((self.ms, bytes(payload)))
            text = ''.join(chr(c) if 0x20 <= c <= 0x7e else '~' for c in payload)
            self.log('%7d ms  panel  %s' % (self.ms, text))
            if self.finish_screen and self.finish_screen in text and self._finish_at is None:
                self._finish_at = time.monotonic() + self.hold_after
        elif tag == 'C':
            opcode = payload[0]
            self.telephony.append((self.ms, opcode))
            if opcode == DIAL:
                self.board_digits = []
            elif self.board_digits is not None and opcode & 0xF0 == 0x10 and opcode & 0x0F < 10:
                self.board_digits.append(opcode & 0x0F)
                if self.board_digits == self.ncc and self.wire is None:
                    self.board_digits = None
                    self.board_armed = True
            elif self.board_digits is not None:
                self.board_digits = None
        elif tag == 'H':
            self.log('%7d ms  hook   %s' % (self.ms, 'lifted' if payload[0] else 'hung up'))
            if not payload[0] and self.kind == 'board':
                self._hang_up(link, 'the handset went back on hook')
        elif tag == 'W':
            address, value = payload
            self.modem_writes.append((self.ms, address, value))
            # TR 9d: the 73K212 sends DTMF digit d (TELEPHONY_PROTOCOL §11).
            if address == 3 and value & 0xF0 == 0x90 and self.wire is None:
                self.digits.append(value & 0x0F)
                if self.digits[-len(self.ncc):] == self.ncc:
                    self._dial(link, 'board' if self.board_armed else 'modem')
        elif tag == 'D':
            if self.wire is not None:
                self.wire.sendall(payload)
            self._from_phone = self._record('phone', self._from_phone + payload, link)

    def _record(self, side, data, link):
        found, rest = packets(data)
        for packet in found:
            self.packets.append((self.ms, side, packet))
            self.log('%7d ms  %-7s %s%s' % (self.ms, side, packet.hex(' '),
                                            '' if packet_ok(packet) else '  (bad CRC)'))
            if side == 'phone' and packet[1] & FLAG_DISCONNECT and self.wire is not None:
                self._hangup_at = time.monotonic() + HANGUP_AFTER
        return rest

    # -- the line ---------------------------------------------------------
    def _dial(self, link, kind):
        self.digits = []
        self.board_armed = False
        self.calls += 1
        self.kind = kind
        self.log('%7d ms  line   the NCC dialled by the %s; call %d to %s:%d'
                 % ((self.ms, 'telephony board' if kind == 'board' else '73K212', self.calls)
                    + tuple(self.wire_address)))
        self.wire = socket.create_connection(self.wire_address, timeout=10)
        self.wire.setblocking(False)
        if kind == 'board':
            self._answer_at = time.monotonic() + RING_TO_ANSWER
        else:
            link.detect(ANSWER_TONE)
            self._carrier_at = time.monotonic() + ANSWER_TO_CARRIER

    def _hang_up(self, link, why):
        self.log('%7d ms  line   hung up (%s)' % (self.ms, why))
        try:
            self.wire.close()
        finally:
            self.wire = None
        self._hangup_at = self._carrier_at = self._answer_at = None
        self._from_manager = b''
        if self.kind == 'modem':
            link.detect(0)
        self.kind = None
        self.digits = []
        if self.finish_after_calls and self.calls >= self.finish_after_calls and self._finish_at is None:
            self._finish_at = time.monotonic() + self.hold_after

    def selectables(self):
        return [(self.wire, self._wire_readable)] if self.wire is not None else []

    def _wire_readable(self, link):
        try:
            data = self.wire.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b''
        if not data:
            self._hang_up(link, 'the manager side closed')
            return
        link.receive(data)
        self._from_manager = self._record('manager', self._from_manager + data, link)

    def poll(self, link):
        now = time.monotonic()
        if self._answer_at is not None and now >= self._answer_at:
            self._answer_at = None
            link.telephony(ANSWER_REPORT)
        if self._carrier_at is not None and now >= self._carrier_at:
            self._carrier_at = None
            link.detect(CONNECTED)
        if self._hangup_at is not None and now >= self._hangup_at:
            self._hang_up(link, 'the terminal disconnected')
        if self._finish_at is not None and now >= self._finish_at and not self.finished:
            self.finished = True
            if self.wire is not None:
                self._hang_up(link, 'end of run')
            link.finish()

    # -- what happened ----------------------------------------------------
    def tables_acknowledged(self):
        """Table IDs the terminal acknowledged, in order: its
        TABLE_UPD_ACK (0E) packets, terminal ID then 0E then the table ID.
        This is the firmware saying it took a table, not the manager
        saying it sent one."""
        return [packet[9] for _, side, packet in self.packets
                if side == 'phone' and len(packet) > 12 and packet[8] == TABLE_UPD_ACK]

    def table_bytes(self):
        """Payload bytes of the manager's data packets (not bare ACKs)."""
        return sum(len(packet) - 6 for _, side, packet in self.packets
                   if side == 'manager' and len(packet) > 6)
