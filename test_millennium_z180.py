"""Synthetic CPU contracts and private-worker boundaries; no terminal ROMs."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import millennium_z180 as diagnostic
import millennium_ncc_line as ncc
import millennium_craft as craft
from z180.build_millennium_z180 import build


def report():
    return dict(schema_version=13, stop_reason='budget', attempted_instructions=1,
                fault_address=0, last_instruction_pc=0, pc=1, sp=0,
                cbr=0, bbr=0, cbar=240, iocr=0, rcr=192, dcntl=240, omcr=224,
                tcr=0, cntr=7, itc=1, il=0, dstat=48, trace_pc=[0],
                trace_tail_first_attempt=1, trace_tail_pc=[0],
                board_reads=0, board_writes=0, eeprom_reads=0, eeprom_writes=0, status80_contacts=-1,
                modeled_clocks=0, timer0_interrupts=0, timer1_interrupts=0, iff1=0,
                int2_period_clocks=0, clock_input_events=0, external2_interrupts=0, status_e0_fixture=-1,
                panel_profile='unconnected', panel_writes=0, panel_data_writes=0,
                panel_command_writes=0, panel_ignored_writes=0, panel_cursor=0,
                panel_cursor_visible=0, panel_pending_bytes=0, panel_cooldown_clocks=0,
                panel_cells=[], panel_custom_codes=[], asci0_pin_fixture=-1,
                asci0_transmitted_bytes=0, asci0_last_transmitted_byte=-1, asci0_interrupts=0,
                telephony_profile='unconnected', telephony_command_log=[], events_applied=0,
                panel_history_dropped=0, panel_history=[], coin_profile='unconnected',
                coin_command_log=[], ram_peek=[], modem_profile='unconnected',
                modem_registers=[0, 0, 0, 0], modem_write_log=[], modem_transmit_log=[],
                card_profile='unconnected', coin_hello_replies=0, coin_raw_bytes=0, panel_brightness=-1, panel_status_reads=0, speech_profile='unconnected', speech_writes=0,
                voice_busy=0, voice_log=[],
                **{key: 0 for key in diagnostic.TELEPHONY_COUNTERS + diagnostic.COIN_COUNTERS
                   + diagnostic.MODEM_COUNTERS + diagnostic.STREAM_COUNTERS + diagnostic.CARD_COUNTERS
                   + diagnostic.VOICE_COUNTERS})


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.image = Path(self.temp.name) / 'private-original.bin'
        self.data = bytes(262144)
        self.image.write_bytes(self.data)
        self.identity = patch.object(diagnostic, 'EXECUTABLE_FIRMWARE', (hashlib.sha256(self.data).hexdigest(),))
        self.identity.start()
        self.addCleanup(self.identity.stop)

    def fake_worker(self, payload, *, stderr=b'', failure=None):
        def run(args, **kwargs):
            self.assertNotEqual(args[1], str(self.image))
            self.assertEqual(Path(args[1]).read_bytes(), self.data)
            self.assertEqual(Path(args[1]).stat().st_mode & 0o777, 0o400)
            self.assertEqual(kwargs['timeout'], diagnostic.WORKER_TIMEOUT)
            self.assertIs(kwargs['preexec_fn'], diagnostic.worker_limits)
            if failure:
                raise failure
            kwargs['stdout'].write(payload)
            kwargs['stderr'].write(stderr)
            return SimpleNamespace(returncode=0)
        return patch.object(diagnostic.subprocess, 'run', side_effect=run)

    def test_private_snapshot_and_truthful_flags(self):
        with self.fake_worker(json.dumps(report()).encode()):
            result = diagnostic.run_diagnostic(self.image, '/synthetic-worker', instructions=1)
        for flag in ('boot_verified', 'board_verified', 'implemented', 'clock_verified', 'peripheral_timing_verified'):
            self.assertIs(result[flag], False)
        self.assertNotIn('private-original', json.dumps(result))
        self.assertEqual(self.image.read_bytes(), self.data)

    def test_unknown_or_modified_image_never_launches(self):
        self.image.write_bytes(self.data[:-1] + b'X')
        with patch.object(diagnostic.subprocess, 'run') as launch:
            with self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/unused')
            launch.assert_not_called()

    def test_asci_pin_fixture_and_report_are_strictly_scoped(self):
        for pins in (True,-1,4,'0'):
            with self.subTest(pins=pins), self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/unused', profile='lab-board-startup',
                                          asci0_pin_fixture=pins)
        with self.assertRaises(diagnostic.DiagnosticError):
            diagnostic.run_diagnostic(self.image, '/unused', asci0_pin_fixture=0)
        for pins in range(4):
            with self.fake_worker(json.dumps(dict(report(), asci0_pin_fixture=pins)).encode()) as launch:
                result = diagnostic.run_diagnostic(self.image, '/unused', instructions=1,
                    profile='lab-board-startup', asci0_pin_fixture=pins)
                self.assertEqual(launch.call_args.args[0][4:], ['-1','0','-1','unconnected',str(pins)])
                self.assertEqual(result['asci0_pin_fixture'], pins)
        for fields in ({'asci0_pin_fixture':True}, {'asci0_pin_fixture':1},
                       {'asci0_transmitted_bytes':1}, {'asci0_last_transmitted_byte':0},
                       {'asci0_last_transmitted_byte':256}, {'asci0_last_transmitted_byte':True},
                       {'asci0_interrupts':True}, {'asci0_interrupts':2}):
            with self.subTest(fields=fields), self.fake_worker(json.dumps(dict(report(), **fields)).encode()):
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.run_diagnostic(self.image, '/unused', instructions=1,
                        profile='lab-board-startup', asci0_pin_fixture=0)
        for fields in ({'asci0_transmitted_bytes':1,'asci0_last_transmitted_byte':0},
                       {'asci0_interrupts':1}):
            with self.fake_worker(json.dumps(dict(report(), **fields)).encode()):
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.run_diagnostic(self.image, '/unused', instructions=1)

    def test_budget_and_profile_validation(self):
        for kwargs in ({'instructions': 0}, {'instructions': 100001},
                       {'instructions': True}, {'profile': 'real-board'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/unused', **kwargs)
        for kwargs in ({'status80_contacts': 0},
                       {'profile': 'lab-board-startup', 'status80_contacts': True},
                       {'profile': 'lab-board-startup', 'status80_contacts': 16},
                       {'profile': 'lab-board-startup', 'instructions': 64000001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/unused', **kwargs)
        for period in (True,0,19,6144001):
            with self.subTest(period=period), self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/unused', profile='lab-board-startup',
                                           int2_period_clocks=period)
        with self.assertRaises(diagnostic.DiagnosticError):
            diagnostic.run_diagnostic(self.image, '/unused', int2_period_clocks=61440)
        for status in (True,-1,256,'64'):
            with self.subTest(status=status), self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/unused', profile='lab-board-startup',
                                           status_e0_fixture=status)
        with self.assertRaises(diagnostic.DiagnosticError):
            diagnostic.run_diagnostic(self.image, '/unused', status_e0_fixture=64)

    def test_stream_framing_and_far_end_requirements(self):
        messages, rest = diagnostic.split_stream(b'D\x02W\x03\x91Z\x10\x27\x00\x00H\x01S' + bytes(10))
        self.assertEqual(messages, [('D', b'\x02'), ('W', b'\x03\x91'), ('Z', b'\x10\x27\x00\x00'),
                                    ('H', b'\x01')])
        self.assertEqual(rest, b'S' + bytes(10))
        with self.assertRaises(diagnostic.DiagnosticError):
            diagnostic.split_stream(b'Q\x00')
        far_end = SimpleNamespace(on_message=lambda *a: None)
        startup = dict(profile='lab-board-startup', status_e0_fixture=0x40, int2_period_clocks=61440,
                       telephony_profile='standin')
        with self.assertRaises(diagnostic.DiagnosticError):      # no modem, no far end
            diagnostic.run_diagnostic(self.image, '/unused', far_end=far_end, **startup)
        with self.assertRaises(diagnostic.DiagnosticError):      # minutes, not forever
            diagnostic.run_diagnostic(self.image, '/unused', far_end=far_end, modem_profile='control',
                                      instructions=diagnostic.MAX_STREAM_INSTRUCTIONS + 1, **startup)
        with self.assertRaises(diagnostic.DiagnosticError):      # the batch ceiling still holds
            diagnostic.run_diagnostic(self.image, '/unused', modem_profile='control',
                                      instructions=diagnostic.MAX_STARTUP_INSTRUCTIONS + 1, **startup)
        # Paced, the worker mostly sleeps: its CPU allowance is under its wall one.
        self.assertLess(diagnostic.STREAM_CPU_SECONDS, diagnostic.STREAM_WALL_SECONDS)

    def test_events_go_on_the_wire_as_the_worker_takes_them(self):
        sent = []
        link = diagnostic.StreamLink(SimpleNamespace(sendall=sent.append))
        link.event('k', '5')
        link.event('k', '#')
        link.event('k', 'A10')          # a named key is its telephony opcode
        link.event('k', 'lang')
        link.event('h', 1)
        link.event('w', 7)
        link.event('b', 0x47)
        self.assertEqual(sent, [b'Ek5', b'Ek#', b'X\x52', b'X\x5a', b'Eh\x01', b'Ew\x07', b'EbG'])
        for kind, value in (('k', 'A11'), ('k', 'V'), ('k', 5), ('h', 2), ('w', 8), ('b', 256),
                            ('b', -1), ('c', 0), ('c', 17), ('q', 0), ('w', '1')):
            with self.subTest(kind=kind, value=value), self.assertRaises(diagnostic.DiagnosticError):
                link.event(kind, value)
        self.assertEqual(len(sent), 7)
        # Every key on the set has its own opcode, and the dial pad's are the
        # ones the stand-in's `k` already sends.
        self.assertEqual(len(diagnostic.KEY_OPCODES), 26)
        self.assertEqual(len(set(diagnostic.KEY_OPCODES.values())), 26)
        self.assertEqual([diagnostic.KEY_OPCODES[k] for k in '1234567890*#'], list(range(0x22, 0x3a, 2)))
        self.assertEqual(diagnostic.parse_event('900:kV+'), (900, 'k', 'V+'))
        self.assertEqual(diagnostic.parse_event('900:b47'), (900, 'b', 0x47))
        self.assertEqual(diagnostic.parse_event('900:w2'), (900, 'w', 2))
        self.assertEqual(diagnostic.card_spec('4761=2512'), 'b4761=2512')
        self.assertEqual(diagnostic.card_spec(('out', '4761=2512')), 'o4761=2512')
        for card in ('', '4' * 38, '4761?', ('sideways', '4761'), ('both', 4761)):
            with self.subTest(card=card), self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.card_spec(card)

    def test_operator_follows_the_panel_and_hands_the_rest_to_the_line(self):
        import millennium_craft as craft
        events, forwarded, finished = [], [], []
        link = SimpleNamespace(event=lambda k, v: events.append((k, v)), finish=lambda: finished.append(1))
        line = SimpleNamespace(on_message=lambda link, tag, payload: forwarded.append(tag),
                               poll=lambda link: None, selectables=lambda: [])
        steps = [craft.Step('Enter PIN', craft.keys('55', 0, 0)),
                 craft.Step('Completed', [(0, 'k', '*')], name='first'),
                 craft.Step('Completed', [(0, 'h', 0)], name='second'),
                 craft.Step('never shown', [], timeout=0)]
        op = craft.Operator(steps, line, hold_after=0)

        def screen(ms, text):
            op.on_message(link, 'S', ms.to_bytes(4, 'little') + text.ljust(40).encode())
            op.poll(link)

        screen(10, 'out of service')
        self.assertEqual(events, [])
        screen(20, 'Enter PIN:')
        self.assertEqual(events, [('k', '5'), ('k', '5')])
        # One screen starts one step; the same text starts the next step
        # only on a later screen, after the first one's actions are done
        # (the calibration step's prompt is shown twice).
        screen(30, 'Completed: 00')
        self.assertEqual(events[2:], [('k', '*')])
        screen(40, 'Completed: 00')
        self.assertEqual(events[3:], [('h', 0)])
        self.assertEqual([name for _, name in op.done], ['Enter PIN', 'first', 'second'])
        self.assertEqual(op.screens_during('first'), ['Completed: 00'.ljust(40)])
        self.assertEqual(op.screens_during('second'), ['Completed: 00'.ljust(40)])
        op.on_message(link, 'Z', (50).to_bytes(4, 'little'))
        op.poll(link)
        self.assertEqual(op.failed, 'never shown')       # a zero timeout gives up at once
        self.assertEqual(finished, [1])
        self.assertEqual(forwarded, ['S', 'S', 'S', 'S', 'Z'])

    def test_ncc_line_places_answers_and_hangs_up_both_calls(self):
        # The answer-detect call and the download call, against a listener
        # standing in for atshim's wire port.
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        self.addCleanup(listener.close)
        sent = []
        link = SimpleNamespace(detect=lambda v: sent.append(('M', v)), receive=lambda d: sent.append(('R', d)),
                               telephony=lambda op: sent.append(('X', op)), finish=lambda: sent.append(('end',)))
        line = ncc.ManagerLine(listener.getsockname(), '6135559999', finish_after_calls=2, hold_after=0)
        digits = [6, 1, 3, 5, 5, 5, 9, 9, 9, 9]
        line.on_message(link, 'C', bytes([0x3e]))
        for d in digits:
            line.on_message(link, 'C', bytes([0x10 | d]))
        self.assertIsNone(line.wire)                         # told, not yet dialled
        for d in digits:
            line.on_message(link, 'W', bytes([3, 0x90 | d]))
        self.assertEqual((line.kind, line.calls), ('board', 1))
        manager, _ = listener.accept()
        self.addCleanup(manager.close)
        line._answer_at = 0
        line.poll(link)
        self.assertEqual(sent, [('X', 0x68)])                # the board's answer report
        line.on_message(link, 'H', b'\x00')
        self.assertIsNone(line.wire)
        self.assertEqual(manager.recv(16), b'')              # atshim's NO CARRIER
        # The download: DTMF from the 73K212 alone, answered on DR.
        sent.clear()
        for d in [1] + digits:
            line.on_message(link, 'W', bytes([3, 0x90 | d]))
        self.assertEqual((line.kind, line.calls), ('modem', 2))
        manager, _ = listener.accept()
        self.addCleanup(manager.close)
        line._carrier_at = 0
        line.poll(link)
        self.assertEqual(sent, [('M', 0x16), ('M', 0x3e)])
        packet = bytes.fromhex('02000b6135550100')
        packet += b'\x24'
        packet += ncc.crc16(packet).to_bytes(2, 'little') + b'\x03'
        self.assertEqual(packet.hex(), '02000b61355501002467c203')   # what the firmware sent
        self.assertTrue(ncc.packet_ok(packet))
        for byte in packet:
            line.on_message(link, 'D', bytes([byte]))
        self.assertEqual(manager.recv(64), packet)
        manager.sendall(bytes.fromhex('02080566030302'))
        line._wire_readable(link)
        self.assertEqual(sent[-1], ('R', bytes.fromhex('02080566030302')))
        for byte in bytes.fromhex('022005780303'):
            line.on_message(link, 'D', bytes([byte]))
        line._hangup_at = 0
        line.poll(link)
        self.assertEqual(sent[-1], ('M', 0))
        line.poll(link)
        self.assertEqual(sent[-1], ('end',))
        self.assertEqual([side for _, side, _ in line.packets], ['phone', 'manager', 'phone'])

    def test_worker_payload_refused(self):
        bad = report()
        bad['rom_bytes'] = 'private'
        for payload in (b'not json', json.dumps(bad).encode(), b'X' * 16385,
                        json.dumps(dict(report(), schema_version=1)).encode(),
                        json.dumps(dict(report(), schema_version=2)).encode(),
                        json.dumps(dict(report(), schema_version=3)).encode(),
                        json.dumps(dict(report(), schema_version=4)).encode(),
                        json.dumps(dict(report(), schema_version=5)).encode(),
                        json.dumps(dict(report(), schema_version=6)).encode(),
                        json.dumps(dict(report(), schema_version=7)).encode(),
                        json.dumps(dict(report(), schema_version=9)).encode(),
                        json.dumps(dict(report(), schema_version=10)).encode(),
                        json.dumps(dict(report(), schema_version=11)).encode(),
                        json.dumps(dict(report(), schema_version=12)).encode(),
                        # No chip was asked for, so nothing can have played.
                        json.dumps(dict(report(), voice_starts=1, voice_busy=1, speech_writes=1,
                                        voice_log=[[0, 1, 0, 3]])).encode(),
                        json.dumps(dict(report(), int0_interrupts=1)).encode(),
                        # No reader was asked for, so none can have swiped.
                        json.dumps(dict(report(), card_swipes=1)).encode(),
                        json.dumps(dict(report(), panel_brightness=0x10)).encode(),
                        # A batch run never streamed, so it cannot say it did.
                        json.dumps(dict(report(), stream=1)).encode(),
                        json.dumps(dict(report(), stream_rx_bytes=1)).encode(),
                        json.dumps(dict(report(), trace_pc=[65536])).encode(),
                        json.dumps(dict(report(), attempted_instructions=2)).encode(),
                        json.dumps(dict(report(), stop_reason='booted')).encode()):
            with self.fake_worker(payload), self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/synthetic-worker', instructions=1)

    def test_board_counters_and_fixture_must_match_the_request(self):
        for fields in ({'board_reads': True}, {'board_writes': -1}, {'board_writes': 1},
                       {'eeprom_reads': 1}, {'eeprom_writes': 1}, {'status80_contacts': 0}):
            with self.subTest(fields=fields), self.fake_worker(json.dumps(dict(report(), **fields)).encode()):
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.run_diagnostic(self.image, '/synthetic-worker', instructions=1)
        with self.fake_worker(json.dumps(dict(report(), status80_contacts=15)).encode()):
            result = diagnostic.run_diagnostic(self.image, '/synthetic-worker', profile='lab-board-startup',
                                               instructions=1, status80_contacts=15)
        self.assertEqual(result['status80_contacts'], 15)
        for fields in ({'int2_period_clocks': 61440}, {'external2_interrupts': 1},
                       {'clock_input_events': 1}):
            with self.fake_worker(json.dumps(dict(report(), **fields)).encode()):
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.run_diagnostic(self.image, '/synthetic-worker', instructions=1)
        with self.fake_worker(json.dumps(dict(report(), int2_period_clocks=20, clock_input_events=1)).encode()):
            with self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/synthetic-worker', profile='lab-board-startup',
                                           instructions=1, int2_period_clocks=20)

    def test_timeout_and_stderr_are_sanitized(self):
        for kwargs in ({'stderr': b'private contents'},
                       {'failure': subprocess.TimeoutExpired('private path', 5)}):
            with self.fake_worker(json.dumps(report()).encode(), **kwargs):
                with self.assertRaises(diagnostic.DiagnosticError) as caught:
                    diagnostic.run_diagnostic(self.image, '/synthetic-worker', instructions=1)
                self.assertNotIn('private', str(caught.exception))

    def test_status_hypothesis_is_explicit_and_report_must_match(self):
        for status in (0,64,255):
            with self.subTest(status=status), self.fake_worker(json.dumps(
                    dict(report(), status_e0_fixture=status)).encode()) as launch:
                result = diagnostic.run_diagnostic(self.image, '/synthetic-worker',
                    profile='lab-board-startup', instructions=1, status_e0_fixture=status)
                self.assertEqual(launch.call_args.args[0][4:], ['-1','0',str(status)])
                self.assertEqual(result['status_e0_fixture'], status)
                self.assertFalse(result['boot_verified'])
        for fields in ({'status_e0_fixture': 64}, {'status_e0_fixture': True}):
            with self.fake_worker(json.dumps(dict(report(), **fields)).encode()):
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.run_diagnostic(self.image, '/synthetic-worker', instructions=1)

    def test_tail_metadata_and_payload_bounds_are_enforced(self):
        for fields in ({'trace_tail_pc': [65536]}, {'trace_tail_pc': [True]},
                       {'trace_tail_pc': [0] * 65}, {'trace_tail_pc': []},
                       {'trace_tail_first_attempt': 0}, {'trace_tail_first_attempt': True},
                       {'last_instruction_pc': 1}, {'trace_pc': []}):
            with self.subTest(fields=fields), self.fake_worker(json.dumps(dict(report(), **fields)).encode()):
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.run_diagnostic(self.image, '/synthetic-worker', instructions=1)

    def test_panel_profile_is_explicit_and_arguments_preserve_unset_inputs(self):
        for profile in ('rom-only','lab-ram-40000'):
            with self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/unused', profile=profile,
                                          panel_profile='cu20026-t23a-lab')
        for panel in ('guessed-ready',True,None,1):
            with self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.run_diagnostic(self.image, '/unused', profile='lab-board-startup', panel_profile=panel)
        enabled = dict(report(), panel_profile='cu20026-t23a-lab', panel_cells=[32]*40,
                       panel_cooldown_clocks=921600, panel_cursor_visible=1)
        with self.fake_worker(json.dumps(enabled).encode()) as launch:
            result = diagnostic.run_diagnostic(self.image, '/worker', profile='lab-board-startup',
                instructions=1, panel_profile='cu20026-t23a-lab')
            self.assertEqual(launch.call_args.args[0][4:], ['-1','0','-1','cu20026-t23a-lab'])
            self.assertFalse(result['boot_verified'])
        for status,period,contacts in ((0,61440,15),(64,None,0)):
            fixture = dict(enabled, status_e0_fixture=status, int2_period_clocks=period or 0,
                           status80_contacts=contacts)
            with self.fake_worker(json.dumps(fixture).encode()) as launch:
                diagnostic.run_diagnostic(self.image, '/worker', profile='lab-board-startup', instructions=1,
                    panel_profile='cu20026-t23a-lab', status_e0_fixture=status,
                    int2_period_clocks=period, status80_contacts=contacts)
                self.assertEqual(launch.call_args.args[0][4:],
                                 [str(contacts),str(period or 0),str(status),'cu20026-t23a-lab'])

    def test_panel_report_size_types_counters_and_profile_are_checked(self):
        enabled = dict(report(), panel_profile='cu20026-t23a-lab', panel_cells=[32]*40)
        for fields in ({'panel_profile':'unconnected'}, {'panel_cells':[32]*39},
                       {'panel_cells':[32]*41}, {'panel_cells':[True]*40}, {'panel_cells':[256]*40},
                       {'panel_custom_codes':[1,2,3]}, {'panel_custom_codes':[1,1]},
                       {'panel_custom_codes':[True]}, {'panel_custom_codes':[1]},
                       {'panel_writes':1}, {'panel_data_writes':1}, {'panel_ignored_writes':1},
                       {'panel_cursor':40}, {'panel_cursor_visible':True},
                       {'panel_pending_bytes':7}, {'panel_pending_bytes':1},
                       {'panel_cooldown_clocks':921601}):
            with self.subTest(fields=fields), self.fake_worker(json.dumps(dict(enabled,**fields)).encode()):
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.run_diagnostic(self.image, '/worker', profile='lab-board-startup',
                                              instructions=1, panel_profile='cu20026-t23a-lab')
        for fields in ({'panel_cursor':1}, {'panel_cursor_visible':1}, {'panel_cells':[32]*40},
                       {'panel_cooldown_clocks':1}, {'panel_profile':'cu20026-t23a-lab'}):
            with self.fake_worker(json.dumps(dict(report(),**fields)).encode()):
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.run_diagnostic(self.image, '/worker', instructions=1)


@unittest.skipUnless(os.environ.get('MILLENNIUM_Z180_SOURCE'), 'external pinned Z180 source not supplied')
class NativeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.worker = Path(cls.temp.name) / 'z180-worker'
        cls.source = Path(os.environ['MILLENNIUM_Z180_SOURCE'])
        build(cls.source, cls.worker)

    def execute(self, program, *, profile='rom-only', instructions=100, **fixtures):
        image = Path(self.temp.name) / 'synthetic.bin'
        data = bytes(program) + bytes(262144 - len(program))
        image.write_bytes(data)
        with patch.object(diagnostic, 'EXECUTABLE_FIRMWARE', (hashlib.sha256(data).hexdigest(),)):
            return diagnostic.run_diagnostic(image, self.worker, profile=profile, instructions=instructions,
                                               **fixtures)

    def test_independent_cpu_and_mmu_contracts(self):
        result = subprocess.run([str(self.worker), '--self-test'], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b'ASCI CPU-clock/divisor/frame/queue/reset model contracts passed', result.stdout)
        self.assertIn(b'ASCI status/pin/latch/vector/mask/wakeup contracts passed', result.stdout)
        self.assertIn(b'Board PPI/Microwire startup model contracts passed', result.stdout)
        self.assertIn(b'Panel selector/packet/cursor/clear/delay/guard model contracts passed', result.stdout)
        self.assertIn(b'DMA0 I/O-to-memory edge/level request, count, end and interrupt contracts passed',
                      result.stdout)
        self.assertIn(b'Card track-2 encoding and swipe sequence contracts passed', result.stdout)
        self.assertIn(b'contracts passed', result.stdout)

    def test_board_profile_is_opt_in_and_unknown_devices_still_stop(self):
        program = [0x3e,0x84,0xed,0x39,0x60,0x76]
        for profile in ('rom-only', 'lab-ram-40000'):
            self.assertEqual(self.execute(program, profile=profile)['stop_reason'], 'unsupported_external_io_write')
        startup = self.execute(program, profile='lab-board-startup')
        self.assertEqual((startup['stop_reason'], startup['board_writes']), ('halt_or_sleep', 1))
        for program in ([0xdb,0x60], [0xdb,0xe0], [0xd3,0xa0]):
            with self.subTest(program=program):
                self.assertTrue(self.execute(program, profile='lab-board-startup')['stop_reason'].startswith('unsupported_external_io'))

    def test_ppi_output_aliases_bsr_and_mode_change(self):
        # Full Z80 OUT(n),A addresses carry A on the high address byte.
        program = [0x3e,0x80,0xd3,0x43,0x3e,0xa5,0xd3,0x40,
                   0x01,0x40,0x9a,0xed,0x78,0xfe,0xa5,0x20,0xfe,
                   0x3e,7,0xd3,0x43,0xed,0x38,0x42,0xfe,8,0x20,0xfe,
                   0xed,0x38,0x43,0xfe,0x80,0x20,0xfe,
                   0x3e,0x80,0xd3,0x43,0xed,0x38,0x40,0xb7,0x20,0xfe,0x76]
        result = self.execute(program, profile='lab-board-startup')
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['board_reads'], result['board_writes']), (4,4))

    # Original programs, independent of any terminal ROM: wait through the
    # explicit power policy, configure PPI-B output, then space byte writes.
    PANEL_START = [0x11,0xff,0xff,0x1b,0x7a,0xb3,0x20,0xfb,
                   0x3e,0x81,0xed,0x39,0x43]
    PANEL_PAUSE = [0x11,0x00,0x01,0x1b,0x7a,0xb3,0x20,0xfb]

    def panel_program(self, data, *, selector=0, pause=True):
        program = self.PANEL_START + [0x3e,selector,0xd3,0x41]
        for value in data:
            program += [0x3e,value,0xd3,0xa0]
            if pause: program += self.PANEL_PAUSE
        return program

    def test_executed_panel_text_wrap_and_command_selector(self):
        text = list(range(0x21,0x21+40))
        program = self.panel_program(text)
        program += [0x3e,0x20,0xd3,0x41,0x3e,23,0xed,0x39,0xa0] + self.PANEL_PAUSE
        program += [0x3e,0,0xd3,0x41,0x3e,0x5a,0xed,0x39,0xa0,0x76]
        result = self.execute(program, profile='lab-board-startup', instructions=400000,
                              panel_profile='cu20026-t23a-lab')
        text[23] = 0x5a
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['panel_cells'],result['panel_cursor']), (text,24))
        self.assertEqual((result['panel_writes'],result['panel_data_writes'],result['panel_command_writes']),
                         (42,41,1))
        self.assertFalse(result['boot_verified'])

    def test_documented_unlisted_panel_codes_are_null(self):
        nulls = list(range(10)) + [0x0b,0x0d,0x0e,0x0f,0x10,0x17,0x18,0x19,0x1c,0x1d,0x1e,0x1f]
        result = self.execute(self.panel_program([0x41] + nulls + [0x42]) + [0x76],
            profile='lab-board-startup', instructions=400000, panel_profile='cu20026-t23a-lab')
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['panel_cells'],result['panel_cursor']), ([0x41,0x42]+[32]*38,2))
        self.assertEqual(result['panel_data_writes'], len(nulls)+2)
        self.assertEqual((result['panel_cursor_visible'],result['panel_pending_bytes']), (1,0))

    def test_extended_font_codes_are_stored_as_codes(self):
        # 1.7's craft prompts use B7 (the FIX key's glyph) from the module's
        # own font. The cell keeps the code; no glyph is invented.
        result = self.execute(self.panel_program([0x41,0xa1,0xb7,0xff]) + [0x76],
            profile='lab-board-startup', instructions=400000, panel_profile='cu20026-t23a-lab')
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual(result['panel_cells'][:5], [0x41,0xa1,0xb7,0xff,32])
        self.assertEqual(diagnostic.panel_text(result['panel_cells'][:4]), 'A~~~')

    def test_executed_panel_packets_do_not_print_payload_and_reset_aborts(self):
        # Packet bytes deliberately overlap control/character codes.
        packet = [0x1b,0xb4,0x0c,0x14,0x41,0x1b,0xff]
        result = self.execute(self.panel_program(packet + [0xb4]) + [0x76],
            profile='lab-board-startup', instructions=300000, panel_profile='cu20026-t23a-lab')
        self.assertEqual((result['panel_custom_codes'],result['panel_pending_bytes']), ([0xb4],0))
        self.assertEqual(result['panel_cells'], [0xb4]+[32]*39)
        self.assertEqual(result['panel_cursor'], 1)
        for count in range(1,7):
            with self.subTest(count=count):
                program = self.panel_program(packet[:count])
                result = self.execute(program+[0x76], profile='lab-board-startup', instructions=300000,
                                      panel_profile='cu20026-t23a-lab')
                self.assertEqual((result['panel_custom_codes'],result['panel_pending_bytes']), ([],7-count))
                self.assertEqual(result['panel_cells'], [32]*40)
        program = self.panel_program(packet[:3]) + [0x3e,0x20,0xd3,0x41,0x3e,0x40,0xd3,0xa0,0x76]
        result = self.execute(program, profile='lab-board-startup', instructions=300000,
                              panel_profile='cu20026-t23a-lab')
        self.assertEqual((result['panel_custom_codes'],result['panel_pending_bytes'],result['panel_cursor']), ([],0,0))
        self.assertGreater(result['panel_cooldown_clocks'], 0)

    def test_executed_panel_unknown_reads_controls_and_early_writes_stop(self):
        for suffix in ([0xed,0x38,0xa0], [0x01,0xa0,0x9a,0xed,0x78]):
            result = self.execute(self.PANEL_START+suffix, profile='lab-board-startup',
                instructions=300000, panel_profile='cu20026-t23a-lab')
            self.assertEqual((result['stop_reason'],result['panel_writes']), ('unsupported_panel_read',0))
        for selector, byte in ((0,0x12),(0x20,0x41),(0x20,0xff)):
            with self.subTest(selector=selector, byte=byte):
                result = self.execute(self.panel_program([byte],selector=selector)+[0x76],
                    profile='lab-board-startup', instructions=300000, panel_profile='cu20026-t23a-lab')
                self.assertEqual((result['stop_reason'],result['panel_writes']), ('unsupported_panel_protocol',0))
        result = self.execute([0x3e,0x81,0xd3,0x43,0x3e,0x14,0xd3,0xa0],
            profile='lab-board-startup', panel_profile='cu20026-t23a-lab')
        self.assertEqual((result['stop_reason'],result['panel_writes']), ('unsupported_panel_write_timing',0))
        result = self.execute(self.panel_program([0x14,0x41],pause=False),
            profile='lab-board-startup', instructions=300000, panel_profile='cu20026-t23a-lab')
        self.assertEqual((result['stop_reason'],result['panel_writes'],result['panel_cursor']),
                         ('unsupported_panel_write_timing',1,0))

    def test_executed_panel_deselection_direction_and_opt_in(self):
        result = self.execute([0x3e,0x14,0xd3,0xa0], profile='lab-board-startup',
                              panel_profile='cu20026-t23a-lab')
        self.assertEqual(result['stop_reason'], 'unresolved_board_input')
        program = self.panel_program([0xff],selector=0x60)+[0x76]
        result = self.execute(program, profile='lab-board-startup', instructions=300000,
                              panel_profile='cu20026-t23a-lab')
        self.assertEqual((result['stop_reason'],result['panel_writes'],result['panel_ignored_writes']),
                         ('halt_or_sleep',0,1))
        self.assertEqual(result['panel_cells'], [32]*40)
        result = self.execute(program, profile='lab-board-startup', instructions=300000)
        self.assertEqual(result['stop_reason'], 'unsupported_external_io_write')
        self.assertEqual(result['panel_profile'], 'unconnected')

    def test_unresolved_input_and_handshake_modes_are_guarded(self):
        for program in ([0xed,0x38,0x40], [0x3e,0x81,0xed,0x39,0x43,0xed,0x38,0x42],
                        [0xed,0x38,0x80]):
            with self.subTest(program=program):
                self.assertEqual(self.execute(program, profile='lab-board-startup')['stop_reason'], 'unresolved_board_input')
        for mode in (0x84,0xa0,0xc0,0xe4):
            with self.subTest(mode=mode):
                result = self.execute([0x3e,mode,0xed,0x39,0x43], profile='lab-board-startup')
                self.assertEqual(result['stop_reason'], 'unsupported_ppi_mode')
                self.assertEqual(result['board_writes'], 0)

    # Original synthetic programs speaking the telephony handshake from the
    # control board's side: release reset, wait for the request on E0.6,
    # arm the CSI/O, strobe latch 60 bit 7, wait for EF.
    RELEASE = [0x3e,0xa0,0xed,0x39,0x60]                    # reset high, strobe high
    WAIT_REQUEST = [0xed,0x38,0xe0,0xcb,0x77,0x20,0xf9]     # IN0 A,(E0); BIT 6,A; JR NZ
    STROBE = [0x3e,0x20,0xed,0x39,0x60,0x3e,0xa0,0xed,0x39,0x60]
    WAIT_EF = [0xed,0x38,0x0a,0xcb,0x7f,0x28,0xf9]          # IN0 A,(CNTR); BIT 7,A; JR Z

    def receive_byte(self):
        return ([0x3e,0x27,0xed,0x39,0x0a] + self.STROBE + self.WAIT_EF
                + [0xed,0x38,0x0b])                         # IN0 A,(TRDR)

    def send_byte(self, byte):
        # Reading TRDR afterwards clears EF, as the firmware's own ISR does.
        return ([0x3e,0x17,0xed,0x39,0x0a,0x3e,byte,0xed,0x39,0x0b] + self.STROBE + self.WAIT_EF
                + [0xed,0x38,0x0b])

    def expect_a(self, value):
        return [0xfe,value,0x20,0xfe]                       # CP n; JR NZ,$ (spin to budget)

    def telephony(self, program, **fixtures):
        return self.execute(program + [0x76], **dict(dict(
            profile='lab-board-startup', instructions=2000000, status_e0_fixture=0x40,
            int2_period_clocks=61440, telephony_profile='standin'), **fixtures))

    def test_telephony_stand_in_announces_after_reset_and_answers_queries(self):
        program = (self.RELEASE + self.WAIT_REQUEST + self.receive_byte() + self.expect_a(0x70)
                   + self.send_byte(0x33) + self.WAIT_REQUEST + self.receive_byte() + self.expect_a(0x7a)
                   + self.send_byte(0x38))
        for expected in (0xc4, 0x04, 0x00, 0xc8):
            program += self.receive_byte() + self.expect_a(expected)
        result = self.telephony(program)
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['telephony_announces'], result['telephony_replies'],
                          result['telephony_keepalives']), (1, 6, 1))
        self.assertEqual(result['telephony_command_log'], [0x33, 0x38])
        # It announces only once reset is released, and not at once.
        self.assertEqual(self.telephony([0x3e,0x80,0xed,0x39,0x60] + self.WAIT_REQUEST)['stop_reason'], 'budget')

    def test_telephony_frames_are_checked_and_reset_drops_the_queue(self):
        # A configuration frame whose last byte is the sum of its data: 74.
        frame = [0xc0, 6, 0x78, 0x00, 0x03, 0x7b]
        program = self.RELEASE + self.WAIT_REQUEST + self.receive_byte()
        for byte in frame:
            program += self.send_byte(byte)
        program += self.WAIT_REQUEST + self.receive_byte() + self.expect_a(0x74)
        result = self.telephony(program)
        self.assertEqual((result['stop_reason'], result['telephony_frames'],
                          result['telephony_frame_errors']), ('halt_or_sleep', 1, 0))
        bad = self.RELEASE + self.WAIT_REQUEST + self.receive_byte()
        for byte in frame[:-1] + [0x7c]:
            bad += self.send_byte(byte)
        result = self.telephony(bad)
        self.assertEqual((result['telephony_frames'], result['telephony_frame_errors']), (0, 1))
        # Pulling reset while 70 waits withdraws the request.
        program = (self.RELEASE + self.WAIT_REQUEST + [0x3e,0x80,0xed,0x39,0x60]
                   + [0xed,0x38,0xe0,0xcb,0x77,0x28,0xfe])   # E0.6 must read high again
        result = self.telephony(program)
        self.assertEqual((result['stop_reason'], result['telephony_resets']), ('halt_or_sleep', 1))

    def test_keys_and_the_handset_arrive_as_telephony_opcodes(self):
        # 70 first, then the scheduled key and lift; `30` then reports off-hook.
        program = self.RELEASE + self.WAIT_REQUEST + self.receive_byte() + self.expect_a(0x70)
        for expected in (0x38, 0x34, 0x22, 0x62):
            program += self.WAIT_REQUEST + self.receive_byte() + self.expect_a(expected)
        program += self.send_byte(0x30) + self.WAIT_REQUEST + self.receive_byte() + self.expect_a(0x6e)
        result = self.telephony(program, instructions=4000000,
                                events=((300, 'k', '#'), (310, 'k', '0'), (320, 'k', '1'), (330, 'h', 1)))
        self.assertEqual((result['stop_reason'], result['events_applied']), ('halt_or_sleep', 4))

    def test_a_contact_event_changes_port_80(self):
        program = [0xed,0x38,0x80,0xfe,0xd0,0x20,0xf9]          # IN0 A,(80); CP D0; JR NZ
        result = self.telephony(program, status80_contacts=15, events=((50, 's', 13),))
        self.assertEqual((result['stop_reason'], result['events_applied']), ('halt_or_sleep', 1))
        for bad in (((0, 's', 16),), ((0, 'k', '+'),), ((0, 'k', 1),), ((5, 'h', 1), (4, 'h', 0))):
            with self.subTest(events=bad):
                with self.assertRaises(diagnostic.DiagnosticError):
                    self.telephony([0x76], status80_contacts=15, events=bad)
        with self.assertRaises(diagnostic.DiagnosticError):
            self.telephony([0x76], events=((0, 's', 13),))     # no contact fixture to change

    def test_telephony_stand_in_is_opt_in_and_needs_its_fixtures(self):
        for fixtures in ({'status_e0_fixture': 0x40}, {'int2_period_clocks': 61440}, {}):
            with self.subTest(fixtures=fixtures):
                with self.assertRaises(diagnostic.DiagnosticError):
                    self.execute([0x76], profile='lab-board-startup', telephony_profile='standin', **fixtures)
        with self.assertRaises(diagnostic.DiagnosticError):
            self.execute([0x76], profile='rom-only', telephony_profile='standin',
                         status_e0_fixture=0x40, int2_period_clocks=61440)
        with self.assertRaises(diagnostic.DiagnosticError):
            self.execute([0x76], profile='lab-board-startup', events=((0, 'h', 1),))
        result = self.execute(self.RELEASE + [0xed,0x38,0xe0,0x76], profile='lab-board-startup',
                              status_e0_fixture=0x40)
        self.assertEqual(result['telephony_profile'], 'unconnected')

    # Original synthetic ASCI0 programs, playing the coin task's side.
    ASCI0_ON = [0x3e,0x0c,0xed,0x39,0x02,0x3e,0x64,0xed,0x39,0x00]   # CNTLB0, CNTLA0 8N1 RE TE

    def asci_send(self, byte):
        return [0xed,0x38,0x04,0xcb,0x4f,0x28,0xf9,0x3e,byte,0xed,0x39,0x06]   # wait TDRE; TDR0

    def asci_expect(self, byte):
        return ([0xed,0x38,0x04,0xcb,0x7f,0x28,0xf9,0xed,0x38,0x08]          # wait RDRF; RDR0
                + self.expect_a(byte))

    def coin(self, program, **fixtures):
        return self.execute(program + [0x76], profile='lab-board-startup', instructions=2000000,
                            status_e0_fixture=0x40, int2_period_clocks=61440, asci0_pin_fixture=0,
                            telephony_profile='standin', coin_profile='standin', **fixtures)

    def test_coin_stand_in_answers_status_and_queries(self):
        program = (self.ASCI0_ON + self.asci_send(ord('r')) + self.asci_expect(ord('B'))
                   + self.asci_expect(0) + self.asci_expect(0))
        for byte in (ord('q'), 1, 0xfc, ord('d'), ord('q')):   # d's argument is 'q', not a query
            program += self.asci_send(byte)
        program += self.asci_expect(0)
        result = self.coin(program)
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['coin_status_replies'], result['coin_query_replies'],
                          result['coin_bytes_sent']), (1, 1, 4))
        # The last byte may still be on the line when the program halts.
        self.assertEqual(result['coin_command_log'][:5], [ord('r'), ord('q'), 1, 0xfc, ord('d')])

    def test_a_coin_is_its_channel_letter(self):
        program = self.ASCI0_ON + self.asci_expect(ord('3')) + self.asci_expect(ord('g'))
        result = self.coin(program, events=((20, 'c', 3), (30, 'c', 16)))
        self.assertEqual((result['stop_reason'], result['coin_inserted'], result['events_applied']),
                         ('halt_or_sleep', 2, 2))

    def test_coin_stand_in_says_hello_to_g_and_puts_any_byte_on_the_line(self):
        # INSTALL's coin step sends g and wants A (09:6B55); E is an EEPROM
        # fault (66) and silence no mechanism (61).
        program = self.ASCI0_ON + self.asci_send(ord('g')) + self.asci_expect(ord('A'))
        result = self.coin(program)
        self.assertEqual((result['stop_reason'], result['coin_hello_replies']), ('halt_or_sleep', 1))
        result = self.coin(self.ASCI0_ON + self.asci_send(ord('g')) + self.asci_expect(ord('E')),
                           coin_hello='E')
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        result = self.coin(self.ASCI0_ON + self.asci_send(ord('g')) + self.DELAY, coin_hello=None)
        self.assertEqual((result['coin_hello_replies'], result['coin_bytes_sent']), (0, 0))
        # Nothing to l or k either: calibration waits on silence.
        result = self.coin(self.ASCI0_ON + self.asci_send(ord('l')) + self.asci_send(ord('k')) + self.DELAY)
        self.assertEqual(result['coin_bytes_sent'], 0)
        # And from l to the next g it is deaf to r: the step forwards every
        # B status to the craft task, which fails 61 on it (09:6CB7).
        result = self.coin(self.ASCI0_ON + self.asci_send(ord('l')) + self.asci_send(ord('r'))
                           + self.asci_send(ord('k')) + self.asci_send(ord('r')) + self.DELAY)
        self.assertEqual((result['coin_status_replies'], result['coin_bytes_sent']), (0, 0))
        result = self.coin(self.ASCI0_ON + self.asci_send(ord('l')) + self.asci_send(ord('g'))
                           + self.asci_expect(ord('A')) + self.DELAY + self.asci_send(ord('r'))
                           + self.asci_expect(ord('B')))
        self.assertEqual((result['stop_reason'], result['coin_status_replies']), ('halt_or_sleep', 1))
        # r goes unanswered for 100 ms after g (09:6BD0 fails on a B as
        # well), and g drops a reply not yet on the line but finishes one
        # already started: here the B of the first poll had begun, so
        # B 00 00 then A, and nothing for the second poll.
        result = self.coin(self.ASCI0_ON + self.asci_send(ord('r')) + self.asci_send(ord('g'))
                           + self.asci_send(ord('r')) + self.asci_expect(ord('B')) + self.asci_expect(0)
                           + self.asci_expect(0) + self.asci_expect(ord('A')) + self.DELAY)
        self.assertEqual((result['stop_reason'], result['coin_status_replies'], result['coin_bytes_sent']),
                         ('halt_or_sleep', 1, 4))
        result = self.coin(self.ASCI0_ON + self.asci_send(ord('d')) + self.asci_send(ord('1'))
                           + self.asci_send(ord('r')) + self.asci_send(ord('q')) + self.asci_send(1)
                           + self.asci_send(0xfc) + self.asci_send(ord('g')) + self.asci_expect(ord('B'))
                           + self.asci_expect(0) + self.asci_expect(0) + self.asci_expect(ord('A')) + self.DELAY)
        self.assertEqual((result['stop_reason'], result['coin_query_replies'], result['coin_bytes_sent']),
                         ('halt_or_sleep', 1, 4))         # the query's byte never went
        # A fault letter, as the mechanism would send it mid-calibration.
        result = self.coin(self.ASCI0_ON + self.asci_expect(ord('D')), events=((20, 'b', 0x44),))
        self.assertEqual((result['stop_reason'], result['coin_raw_bytes']), ('halt_or_sleep', 1))
        with self.assertRaises(diagnostic.DiagnosticError):
            self.telephony([0x76], events=((0, 'b', 0x44),))       # no mechanism to send it
        with self.assertRaises(diagnostic.DiagnosticError):
            self.telephony([0x76], coin_hello='E')

    def test_every_key_on_the_set_arrives_as_its_opcode(self):
        # The keypad step's echo table (09:567D) names 26 keys; each one is
        # the opcode the telephony board sends, named keys included.
        names = list(diagnostic.KEY_OPCODES)
        self.assertEqual(len(names), 26)
        program = self.RELEASE + self.WAIT_REQUEST + self.receive_byte() + self.expect_a(0x70)
        for name in names:
            program += self.WAIT_REQUEST + self.receive_byte() + self.expect_a(diagnostic.KEY_OPCODES[name])
        result = self.telephony(program, instructions=8000000,
                                events=tuple((300 + 10 * i, 'k', name) for i, name in enumerate(names)))
        self.assertEqual((result['stop_reason'], result['events_applied']), ('halt_or_sleep', 26))

    # An original program that sets DMA channel 0 up exactly as the card
    # driver does (24:579B, 24:58BF): SAR0 = I/O port 80, DAR0 = RAM, BCR0,
    # DMODE 0C, DCNTL F4, DSTAT 64. Then it waits about 640 ms, stores
    # BCR0L at F100 and halts. DMA0's end interrupt (I = 01, vector 0108)
    # marks F101 and turns DIE0 off.
    def card_program(self, count, interrupts):
        program = [0x3e,0x38,0xed,0x39,0x38, 0x31,0xf0,0xff, 0x3e,0x01,0xed,0x47,
                   0x3e,0x80,0xed,0x39,0x20, 0xaf,0xed,0x39,0x21, 0xed,0x39,0x22, 0xed,0x39,0x23,
                   0x3e,0x70,0xed,0x39,0x24, 0x3e,0x04,0xed,0x39,0x25,
                   0x3e,count,0xed,0x39,0x26, 0xaf,0xed,0x39,0x27,
                   0x3e,0x0c,0xed,0x39,0x31, 0x3e,0xf4,0xed,0x39,0x32, 0x3e,0x64,0xed,0x39,0x30,
                   0xfb if interrupts else 0x00,
                   0x16,0x03, 0x01,0x00,0x00, 0x0b,0x78,0xb1,0x20,0xfb, 0x15,0x20,0xf5,
                   0xed,0x38,0x26, 0x32,0x00,0xf1, 0x76]
        program += [0] * (0x108 - len(program)) + [0x00,0x02]
        program += [0] * (0x200 - len(program))
        return program + [0x3e,0x01,0x32,0x01,0xf1, 0x3e,0x30,0xed,0x39,0x30, 0xfb,0xed,0x4d]

    @staticmethod
    def track2(body):
        # ABA track 2 from the standard, written independently of the
        # worker: 5-bit characters, LSB first, odd parity, ; = ? and LRC.
        codes = [0x0b] + [0x0d if c == '=' else int(c) for c in body] + [0x0f]
        lrc = 0
        for code in codes:
            lrc ^= code
        bits = [0] * 12
        for code in codes + [lrc]:
            data = [code >> i & 1 for i in range(4)]
            bits += data + [1 - sum(data) % 2]
        return bits + [0] * 12

    PAN = '4761739001010010=25122011143804400000'

    def card_run(self, count, interrupts, cards=None, events=((1, 'w', 0),)):
        return self.execute(self.card_program(count, interrupts), profile='lab-board-startup',
                            instructions=3000000, status_e0_fixture=0x40, int2_period_clocks=61440,
                            telephony_profile='standin', status80_contacts=15,
                            cards=cards or (self.PAN,), events=events,
                            peek=(0xf00c, 0xf00d, 0xf00e, 0xf00f, 0xf010, 0xf011, 0xf100, 0xf101))

    def test_a_card_reader_answers_the_chip_probe_with_no_chip(self):
        # 24:71D5 reads PPI port C bit 2 eight times a byte; with no chip
        # the I/O contact reads high, and port C's output half is the latch.
        program = [0x3e,0x38,0xed,0x39,0x38, 0x3e,0x81,0xd3,0x43, 0x3e,0xc0,0xd3,0x42,
                   0xed,0x38,0x42, 0x32,0x00,0xf1, 0x76]
        base = dict(profile='lab-board-startup', instructions=200000, status_e0_fixture=0x40,
                    int2_period_clocks=61440, telephony_profile='standin', status80_contacts=15,
                    peek=(0xf100,))
        result = self.execute(program, cards=(self.PAN,), **base)
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['ram_peek'], result['card_chip_reads']), ([[0xf100, 0xc4]], 1))
        # Without the reader, port C's input half is still an unresolved net.
        result = self.execute(program, **base)
        self.assertEqual((result['stop_reason'], result['fault_address'] & 0xff), ('unresolved_board_input', 0x42))

    def test_speech_sink_takes_port_c0_only_when_asked(self):
        # The voice-prompt player writes ADPCM bytes to C0 (00:6AC1).
        program = [0x3e,0x38,0xed,0x39,0x38, 0x3e,0x12,0xed,0x39,0xc0, 0x3e,0x34,0xd3,0xc0, 0x76]
        base = dict(profile='lab-board-startup', instructions=200000, status_e0_fixture=0x40,
                    int2_period_clocks=61440, telephony_profile='standin', status80_contacts=15)
        result = self.execute(program, speech_profile='silent', **base)
        self.assertEqual((result['stop_reason'], result['speech_writes']), ('halt_or_sleep', 2))
        result = self.execute(program, **base)
        self.assertEqual((result['stop_reason'], result['fault_address'] & 0xff),
                         ('unsupported_external_io_write', 0xc0))
        with self.assertRaises(diagnostic.DiagnosticError):
            self.execute(program, profile='lab-board-startup', speech_profile='silent')
        with self.assertRaises(diagnostic.DiagnosticError):
            self.execute(program, speech_profile='loud', **base)

    def test_a_card_going_in_is_read_by_dma0_one_bit_per_strobe(self):
        bits = self.track2(self.PAN)
        self.assertEqual(len(bits), 224)
        result = self.card_run(255, False)
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        peeked = dict(result['ram_peek'])
        # Port 80 as stored: contacts 15 in bits 7..4, the slot sensor
        # (bit 1), not yet seated (bit 2), the data bit (bit 0).
        self.assertEqual([peeked[0xf00c + i] for i in range(6)], [0xf2 | bit for bit in bits[12:18]])
        self.assertEqual(peeked[0xf100], 255 - len(bits))               # BCR0 counted every byte
        self.assertEqual((result['card_swipes'], result['card_strobes'], result['card_latched']),
                         (1, len(bits), len(bits)))
        self.assertEqual((result['dma0_interrupts'], peeked[0xf101]), (0, 0))

    def test_dma0_ends_the_block_with_its_interrupt(self):
        result = self.card_run(16, True)
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        peeked = dict(result['ram_peek'])
        self.assertEqual((peeked[0xf100], peeked[0xf101], result['dma0_interrupts']), (0, 1, 1))
        # After the count ran out the reader kept strobing into a disabled
        # channel, which takes nothing.
        self.assertEqual((result['card_latched'], result['card_lost']), (16, 224 - 16))
        self.assertEqual(result['dstat'] & 0x44, 0)

    def test_card_reader_is_opt_in_and_checked(self):
        for cards in (('12a',), ('',), ('1' * 38,), (('sideways', '1'),), ('1',) * 9):
            with self.subTest(cards=cards):
                with self.assertRaises(diagnostic.DiagnosticError):
                    self.card_run(255, False, cards=cards)
        with self.assertRaises(diagnostic.DiagnosticError):
            self.card_run(255, False, events=((1, 'w', 1),))       # no second card
        with self.assertRaises(diagnostic.DiagnosticError):         # no contacts fixture
            self.execute([0x76], profile='lab-board-startup', status_e0_fixture=0x40,
                         int2_period_clocks=61440, telephony_profile='standin', cards=(self.PAN,))
        # Without the reader, the driver's DSTAT write still stops.
        result = self.execute(self.card_program(255, False), profile='lab-board-startup',
                              instructions=3000000, status_e0_fixture=0x40, int2_period_clocks=61440,
                              telephony_profile='standin', status80_contacts=15)
        self.assertEqual((result['stop_reason'], result['fault_address']),
                         ('unsupported_dma_configuration', 0x30))

    def test_panel_brightness_is_a_level_byte_after_1a(self):
        # Menu 274 (09:5EDD) sends data 1A, then (level - 1) * 16.
        result = self.execute(self.panel_program([0x41, 0x1a, 0x10, 0x42]) + [0x76],
            profile='lab-board-startup', instructions=400000, panel_profile='cu20026-t23a-lab')
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['panel_brightness'], result['panel_cells'][:3], result['panel_cursor']),
                         (0x10, [0x41, 0x42, 32], 2))
        result = self.execute(self.panel_program([0x1a, 0x15]) + [0x76],
            profile='lab-board-startup', instructions=400000, panel_profile='cu20026-t23a-lab')
        self.assertEqual((result['stop_reason'], result['panel_brightness']), ('unsupported_panel_protocol', -1))

    def test_panel_modes_15_and_16_leave_the_cells_alone(self):
        # The display-mode routine (00:710F): 15 alone (the INSTALL keypad
        # step), 16 and one cell 0..39 (06:5536..5565).
        result = self.execute(self.panel_program([0x41, 0x15, 0x16, 0x27, 0x17, 0x42]) + [0x76],
            profile='lab-board-startup', instructions=600000, panel_profile='cu20026-t23a-lab')
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['panel_cells'][:3], result['panel_cursor'], result['panel_pending_bytes']),
                         ([0x41, 0x42, 32], 2, 0))
        result = self.execute(self.panel_program([0x16, 0x28]) + [0x76],
            profile='lab-board-startup', instructions=400000, panel_profile='cu20026-t23a-lab')
        self.assertEqual((result['stop_reason'], result['panel_writes']), ('unsupported_panel_protocol', 1))

    def test_coin_stand_in_needs_the_line_it_answers_on(self):
        base = dict(profile='lab-board-startup', status_e0_fixture=0x40, int2_period_clocks=61440,
                    telephony_profile='standin', coin_profile='standin')
        for pins in (None, 2, 3):
            with self.subTest(pins=pins):
                with self.assertRaises(diagnostic.DiagnosticError):
                    self.execute([0x76], asci0_pin_fixture=pins, **base)
        with self.assertRaises(diagnostic.DiagnosticError):
            self.execute([0x76], profile='lab-board-startup', status_e0_fixture=0x40,
                         int2_period_clocks=61440, telephony_profile='standin', asci0_pin_fixture=0,
                         events=((0, 'c', 1),))
        # Without the stand-in, RDR0 is still an unsupported access.
        result = self.execute(self.ASCI0_ON + [0xed,0x38,0x08], profile='lab-board-startup',
                              asci0_pin_fixture=0)
        self.assertEqual(result['stop_reason'], 'unsupported_asci_data_access')

    # Original synthetic programs driving the 73K212's serial control bus the
    # way the firmware's register routines do: PA0 data (inverted), PA1
    # EXCLK, PA2 RD, PA3 WR, PA4-5 the register; read data on E0 bit 4.
    PPI_OUTPUTS = [0x3e,0x80,0xd3,0x43]
    DELAY = [0x11,0xff,0xff,0x1b,0x7a,0xb3,0x20,0xfb]       # about 200 ms

    @staticmethod
    def port_a(value):
        return [0x3e,value,0xd3,0x40]

    def modem_write(self, register, value):
        base = 0x0c | register << 4
        program = []
        for bit in range(8):
            line = base | (0 if value >> bit & 1 else 1)
            program += self.port_a(line | 2) + self.port_a(line)    # taken on the fall
        # WR's own rising EXCLK is not a ninth bit.
        return program + self.port_a((line & ~8) | 2) + self.port_a(line | 2 | 8)

    def modem_read(self, register, expected):
        base = 0x08 | register << 4                              # RD low
        program = self.port_a(base | 4)
        for bit in range(8):
            program += self.port_a(base | 2) + [0xed,0x38,0xe0,0xe6,0x10] + self.expect_a(
                0x10 if expected >> bit & 1 else 0) + self.port_a(base)
        return program + self.port_a(base | 4 | 2)

    def modem(self, program, **fixtures):
        return self.execute(self.PPI_OUTPUTS + program + [0x76], **dict(dict(
            profile='lab-board-startup', instructions=2000000, status_e0_fixture=0x40,
            int2_period_clocks=61440, telephony_profile='standin', modem_profile='control'), **fixtures))

    def test_modem_control_bus_writes_reads_and_detect_events(self):
        program = (self.modem_write(3, 0x96) + self.modem_write(0, 0x1b) + self.modem_write(2, 0x55)
                   + self.modem_read(3, 0x96) + self.modem_read(2, 0) + self.DELAY
                   + self.modem_read(2, 0x2a))
        result = self.modem(program, events=((20, 'm', 0x2a),))
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual(result['modem_write_log'], [0x396, 0x01b, 0x255])   # DR is read-only
        self.assertEqual(result['modem_registers'], [0x1b, 0, 0x2a, 0x96])
        self.assertEqual((result['modem_writes'], result['modem_reads'], result['modem_resets']), (3, 3, 0))
        # CR1 bit 2 is the reset command: CR0, CR1 and TR go back to zero.
        result = self.modem(self.modem_write(3, 0x96) + self.modem_write(1, 0x04) + self.modem_read(3, 0))
        self.assertEqual((result['stop_reason'], result['modem_resets']), ('halt_or_sleep', 1))

    def test_modem_owns_asci1_and_records_what_it_is_sent(self):
        send = lambda byte: [0xed,0x38,0x05,0xcb,0x4f,0x28,0xf9,0x3e,byte,0xed,0x39,0x07]
        program = [0x3e,0x0c,0xed,0x39,0x03,0x3e,0x64,0xed,0x39,0x01] + send(0x02) + send(0x03) + self.DELAY
        result = self.modem(program)
        self.assertEqual((result['stop_reason'], result['modem_transmitted_bytes'],
                          result['modem_transmit_log']), ('halt_or_sleep', 2, [0x02, 0x03]))
        # Without it, ASCI1's status is still a pin nobody has verified.
        result = self.telephony([0x3e,0x0c,0xed,0x39,0x03,0x3e,0x64,0xed,0x39,0x01] + send(0x02))
        self.assertEqual(result['stop_reason'], 'unsupported_asci_pin_read')

    def test_streamed_asci1_reaches_a_live_far_end_and_back(self):
        # The firmware's side of a call: send STX, wait for a byte, send it
        # back plus one, then read the detect register the far end set when
        # the first byte arrived. Nothing in the run is scheduled: every
        # reply comes from the far end while the worker runs.
        program = ([0x3e,0x0c,0xed,0x39,0x03,0x3e,0x64,0xed,0x39,0x01] + self.modem_write(3, 0x91)
                   + [0x3e,0x02, 0x47,0xed,0x38,0x05,0xcb,0x4f,0x28,0xf9,0x78,0xed,0x39,0x07,
                      0xed,0x38,0x05,0xcb,0x7f,0x28,0xf9, 0xed,0x38,0x09, 0x3c,
                      0x47,0xed,0x38,0x05,0xcb,0x4f,0x28,0xf9,0x78,0xed,0x39,0x07]
                   + self.DELAY + self.modem_read(2, 0x3e))

        class Echo(object):
            def __init__(self):
                self.seen = []

            def on_message(self, link, tag, payload):
                self.seen.append((tag, bytes(payload)))
                if (tag, payload) == ('D', b'\x02'):
                    link.detect(0x3e)
                    link.receive(b'\x41')

        far_end = Echo()
        started = time.monotonic()
        result = self.modem(program, far_end=far_end)
        elapsed = time.monotonic() - started
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual(result['modem_transmit_log'], [0x02, 0x42])
        self.assertEqual((result['stream'], result['stream_rx_bytes'], result['stream_overruns'],
                          result['stream_messages_in']), (1, 1, 0, 2))
        self.assertEqual([m for m in far_end.seen if m[0] in 'DW'],
                         [('W', b'\x03\x91'), ('D', b'\x02'), ('D', b'\x42')])
        # Paced: never ahead of the wall clock.
        self.assertGreaterEqual(elapsed, result['modeled_clocks'] / 6144000 - 0.01)

    def test_streamed_run_ends_when_the_far_end_finishes(self):
        class Quit(object):
            def on_message(self, link, tag, payload):
                pass

            def poll(self, link):
                if not getattr(self, 'done', False):
                    self.done = True
                    link.finish()

        result = self.modem([0x18,0xfe], far_end=Quit())
        self.assertEqual((result['stop_reason'], result['stream']), ('stream_closed', 1))
        # And a batch run reports that it never streamed.
        result = self.modem([])
        self.assertEqual((result['stream'], result['stream_rx_bytes']), (0, 0))

    def test_modem_stand_in_is_opt_in_and_needs_the_telephony_stand_in(self):
        with self.assertRaises(diagnostic.DiagnosticError):
            self.execute([0x76], profile='lab-board-startup', status_e0_fixture=0x40,
                         modem_profile='control')
        with self.assertRaises(diagnostic.DiagnosticError):
            self.telephony([0x76], events=((0, 'm', 2),))
        with self.assertRaises(diagnostic.DiagnosticError):
            self.modem([], events=((0, 'm', 256),))
        self.assertEqual(diagnostic.parse_event('60000:m3e'), (60000, 'm', 0x3e))
        # Without the stand-in port A drives nothing and the report says so.
        result = self.telephony(self.PPI_OUTPUTS + self.modem_write(3, 0x96))
        self.assertEqual((result['modem_profile'], result['modem_writes']), ('unconnected', 0))

    def test_ram_peek_reads_the_mapped_byte(self):
        program = [0x3e,0x5a,0x32,0x27,0xf1,0x76]               # LD (F127),A with the reset MMU
        result = self.execute(program, profile='lab-board-startup', instructions=100,
                              status_e0_fixture=0x40, int2_period_clocks=61440,
                              telephony_profile='standin', peek=(0xf127, 0x0000))
        self.assertEqual(result['ram_peek'], [[0xf127, -1], [0, -1]])

    def test_power_on_port_b_reads_zero_only_before_any_mode_word(self):
        # MTR 1.7 pulses PB7 before configuring the PPI. Before the first
        # mode word port B reads zero; afterwards an input port is a board
        # net again, and ports A and C never get the allowance.
        pulse = [0xed,0x38,0x41,0xf6,0x80,0xed,0x39,0x41,0xe6,0x7f,0xed,0x39,0x41]
        result = self.execute(pulse + [0xfe,0,0x20,0xfe,0x76], profile='lab-board-startup')
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        configured = [0x3e,0x9b,0xed,0x39,0x43] + pulse
        self.assertEqual(self.execute(configured, profile='lab-board-startup')['stop_reason'],
                         'unresolved_board_input')
        for port in (0x40, 0x42):
            self.assertEqual(self.execute([0xed,0x38,port], profile='lab-board-startup')['stop_reason'],
                             'unresolved_board_input')

    def test_contact_fixtures_and_fresh_erased_eeprom(self):
        for contacts in range(16):
            program = [0xed,0x38,0x80,0xfe,contacts << 4,0x20,0xfe,0x76]
            result = self.execute(program, profile='lab-board-startup', status80_contacts=contacts)
            self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        program = [0x3e,0x80,0xed,0x39,0x43,0x3e,0x40,0xed,0x39,0x60]
        for bit in '110101011':  # Original synthetic READ, address 43.
            program += [0x3e,int(bit),0xed,0x39,0x40,
                        0x3e,1,0xed,0x39,0x41,0xaf,0xed,0x39,0x41]
        for expected in [0] + [8] * 16:
            program += [0xed,0x38,0x80,0xfe,expected,0x20,0xfe,
                        0x3e,1,0xed,0x39,0x41,0xaf,0xed,0x39,0x41]
        program += [0x76]
        for _ in range(2):
            result = self.execute(program, profile='lab-board-startup', instructions=500)
            self.assertEqual((result['stop_reason'], result['eeprom_reads']), ('halt_or_sleep',1))

    def test_startup_configuration_has_no_serial_or_dma_responses(self):
        program = [0x3e,8,0xed,0x39,4,0x3e,0x64,0xed,0x39,0,
                   0x3e,0x0c,0xed,0x39,2,0x3e,4,0xed,0x39,0x34,0x76]
        self.assertEqual(self.execute(program, profile='lab-board-startup')['stop_reason'], 'halt_or_sleep')
        for program, reason in [([0x3e,1,0xed,0x39,4], 'unsupported_asci_interrupt_enable'),
                                ([0xed,0x38,4], 'unsupported_asci_pin_read'),
                                ([0x3e,0x55,0xed,0x39,6], 'unsupported_asci_data_access'),
                                ([0xed,0x38,8], 'unsupported_asci_data_access'),
                                ([0x3e,0x80,0xed,0x39,0x30], 'unsupported_dma_configuration'),
                                ([0x3e,0x14,0xed,0x39,0x10], 'unsupported_timer_configuration'),
                                ([0x3e,0x10,0xed,0x39,0x0a], 'unsupported_csio_transfer')]:
            with self.subTest(program=program):
                self.assertEqual(self.execute(program, profile='lab-board-startup')['stop_reason'], reason)

    def test_stopped_csio_data_readback_is_quiet(self):
        for value in (0,0x5a,0xff):
            result = self.execute([0x3e,value,0xed,0x39,0x0b,0xed,0x38,0x0b,
                                   0xfe,value,0x20,0xfe,0x76])
            self.assertEqual(result['stop_reason'], 'halt_or_sleep')

    def test_startup_timer_interrupt_executes_through_provisional_ram_stack(self):
        program = [0x3e,0x85,0xed,0x39,0x3a,0x3e,0x38,0xed,0x39,0x38,
                   0x31,0xf0,0xff,0x3e,1,0xed,0x47,
                   0x3e,1,0xed,0x39,0x0c,0xaf,0xed,0x39,0x0d,
                   0x3e,0x11,0xed,0x39,0x10,0xfb,0,0x18,0xfe]
        program += [0] * (0x104 - len(program)) + [0,2]
        program += [0] * (0x200 - len(program)) + [0xaf,0xed,0x39,0x10,0x76]
        result = self.execute(program, profile='lab-board-startup')
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['last_instruction_pc'], result['timer0_interrupts']), (0x204,1))
        self.assertEqual(result['timer1_interrupts'], 0)

    def test_asci_fixture_status_bits_prescaler_and_no_receive_producer(self):
        for pins in range(4):
            expected_status = (0 if pins & 1 else 2) | (4 if pins & 2 else 0)
            program = [0xed,0x38,4,0xfe,expected_status,0x20,0xfe,
                       0x3e,0x21,0xed,0x39,2,0xed,0x38,2,0xfe,0x21 if pins & 1 else 1,
                       0x20,0xfe,0x76]
            result = self.execute(program, profile='lab-board-startup', asci0_pin_fixture=pins)
            self.assertEqual(result['stop_reason'], 'halt_or_sleep')
            self.assertEqual((result['asci0_transmitted_bytes'],result['asci0_interrupts']), (0,0))
            self.assertEqual(self.execute([0xed,0x38,8], profile='lab-board-startup',
                asci0_pin_fixture=pins)['stop_reason'], 'unsupported_asci_data_access')
        self.assertEqual(self.execute([0xed,0x38,4], profile='lab-board-startup')['stop_reason'],
                         'unsupported_asci_pin_read')
        self.assertEqual(self.execute([0xed,0x38,3], profile='lab-board-startup',
            asci0_pin_fixture=0)['stop_reason'], 'unsupported_asci_pin_read')

    def test_asci_transmit_monitor_obeys_cts_modes_and_frame_deadline(self):
        program = [0xaf,0xed,0x39,2,0x3e,0x24,0xed,0x39,0,
                   0x3e,0xa7,0xed,0x39,6,0x18,0xfe]
        for pins in (0,2):
            early = self.execute(program, profile='lab-board-startup', instructions=20,
                                 asci0_pin_fixture=pins)
            self.assertEqual((early['stop_reason'],early['asci0_transmitted_bytes']), ('budget',0))
            result = self.execute(program, profile='lab-board-startup', instructions=1000,
                                  asci0_pin_fixture=pins)
            self.assertEqual((result['stop_reason'],result['asci0_transmitted_bytes'],
                              result['asci0_last_transmitted_byte']), ('budget',1,0xa7))
            self.assertEqual(result['asci0_interrupts'], 0)
        for pins in (1,3):
            self.assertEqual(self.execute(program, profile='lab-board-startup',
                asci0_pin_fixture=pins)['stop_reason'], 'unsupported_asci_data_timing')
        for cntla, cntlb in ((4,0),(0x20,0),(0xa4,0),(0x24,0x40),(0x24,7)):
            guarded = [0x3e,cntlb,0xed,0x39,2,0x3e,cntla,0xed,0x39,0,0xaf,0xed,0x39,6]
            self.assertEqual(self.execute(guarded, profile='lab-board-startup',
                asci0_pin_fixture=0)['stop_reason'], 'unsupported_asci_transmit_mode')

    def test_asci_empty_tdr_interrupt_executes_with_ram_stack_and_cancels_tie(self):
        program = [0x3e,0x85,0xed,0x39,0x3a,0x3e,0x38,0xed,0x39,0x38,
                   0x31,0xf0,0xff,0x3e,1,0xed,0x47,
                   0xaf,0xed,0x39,2,0x3e,0x24,0xed,0x39,0,
                   0x3e,1,0xed,0x39,4,0xfb,0,0x18,0xfe]
        program += [0] * (0x10e - len(program)) + [0,2]
        # ISR writes one byte, removes TIE, then EI/RETI to the spin loop.
        program += [0] * (0x200 - len(program)) + [0x3e,0x61,0xed,0x39,6,
                      0xaf,0xed,0x39,4,0xfb,0xed,0x4d]
        for pins, count in ((0,1),(1,0)):
            result = self.execute(program, profile='lab-board-startup', instructions=2000,
                                  asci0_pin_fixture=pins)
            self.assertEqual((result['stop_reason'],result['asci0_interrupts'],
                              result['asci0_transmitted_bytes']), ('budget',count,count))
            if count:
                self.assertEqual((result['sp'],result['asci0_last_transmitted_byte']), (0xfff0,0x61))

    def test_eeprom_programming_completes_on_executed_cpu_clocks(self):
        program = [0x3e,0x80,0xed,0x39,0x43]
        def send(word, bits):
            for bit in f'{word:0{bits}b}':
                program.extend([0x3e,int(bit),0xed,0x39,0x40,
                                0x3e,1,0xed,0x39,0x41,0xaf,0xed,0x39,0x41])
        def command(word):
            program.extend([0xaf,0xed,0x39,0x60,0xaf,0xed,0x39,0x41,
                            0x3e,0x40,0xed,0x39,0x60])
            send(word, 9)
        command(0x130)  # EWEN
        command(0x145)  # WRITE address 5
        send(0x1357, 16)
        program.extend([0xaf,0xed,0x39,0x60,0x01,0,0x20,
                        0x0b,0x78,0xb1,0x20,0xfb])  # Original counted CPU delay.
        command(0x185)  # READ address 5
        for bit in '0' + f'{0x1357:016b}':
            program.extend([0xed,0x38,0x80,0xfe,int(bit) << 3,0x20,0xfe,
                            0x3e,1,0xed,0x39,0x41,0xaf,0xed,0x39,0x41])
        program.append(0x76)
        result = self.execute(program, profile='lab-board-startup', instructions=40000)
        self.assertEqual((result['stop_reason'], result['eeprom_writes'], result['eeprom_reads']),
                         ('halt_or_sleep',1,1))

    def test_explicit_clock_fixture_wakes_only_when_external_interrupt_enabled(self):
        program = [0x3e,0x85,0xed,0x39,0x3a,0x3e,0x38,0xed,0x39,0x38,
                   0x31,0xf0,0xff,0x3e,1,0xed,0x47,
                   0x3e,4,0xed,0x39,0x34,0xfb,0,0x18,0xfe]
        program += [0] * (0x102 - len(program)) + [0,2]
        program += [0] * (0x200 - len(program)) + [0xaf,0xed,0x39,0x34,0x76]
        idle = self.execute(program, profile='lab-board-startup')
        self.assertEqual((idle['stop_reason'], idle['external2_interrupts'], idle['clock_input_events']), ('budget',0,0))
        result = self.execute(program, profile='lab-board-startup', int2_period_clocks=200)
        self.assertEqual((result['stop_reason'], result['last_instruction_pc'], result['external2_interrupts']),
                         ('halt_or_sleep',0x204,1))
        self.assertGreaterEqual(result['clock_input_events'], 1)
        self.assertEqual(result['int2_period_clocks'], 200)
        # Independently suppress ITE2: generated fixture requests cannot vector.
        program[18] = 0
        masked = self.execute(program, profile='lab-board-startup', int2_period_clocks=200)
        self.assertEqual((masked['stop_reason'], masked['external2_interrupts']), ('budget',0))
        self.assertGreater(masked['clock_input_events'], 0)

    def test_reset_and_budget(self):
        result = self.execute([0x18, 0xfe], instructions=100)
        self.assertEqual(result['stop_reason'], 'budget')
        self.assertEqual(result['attempted_instructions'], 100)
        self.assertEqual(len(result['trace_pc']), 64)
        self.assertTrue(result['trace_truncated'])
        self.assertEqual((result['cbr'], result['bbr'], result['cbar'], result['iocr']), (0, 0, 240, 0))

    def test_tail_retains_order_across_wrap_and_fault_or_budget(self):
        for nops in (0, 63, 64, 300):
            with self.subTest(nops=nops):
                result = self.execute([0] * nops + [0xdb,0x80], instructions=500)
                self.assertEqual(result['stop_reason'], 'unsupported_external_io_read')
                self.assertEqual(result['attempted_instructions'], nops + 1)
                first_pc = max(0, nops - 63)
                self.assertEqual(result['trace_tail_first_attempt'], first_pc + 1)
                self.assertEqual(result['trace_tail_pc'], list(range(first_pc, nops + 1)))
                self.assertEqual(result['trace_pc'], list(range(min(64, nops + 1))))
        result = self.execute([0] * 100, instructions=80)
        self.assertEqual(result['stop_reason'], 'budget')
        self.assertEqual(result['trace_tail_pc'], list(range(16,80)))
        self.assertEqual(result['trace_tail_first_attempt'], 17)

    def test_mmu_unknown_ram_and_provisional_ram_readback(self):
        program = [0x3e,0x85,0xed,0x39,0x3a, 0x3e,0x38,0xed,0x39,0x38,
                   0x3e,0x5a,0x32,0x59,0x80, 0xaf,0x3a,0x59,0x80,
                   0xfe,0x5a,0x28,0x02,0x18,0xfe,0x76]
        strict = self.execute(program)
        self.assertEqual(strict['stop_reason'], 'unmapped_memory_write')
        self.assertEqual(strict['fault_address'], 0x40059)
        lab = self.execute(program, profile='lab-ram-40000')
        self.assertEqual(lab['stop_reason'], 'halt_or_sleep')
        self.assertEqual(lab['last_instruction_pc'], 25)
        self.assertFalse(lab['board_verified'])

    def test_unknown_io_and_internal_peripheral_stop_before_response(self):
        external = self.execute([0xdb,0x80])
        self.assertEqual(external['stop_reason'], 'unsupported_external_io_read')
        self.assertEqual(external['fault_address'], 0x80)
        internal = self.execute([0x3e,0x80,0xed,0x39,0x11])
        self.assertEqual(internal['stop_reason'], 'unsupported_internal_io_write')
        self.assertEqual(internal['fault_address'], 0x11)

    def test_iocr_relocation_preserves_internal_guard(self):
        result = self.execute([0x3e,0x40,0xed,0x39,0x3f,0xed,0x00,0x51])
        self.assertEqual(result['iocr'], 0x40)
        self.assertEqual(result['stop_reason'], 'unsupported_internal_io_read')
        self.assertEqual(result['fault_address'], 0x11)

    def test_configuration_reset_and_explicit_timing_boundary(self):
        result = self.execute([0x76])
        self.assertEqual((result['rcr'], result['dcntl'], result['omcr']), (0xc0, 0xf0, 0xe0))
        self.assertFalse(result['peripheral_timing_verified'])

    def test_timer_reset_counter_and_reload_words(self):
        program = []
        for port in (0x0c,0x0d,0x0e,0x0f,0x14,0x15,0x16,0x17):
            program += [0xed,0x38,port,0xfe,0xff,0x20,0xfe]
        result = self.execute(program + [0x76])
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')

    def test_stopped_timer_byte_order_and_no_stale_high_latch(self):
        for low in (0x0c,0x14):
            for high_first in (False, True):
                writes = [(low,0x5a),(low+1,0xa6)]
                if high_first:
                    writes.reverse()
                program = []
                for port, value in writes:
                    program += [0x3e,value,0xed,0x39,port]
                program += [0xed,0x38,low,0xfe,0x5a,0x20,0xfe,
                    0x3e,0xc3,0xed,0x39,low+1,
                    0xed,0x38,low+1,0xfe,0xc3,0x20,0xfe,
                    0xed,0x38,low,0xfe,0x5a,0x20,0xfe,0x76]
                with self.subTest(low=low, high_first=high_first):
                    self.assertEqual(self.execute(program)['stop_reason'], 'halt_or_sleep')

    def test_external_csio_waits_for_clock_instead_of_polling_completion(self):
        for mode in (0x10, 0x20):
            # Firmware-style enable-before-data order, followed by repeated
            # status reads. No external CKS producer exists in this profile.
            program = [0x3e,mode | 0x47,0xed,0x39,0x0a,
                       0x3e,0xa6,0xed,0x39,0x0b,
                       0xed,0x38,0x0a,0xfe,mode | 0x47,0x20,0xfe,0x18,0xf7]
            with self.subTest(mode=mode):
                result = self.execute(program, profile='lab-board-startup', instructions=10000)
                self.assertEqual(result['stop_reason'], 'budget')
                self.assertEqual(result['cntr'], mode | 0x47)
                self.assertGreater(result['modeled_clocks'], 50000)
                self.assertEqual(result['board_reads'], 0)

    def test_csio_external_permissions_are_scoped_and_mux_checked(self):
        for mode in (0x10, 0x20):
            for speed in range(7):
                with self.subTest(mode=mode, speed=speed):
                    result = self.execute([0x3e,mode | speed,0xed,0x39,0x0a],
                                           profile='lab-board-startup')
                    self.assertEqual(result['stop_reason'], 'unsupported_csio_transfer')
                    self.assertEqual(result['cntr'], 7)
            self.assertEqual(self.execute([0x3e,mode | 7,0xed,0x39,0x0a])['stop_reason'],
                             'unsupported_csio_transfer')
        self.assertEqual(self.execute([0x3e,0x37,0xed,0x39,0x0a],
                         profile='lab-board-startup')['stop_reason'], 'unsupported_csio_transfer')
        result = self.execute([0x3e,4,0xed,0x39,5,0x3e,0x27,0xed,0x39,0x0a],
                              profile='lab-board-startup')
        self.assertEqual(result['stop_reason'], 'unsupported_csio_pin_mux')
        self.assertEqual(result['cntr'], 7)
        result = self.execute([0x3e,0x27,0xed,0x39,0x0a,0x3e,4,0xed,0x39,5],
                              profile='lab-board-startup')
        self.assertEqual(result['stop_reason'], 'unsupported_csio_pin_mux')
        self.assertEqual((result['fault_address'],result['cntr']), (5,0x27))
        result = self.execute([0x3e,4,0xed,0x39,1,0x3e,0x27,0xed,0x39,0x0a,0x18,0xfe],
                              profile='lab-board-startup')
        self.assertEqual((result['stop_reason'],result['cntr']), ('budget',0x27))

    def test_e0_status_input_hypothesis_and_full_address_alias_are_explicit(self):
        for status in (0,64,255):
            for read in ([0xed,0x38,0xe0], [0x01,0xe0,0x5a,0xed,0x78]):
                with self.subTest(status=status, read=read):
                    result = self.execute(read + [0xfe,status,0x20,0xfe,0x76],
                        profile='lab-board-startup', status_e0_fixture=status)
                    self.assertEqual(result['stop_reason'], 'halt_or_sleep')
                    self.assertEqual((result['board_reads'],result['status_e0_fixture']), (1,status))
                    self.assertFalse(result['boot_verified'])
        result = self.execute([0xed,0x38,0xe0], profile='lab-board-startup')
        self.assertEqual(result['stop_reason'], 'unsupported_external_io_read')
        self.assertEqual((result['fault_address'],result['status_e0_fixture']), (0xe0,-1))

    def test_armed_csio_read_before_any_clock_is_the_stopped_register(self):
        # The firmware's link reset reads TRDR with RE still armed, then
        # disarms. No external symbol has moved, so the read is allowed at
        # every I/O relocation and leaves the transfer armed.
        for mode in (0x10, 0x20):
            for base in (0,0x40,0xc0):
                program = [0x3e,base,0xed,0x39,0x3f,
                           0x3e,mode | 7,0xed,0x39,base + 0x0a,
                           0xed,0x38,base + 0x0b,0x76]
                with self.subTest(mode=mode, base=base):
                    result = self.execute(program, profile='lab-board-startup')
                    self.assertEqual(result['stop_reason'], 'halt_or_sleep')
                    self.assertEqual((result['cntr'],result['iocr']), (mode | 7,base))

    def test_timer_channels_reload_isolation_and_relocation(self):
        program = [0x3e,0x40,0xed,0x39,0x3f]
        registers = (0x4c,0x4d,0x4e,0x4f,0x54,0x55,0x56,0x57)
        for index, port in enumerate(registers):
            program += [0x3e,0x80+index,0xed,0x39,port]
        for index, port in enumerate(registers):
            program += [0xed,0x38,port,0xfe,0x80+index,0x20,0xfe]
        result = self.execute(program + [0x76])
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual(result['iocr'], 0x40)
        self.assertEqual(result['tcr'], 0)

    def test_inactive_asci_initialization_and_readback(self):
        for channel in (0, 1):
            # Reset and software control readback; branch to budget on mismatch.
            reset = 0x10 if channel == 0 else 0
            program = [0xed,0x38,channel,0xfe,reset,0x20,0xfe,
                       0x3e,0x17,0xed,0x39,channel,
                       0xed,0x38,channel,0xfe,0x17,0x20,0xfe,
                       0xaf,0xed,0x39,4+channel,
                       0x3e,0x3e,0xed,0x39,2+channel,0x76]
            with self.subTest(channel=channel):
                result = self.execute(program)
                self.assertEqual(result['stop_reason'], 'halt_or_sleep')

    def test_asci_active_operations_and_board_pins_stay_guarded(self):
        for channel in (0, 1):
            for port, value, reason in [
                    (channel,0x20,'unsupported_asci_enable'),
                    (channel,0x40,'unsupported_asci_enable'),
                    (4+channel,1,'unsupported_asci_interrupt_enable'),
                    (4+channel,8,'unsupported_asci_interrupt_enable'),
                    (6+channel,0,'unsupported_asci_data_access'),
                    (8+channel,0,'unsupported_asci_data_access')]:
                with self.subTest(channel=channel, port=port, value=value):
                    result = self.execute([0x3e,value,0xed,0x39,port])
                    self.assertEqual(result['stop_reason'], reason)
                    self.assertEqual(result['fault_address'], port)
            for port in (2+channel,4+channel,6+channel,8+channel):
                with self.subTest(channel=channel, read=port):
                    result = self.execute([0xed,0x38,port])
                    self.assertEqual(result['stop_reason'],
                        'unsupported_asci_pin_read' if port < 6 else 'unsupported_asci_data_access')
                    self.assertEqual(result['fault_address'], port)

    def test_relocated_asci_guard_and_inactive_initialization(self):
        result = self.execute([0x3e,0x40,0xed,0x39,0x3f,
                               0xaf,0xed,0x39,0x44,0xed,0x38,0x42])
        self.assertEqual(result['iocr'], 0x40)
        self.assertEqual(result['stop_reason'], 'unsupported_asci_pin_read')
        self.assertEqual(result['fault_address'], 2)

    def test_active_refresh_m1_pulse_and_iostop_remain_guarded(self):
        for port, value, reason in [(0x36,0x80,'unsupported_refresh_enable'),
                                    (0x3e,0x00,'unsupported_m1_temporary_enable'),
                                    (0x3f,0x20,'unsupported_iostop')]:
            with self.subTest(port=port):
                result = self.execute([0x3e,value,0xed,0x39,port])
                self.assertEqual(result['stop_reason'], reason)
                self.assertEqual(result['fault_address'], port)

    def test_disable_refresh_and_reserved_register_bits(self):
        result = self.execute([0x3e,0x3f,0xed,0x39,0x36,
                               0x3e,0x5f,0xed,0x39,0x3e,
                               0x3e,0x1f,0xed,0x39,0x3f,0x76])
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['rcr'], result['omcr'], result['iocr']), (3, 0x40, 0))

    def test_inactive_timer_and_csio_reset_writes(self):
        result = self.execute([0x3e,0xc0,0xed,0x39,0x10,
                               0x3e,0x88,0xed,0x39,0x0a,0x76])
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['tcr'], result['cntr']), (0, 0))
        for port, value, reason in [(0x10,1,'unsupported_timer_configuration'),
                                    (0x10,0x20,'unsupported_timer_configuration'),
                                    (0x0a,0x10,'unsupported_csio_transfer'),
                                    (0x0a,0x20,'unsupported_csio_transfer')]:
            with self.subTest(port=port, value=value):
                active = self.execute([0x3e,value,0xed,0x39,port])
                self.assertEqual(active['stop_reason'], reason)
                self.assertEqual(active['tcr'], 0)
                self.assertEqual(active['cntr'], 7)

    def test_disabled_interrupt_and_dma_configuration(self):
        result = self.execute([0x3e,0x80,0xed,0x39,0x34,
                               0x3e,0xff,0xed,0x39,0x33,
                               0x3e,0x30,0xed,0x39,0x30,0x76])
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual((result['itc'], result['il'], result['dstat']), (0, 0xe0, 0x30))
        for port, value, reason in [(0x34,1,'unsupported_external_interrupt_enable'),
                                    (0x30,0x40,'unsupported_dma_configuration'),
                                    (0x30,0x80,'unsupported_dma_configuration'),
                                    (0x30,4,'unsupported_dma_configuration')]:
            with self.subTest(port=port, value=value):
                active = self.execute([0x3e,value,0xed,0x39,port])
                self.assertEqual(active['stop_reason'], reason)
                self.assertEqual(active['dstat'], 0x30)
                self.assertEqual(active['itc'], 1)

    def test_rom_writes_and_unmapped_reads_stop(self):
        result = self.execute([0x32,0x00,0x01])
        self.assertEqual(result['stop_reason'], 'rom_write_requires_board_decode')
        result = self.execute([0x3e,0x85,0xed,0x39,0x3a,0x3e,0x38,0xed,0x39,0x38,
                               0x3a,0x00,0x80])
        self.assertEqual(result['stop_reason'], 'unmapped_memory_read')
        self.assertEqual(result['fault_address'], 0x40000)

    def test_inactive_dma_addresses_counts_and_mode_readback(self):
        # Different bytes for each register reveal accidental aliases; upper
        # memory address bytes must discard the reserved high nibble.
        registers = list(range(0x20, 0x2d)) + [0x2e, 0x2f, 0x31]
        program = []
        for index, port in enumerate(registers):
            value = (0xe1 + index) & 0xff
            program += [0x3e,value,0xed,0x39,port]
        for index, port in enumerate(registers):
            value = (0xe1 + index) & 0xff
            mask = 0x0f if port in (0x22,0x25,0x2a) else 0x3e if port == 0x31 else 0xff
            program += [0xed,0x38,port,0xfe,value & mask,0x20,0xfe]
        program += [0x76]
        result = self.execute(program, instructions=200)
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual(result['dstat'], 0x30)

    def test_dma_mode_reserved_bits_and_masked_enables_do_not_transfer(self):
        for value in (0, 0xff, 0xc9, 0x28, 0x3c):
            with self.subTest(mode=value):
                result = self.execute([0x3e,value,0xed,0x39,0x31,
                    0xed,0x38,0x31,0xfe,value & 0x3e,0x20,0xfe,0x76])
                self.assertEqual(result['stop_reason'], 'halt_or_sleep')
                self.assertEqual(result['dstat'], 0x30)
        for value in range(256):
            # DWE=1 protects DE; DIE still requests an unmodeled interrupt.
            active = bool(value & 12) or (value & 0xa0) == 0x80 or (value & 0x50) == 0x40
            with self.subTest(status=value):
                result = self.execute([0x3e,value,0xed,0x39,0x30,0x76])
                self.assertEqual(result['stop_reason'],
                    'unsupported_dma_configuration' if active else 'halt_or_sleep')
                self.assertEqual(result['dstat'], 0x30)

    def test_base_cpu_rejects_enhanced_dma_register_and_relocation_works(self):
        for opcode in (0x38, 0x39):
            result = self.execute([0xed,opcode,0x2d])
            self.assertEqual(result['stop_reason'],
                'unsupported_internal_io_read' if opcode == 0x38 else 'unsupported_internal_io_write')
            self.assertEqual(result['fault_address'], 0x2d)
        result = self.execute([0x3e,0x40,0xed,0x39,0x3f,
            0x3e,0xff,0xed,0x39,0x62,0xed,0x38,0x62,0xfe,0x0f,0x20,0xfe,
            0x3e,0xf0,0xed,0x39,0x70,0x76])
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual(result['iocr'], 0x40)
        self.assertEqual(result['dstat'], 0x30)

    def test_modified_backend_refuses_build(self):
        modified = Path(self.temp.name) / 'modified'
        shutil.copytree(self.source, modified)
        (modified / 'COPYING').write_text('altered')
        with self.assertRaisesRegex(ValueError, 'pinned manifest'):
            build(modified, Path(self.temp.name) / 'refused')


# Original firmware is never committed. These run only when the environment
# names the public images.
MTR17 = os.environ.get('MILLENNIUM_MTR17_ROM')
NPA1S01 = os.environ.get('MILLENNIUM_NPA1S01_ROM')
# hharte/mm_manager, built from a clone (its tables/ and config/ are read
# beside the binary): the far end of the end-to-end install.
MM_MANAGER = os.environ.get('MILLENNIUM_MM_MANAGER')
# The voiceware 1.7 is paired with: with it,
# the whole-install test runs the voice chip instead of the silent sink.
VOICEWARE = os.environ.get('MILLENNIUM_VOICEWARE')


@unittest.skipUnless(os.environ.get('MILLENNIUM_Z180_SOURCE') and (MTR17 or NPA1S01),
                     'external Z180 source and a Millennium ROM image not supplied')
class OriginalFirmware(unittest.TestCase):
    # The fixtures under which 1.20 first drew its own error panel.
    STARTUP = dict(profile='lab-board-startup', instructions=16000000, int2_period_clocks=61440,
                   status_e0_fixture=0x40, panel_profile='cu20026-t23a-lab', asci0_pin_fixture=0)

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.worker = Path(cls.temp.name) / 'z180-worker'
        build(Path(os.environ['MILLENNIUM_Z180_SOURCE']), cls.worker)

    def panel(self, image, **fixtures):
        result = diagnostic.run_diagnostic(image, self.worker, **dict(self.STARTUP, **fixtures))
        return result, diagnostic.panel_text(result['panel_cells'])

    @unittest.skipUnless(MTR17, 'MILLENNIUM_MTR17_ROM not set')
    def test_mtr17_boots_as_far_as_120_and_bit5_is_inverted(self):
        # 1.7 pulses PB7 before its first PPI mode word. With the same
        # fixtures it reaches the same two panels as 1.20, but contact bit 5
        # selects them the other way round.
        result, text = self.panel(MTR17, status80_contacts=13)
        self.assertEqual(result['firmware_identifier'], '06CAA17')
        self.assertEqual(result['stop_reason'], 'budget')
        self.assertIn('not responding', text)
        self.assertIn('out of service', self.panel(MTR17, status80_contacts=15)[1])

    def screens(self, result):
        return [diagnostic.panel_text(bytes.fromhex(text)) for _, text in result['panel_history']]

    @unittest.skipUnless(MTR17, 'MILLENNIUM_MTR17_ROM not set')
    def test_mtr17_link_comes_up_with_the_telephony_stand_in(self):
        # With the stand-in answering, the alarm never appears: contact bit
        # 5 high gives the unprovisioned idle panel, low the craft PIN prompt.
        result, text = self.panel(MTR17, status80_contacts=15, telephony_profile='standin')
        self.assertEqual(result['stop_reason'], 'budget')
        self.assertEqual(text.strip(), '*  out of service  *')
        self.assertEqual(result['telephony_command_log'][:8], [0x30,0x33,0x38,0x31] * 2)
        self.assertEqual((result['telephony_status_queries'], result['telephony_frames'],
                          result['telephony_frame_errors']), (2, 1, 0))
        self.assertFalse(any('responding' in screen for screen in self.screens(result)))
        result, text = self.panel(MTR17, status80_contacts=13, telephony_profile='standin')
        self.assertTrue(text.startswith('Enter PIN:     ~~~~~~=FIX,*=SAVE,#=STOP'))

    # 1.7 keeps its alarms as a bitmap at F123 (bit id%8 of byte id/8), read
    # from its alarm routine. Alarm 33, Coin Hardware Jam, is F127 bit 1.
    ALARMS_17 = 0xf123

    @unittest.skipUnless(MTR17, 'MILLENNIUM_MTR17_ROM not set')
    def test_mtr17_coin_stand_in_keeps_the_jam_alarm_off(self):
        # Ten unanswered 2.5 s status polls raise the jam alarm, so both runs
        # outlast 28 s of modelled time (40M attempts is about 53 s).
        peek = tuple(range(self.ALARMS_17, self.ALARMS_17 + 5))
        silent, _ = self.panel(MTR17, status80_contacts=15, telephony_profile='standin',
                               instructions=40000000, peek=peek)
        answered, text = self.panel(MTR17, status80_contacts=15, telephony_profile='standin',
                                    coin_profile='standin', instructions=40000000, peek=peek,
                                    events=((45000, 'c', 1),))
        self.assertTrue(silent['ram_peek'][4][1] & 2)
        self.assertFalse(answered['ram_peek'][4][1] & 2)
        self.assertFalse(answered['ram_peek'][0][1] & 2)          # alarm 1: telephony board
        self.assertEqual(answered['coin_query_replies'], 4)
        self.assertGreater(answered['coin_status_replies'], 10)
        self.assertEqual((answered['coin_inserted'], text.strip()), (1, '*  out of service  *'))

    @staticmethod
    def keyed(start, keys, step=500):
        return tuple((start + step * i, 'k', key) for i, key in enumerate(keys))

    @unittest.skipUnless(MTR17, 'MILLENNIUM_MTR17_ROM not set')
    def test_mtr17_craft_entry_follows_the_documented_procedure(self):
        # Housing locked, cycle the hook,
        # key CRASERV (2727378), a PIN above 50000, `*`, unlock the housing.
        # The unlock is contact bit 5 going low (13), an inference this
        # sequence supports. Then `#` starts INSTALL's C.O. line check.
        events = (((4000, 'h', 1), (5000, 'h', 0)) + self.keyed(6000, '2727378')
                  + self.keyed(11000, '55555*') + ((16000, 's', 13), (19000, 'k', '#'), (22000, 'h', 1)))
        result, _ = self.panel(MTR17, status80_contacts=15, telephony_profile='standin',
                               coin_profile='standin', instructions=24000000, events=events)
        screens = [screen.strip() for screen in self.screens(result)]
        for expected in ('*  out of service  *', 'Enter PIN:     ~~~~~~=FIX,*=SAVE,#=STOP',
                         'Enter PIN:     55555~=FIX,*=SAVE,#=STOP',
                         'Please use key now  & open the terminal',
                         'Not installed       Use # to INSTALL', 'C.O.  line check    Go offhook',
                         'Checking C.O.       connection'):
            self.assertIn(expected, screens)
        self.assertLess(screens.index('Please use key now  & open the terminal'),
                        screens.index('Not installed       Use # to INSTALL'))
        # A PIN below 50000 is not refused here: it asks for an op code.
        result, _ = self.panel(MTR17, status80_contacts=15, telephony_profile='standin',
                               events=(((4000, 'h', 1), (5000, 'h', 0)) + self.keyed(6000, '2727378')
                                       + self.keyed(11000, '12345*')))
        self.assertEqual(self.screens(result)[-1].strip(), 'Enter op code:   ~~~~=FIX,*=SAVE,#=STOP')
        # Straight into craft (housing already open), the same PIN is refused.
        result, _ = self.panel(MTR17, status80_contacts=13, telephony_profile='standin',
                               events=self.keyed(5000, '12345*'))
        self.assertIn('Not a valid PIN     Access denied', [s.strip() for s in self.screens(result)])

    @staticmethod
    def mm_crc(data):
        # CRC-16, reflected polynomial 0xA001, from zero: what mm_manager
        # checks every packet against (written here from the polynomial).
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = crc >> 1 ^ 0xa001 if crc & 1 else crc >> 1
        return crc

    @unittest.skipUnless(MTR17, 'MILLENNIUM_MTR17_ROM not set')
    def test_mtr17_install_dials_the_ncc_and_sends_the_manager_a_packet(self):
        # The INSTALL procedure at the keypad, every reply the firmware waits
        # for supplied as an event: line state 84 to `37`, call progress on
        # the 73K212 (DR bit 1), answer 68, then answer tone (DR bit 2) and
        # the 212A handshake (bits 3 and 5) on the call to the NCC.
        events = (((4000, 'h', 1), (5000, 'h', 0)) + self.keyed(6000, '2727378')
                  + self.keyed(11000, '55555*')
                  + ((16000, 's', 13), (19000, 'k', '#'), (22000, 'h', 1), (24000, 'x', 0x84),
                     (24100, 'm', 0x02), (28000, 'k', '*'), (29000, 'h', 0))
                  + self.keyed(30000, '6135550100*', 400) + self.keyed(35000, '5550000001*', 400)
                  + self.keyed(40000, '6135559999*', 400)
                  + ((45000, 'h', 1), (50000, 'x', 0x68), (51000, 'k', '*'), (52000, 'h', 0),
                     (54000, 'k', '*'), (60000, 'm', 0x16), (61000, 'm', 0x3e)))
        result, text = self.panel(MTR17, status80_contacts=15, telephony_profile='standin',
                                  coin_profile='standin', modem_profile='control',
                                  instructions=76000000, events=events)
        screens = [screen.strip() for screen in self.screens(result)]
        for expected in ('Checking answer     detection', 'Press * to start NCC     download',
                         'Download in progress*   Please wait    *'):
            self.assertIn(expected, screens)
        self.assertEqual(text, 'Failed:           24Press * to try again')
        # The answer check dials the NCC number through the telephony board...
        log = bytes(result['telephony_command_log'])
        self.assertIn(bytes([0x3e, 0x16, 0x11, 0x13, 0x15, 0x15, 0x15, 0x19, 0x19, 0x19, 0x19]), log)
        # ...and the download dials it again as DTMF from the modem: TR 9d, then
        # CR0 1B/19 around each digit.
        writes = result['modem_write_log']
        digits = [value & 0x0f for value in writes if value >> 8 == 3 and value & 0xf0 == 0x90]
        self.assertEqual(digits[-10:], [6, 1, 3, 5, 5, 5, 9, 9, 9, 9])
        # Then ASCI1 carries mm_manager's framing: STX, flags, length, the
        # terminal ID (the line number typed above, BCD), TIME_SYNC_REQ (0x24),
        # CRC-16 low byte first, ETX. Retried twice, then a disconnect.
        sent = bytes(result['modem_transmit_log'])
        packets, rest = [], sent
        while rest:
            length = rest[2]
            packets.append(rest[:length + 1])
            rest = rest[length + 1:]
        self.assertEqual([packet[1] for packet in packets], [0x00, 0x04, 0x04, 0x20])
        for packet in packets:
            self.assertEqual((packet[0], packet[-1]), (0x02, 0x03))
            self.assertEqual(int.from_bytes(packet[-3:-1], 'little'), self.mm_crc(packet[:-3]))
        self.assertEqual(packets[0][3:-3], bytes.fromhex('613555010024'))
        self.assertEqual(packets[-1][3:-3], b'')

    def start_manager(self):
        """hharte's mm_manager (built at the ref and with the patches
        KL5 pins) behind atshim, as KL5 runs it.
        Returns the shim's wire port and the manager's log."""
        manager = Path(MM_MANAGER).resolve()
        work = Path(tempfile.mkdtemp(prefix='mm-install-'))
        self.addCleanup(shutil.rmtree, work, True)
        (work / 'config').symlink_to(manager.parent / 'config')
        (work / 'terms').mkdir()
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        listener.close()
        here = Path(__file__).resolve().parent
        shim = subprocess.Popen([sys.executable, str(here / 'atshim.py'), '--wire', 'listen:127.0.0.1:%d' % port,
                                 '--link', str(work / 'pty')], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(shim.wait)
        self.addCleanup(shim.terminate)
        for _ in range(80):
            if (work / 'pty').exists():
                break
            time.sleep(0.25)
        with (work / 'mm.log').open('wb') as log:
            # Line-buffered, as KL5's service runs it: a block-buffered
            # log still held the verdict when the test first read it.
            stdbuf = ['stdbuf', '-oL', '-eL'] if shutil.which('stdbuf') else []
            mm = subprocess.Popen(stdbuf + [str(manager), '-f', str(work / 'pty'), '-m', '-w', '-n', '6135559999',
                                   '-d', str(manager.parent / 'tables' / 'default'), '-t', str(work / 'terms')],
                                  cwd=work, stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(mm.wait)
        self.addCleanup(mm.terminate)
        for _ in range(80):
            if b'Waiting for call from terminal' in (work / 'mm.log').read_bytes():
                break
            time.sleep(0.25)
        return port, work / 'mm.log'

    # Every key on the set, in the order the keypad step is given them.
    ALL_KEYS = list('1234567890*#') + ['A%d' % n for n in range(1, 11)] + ['V-', 'V+', 'next', 'lang']
    # What the keypad step echoes for each (09:567D): A10 is "A0", the
    # diamond is glyph B7 ("~" in panel_text), language is "L".
    ECHOES = list('1234567890*#') + ['A%d' % (n % 10) for n in range(1, 11)] + ['V-', 'V+', '~', 'L']
    GOOD_CARD = '4761739001010010=30122011143804400000'
    EXPIRED_CARD = '4761739001010010=25122011143804400000'

    def keypad_step(self, screen, name):
        # Lift, every key 0.7 s apart, hang up: the step echoes each one.
        return craft.Step(screen, [(1, 'h', 1)] + [(2 + 0.7 * i, 'k', key) for i, key in enumerate(self.ALL_KEYS)]
                          + [(3 + 0.7 * len(self.ALL_KEYS), 'h', 0)], name=name, timeout=30)

    def install_and_craft_steps(self):
        Step, keys = craft.Step, craft.keys
        menu = lambda n, screen='DO IT': Step(screen, keys(n, 1), name='menu %s' % n, timeout=60)
        do_it = lambda n: Step('%s.' % n, [(1, 'k', '1')], name='do %s' % n, timeout=30)
        coins = lambda prefix: [Step('Deposit a', [(2, 'c', 1)], name=prefix + ' coin 1', timeout=30),
                                Step('Deposit a', [(2, 'c', 2)], name=prefix + ' coin 2', timeout=30),
                                Step('to test escrow', [(2, 'c', 1)], name=prefix + ' escrow coin', timeout=30),
                                Step('Completed:', [(2, 'k', '*')], name=prefix + ' coins done', timeout=30)]
        return ([
            # Craft entry on an unprovisioned set, then INSTALL's own order.
            Step('out of service', [(1, 'h', 1), (2, 'h', 0)] + keys('2727378', 3), name='entry'),
            Step('Enter PIN:', keys('55555*', 1), name='pin'),
            Step('Please use key now', [(1, 's', 13)], name='housing open'),
            Step('Use # to INSTALL', [(1, 'k', '#')], name='install'),
            Step('Go offhook', [(1, 'h', 1), (3, 'x', 0x84), (3.1, 'm', 0x02)], name='co line'),
            Step('Completed:        00', [(1, 'k', '*'), (2, 'h', 0)], name='co line done'),
            Step('Enter line tel. num', keys('6135550100*', 1, 0.4), name='line number'),
            Step('Enter serial number', keys('5550000001*', 1, 0.4), name='serial'),
            Step('Enter NCC tel.number', keys('6135559999*', 1, 0.4), name='ncc number'),
            Step('Answer detect check', [(1, 'h', 1)], name='answer detect'),
            Step('Completed:        00', [(1, 'k', '*'), (2, 'h', 0)], name='answer detect done'),
            Step('Press * to start NCC', [(1, 'k', '*')], name='download'),
            Step('Completed:        02', [(1, 'k', '*')], name='download done', timeout=400),
            self.keypad_step('press allbuttons', 'keypad'),
            Step('remove your card', [(1, 'w', 0)], name='card', timeout=30),
            Step('To continue, press *', [(2, 'k', '*')], name='card read', timeout=30),
            ] + coins('install') + [
            Step('close terminal', [(2, 's', 15)], name='housing locked', timeout=30),
            # Back in as a craftsperson on the installed set: each test once.
            Step('Local coin calls', [(2, 'h', 1), (3, 'h', 0)] + keys('2727378', 4), name='idle', timeout=60),
            Step('Enter PIN:', keys('55555*', 1), name='pin again', timeout=30),
            Step('Please use key now', [(1, 's', 13)], name='housing open again', timeout=30),
            menu('227', 'dial item number'), do_it('227'),
            Step('remove your card', [(1, 'w', 1)], name='227 expired card', timeout=30),
            Step('Expired', [(2, 'w', 0)], name='227 card', timeout=30),
            Step('To continue, press *', [(2, 'k', '*')], name='227 read', timeout=30),
            menu('264'), do_it('264')] + coins('264') + [
            menu('539'), do_it('539'), self.keypad_step('press allbuttons', '539 keypad'),
            menu('347'), do_it('347'),
            menu('274'), do_it('274'),
            Step('Choose level', [(2, 'k', '2')], name='274 level', timeout=30),
            menu('546'), do_it('546'),
            Step('Go offhook', [(1, 'h', 1), (3, 'x', 0x84), (3.1, 'm', 0x02)], name='546 lift', timeout=30),
            Step('To continue, press *', [(1, 'k', '*')], name='546 done', timeout=30),
            Step('Go back onhook', [(1, 'h', 0)], name='546 hang up', timeout=30),
            Step('DO IT', [], name='back at the menu', timeout=30),
        ])

    @unittest.skipUnless(MTR17 and MM_MANAGER, 'MILLENNIUM_MTR17_ROM and MILLENNIUM_MM_MANAGER not both set')
    def test_mtr17_whole_install_and_craft_tests_against_the_real_mm_manager(self):
        # The whole INSTALL against the real manager, every step the
        # firmware has, then the craft menu's hardware tests once each. A
        # craftsperson (millennium_craft.Operator) reads each panel and
        # reacts: keys, hook, contacts, a card, coins. ncc.ManagerLine plays
        # the network; every packet is the firmware's or the manager's own.
        # Paced to real time: about 8 minutes.
        port, mm_log = self.start_manager()
        line = ncc.ManagerLine(('127.0.0.1', port), '6135559999')
        op = craft.Operator(self.install_and_craft_steps(), line, hold_after=3)
        result, text = self.panel(MTR17, status80_contacts=15, telephony_profile='standin',
                                  coin_profile='standin', modem_profile='control',
                                  **(dict(speech_profile='chip', voiceware=VOICEWARE) if VOICEWARE
                                     else dict(speech_profile='silent')),
                                  instructions=diagnostic.MAX_STREAM_INSTRUCTIONS, far_end=op,
                                  cards=(self.GOOD_CARD, self.EXPIRED_CARD))
        self.assertIsNone(op.failed, op.screens[-3:])
        self.assertEqual(result['stop_reason'], 'stream_closed')
        self.assertEqual(line.calls, 2)
        # The download: every packet each way framed and checked, the
        # firmware's first is the time sync request, its last the clean
        # disconnect, and the manager's verdict is its own log's.
        self.assertTrue(all(ncc.packet_ok(packet) for _, _, packet in line.packets))
        phone = [packet for _, side, packet in line.packets if side == 'phone']
        self.assertEqual(phone[0], bytes.fromhex('02000b61355501002467c203'))
        self.assertEqual(phone[-1][1], 0x20)
        self.assertEqual(len(line.tables_acknowledged()), 29)
        self.assertEqual(result['stream_overruns'], 0)
        log = mm_log.read_text(errors='replace')
        self.assertIn('Received disconnect status OK from terminal.', log)
        self.assertIn('Reason: 0x06 [Install, Lost Memory, ]', log)
        # Both keypad tests echoed every key, in order, each where the one
        # before it ended (the echo runs on over the second row's text).
        for name in ('keypad', '539 keypad'):
            screens = op.screens_during(name)
            self.assertIn(' ' * 20 + 'Go onhook when done ', screens, name)
            for count in range(1, len(self.ECHOES) + 1):
                echoed = ''.join(self.ECHOES[:count])
                self.assertTrue(any(s.startswith(echoed) for s in screens), (name, echoed))
        # The card: its PAN, read going in; the expired one refused.
        self.assertIn('4761739001010010    To continue, press *', op.screens_during('card read'))
        self.assertIn('Expired or too earlyTry a different card', op.screens_during('227 card'))
        self.assertIn('4761739001010010    To continue, press *', op.screens_during('227 read'))
        self.assertEqual((result['card_swipes'], result['card_lost'], result['card_overruns']), (3, 0, 0))
        self.assertGreater(result['card_chip_reads'], 0)          # asked for a chip; there was none
        # The coins: two calibration coins and the escrow coin, twice.
        for prefix in ('install', '264'):
            self.assertEqual(op.screens_during(prefix + ' coins done')[0],
                             'Completed:        00To continue, press *')
        self.assertEqual(result['coin_inserted'], 6)
        self.assertIn('Install is complete  close terminal now ', op.screens_during('housing locked'))
        # The display test walked a glyph across all forty cells, the
        # brightness menu sent its level, the C.O. line test passed.
        walk = op.screens_during('do 347')
        self.assertIn('~' * 40, walk)
        self.assertTrue(all(any(s.index('~') == cell for s in walk if s.count('~') == 1) for cell in range(40)))
        self.assertEqual(result['panel_brightness'], 0x10)
        self.assertIn('Completed:        00To continue, press *', op.screens_during('546 done'))
        self.assertGreater(result['speech_writes'], 0)            # the off-hook prompt
        if VOICEWARE:
            # Played on the chip: the off-hook set's first sample, cut short
            # by the hang-up a second later, and nothing during INSTALL.
            self.assertEqual(result['voice_log'][0][1:], [1, 0, 18])
            self.assertGreater(result['voice_cuts'], 0)
        self.assertGreater(result['panel_status_reads'], 0)

    @unittest.skipUnless(NPA1S01 and MM_MANAGER, 'MILLENNIUM_NPA1S01_ROM and MILLENNIUM_MM_MANAGER not both set')
    def test_npa1s01_install_against_the_real_mm_manager(self):
        # MTR 1.20's own INSTALL, download included, against the real
        # manager. It runs as 1.7's does, with one screen more: after the
        # housing opens, 1.20 shows its version and waits for #. Proven
        # first on 2026-11-22, when the manager's table data went from
        # 245-byte pieces to 128 (patches/0014) and the question was
        # whether this firmware joins smaller pieces too. About 5 minutes.
        Step, keys = craft.Step, craft.keys
        steps = [
            Step('out of service', [(1, 'h', 1), (2, 'h', 0)] + keys('2727378', 3), name='entry'),
            Step('Enter PIN:', keys('55555*', 1), name='pin'),
            Step('Please use key now', [(1, 's', 13)], name='housing open'),
            Step('Press # to continue', [(1, 'k', '#')], name='version'),
            Step('Use # to INSTALL', [(1, 'k', '#')], name='install'),
            Step('Go offhook', [(1, 'h', 1), (3, 'x', 0x84), (3.1, 'm', 0x02)], name='co line'),
            Step('Completed:        00', [(1, 'k', '*'), (2, 'h', 0)], name='co line done'),
            Step('Enter line tel. num', keys('6135550100*', 1, 0.4), name='line number'),
            Step('Enter serial number', keys('5550000001*', 1, 0.4), name='serial'),
            Step('Enter NCC tel.number', keys('6135559999*', 1, 0.4), name='ncc number'),
            Step('Answer detect check', [(1, 'h', 1)], name='answer detect'),
            Step('Completed:        00', [(1, 'k', '*'), (2, 'h', 0)], name='answer detect done'),
            Step('Press * to start NCC', [(1, 'k', '*')], name='download'),
            Step('Completed:        02', [(1, 'k', '*')], name='download done', timeout=600),
        ]
        port, mm_log = self.start_manager()
        line = ncc.ManagerLine(('127.0.0.1', port), '6135559999')
        op = craft.Operator(steps, line, hold_after=3)
        result, text = self.panel(NPA1S01, status80_contacts=15, telephony_profile='standin',
                                  coin_profile='standin', modem_profile='control', speech_profile='silent',
                                  instructions=diagnostic.MAX_STREAM_INSTRUCTIONS, far_end=op)
        self.assertIsNone(op.failed, op.screens[-3:])
        self.assertTrue(all(ncc.packet_ok(packet) for _, _, packet in line.packets))
        self.assertEqual(len(line.tables_acknowledged()), 32)
        self.assertIn('Received disconnect status OK from terminal.', mm_log.read_text(errors='replace'))

    @unittest.skipUnless(NPA1S01, 'MILLENNIUM_NPA1S01_ROM not set')
    def test_npa1s01_link_comes_up_with_the_telephony_stand_in(self):
        result, text = self.panel(NPA1S01, status80_contacts=15, telephony_profile='standin')
        self.assertEqual(text.strip(), '*  out of service  *')
        result, text = self.panel(NPA1S01, status80_contacts=13, telephony_profile='standin')
        self.assertEqual(text, 'Not installed       Use # to INSTALL    ')

    @unittest.skipUnless(NPA1S01, 'MILLENNIUM_NPA1S01_ROM not set')
    def test_npa1s01_still_reports_the_unanswered_telephony_board(self):
        result, text = self.panel(NPA1S01, status80_contacts=15)
        self.assertEqual(result['firmware_identifier'], 'NPA1S01')
        self.assertIn('not responding', text)


if __name__ == '__main__':
    unittest.main()
