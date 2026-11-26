"""The voice chip: the MIT decoder on synthetic banks, the worker's BUSY and
INT0 on a synthetic program, and -- only when pointed at them -- the real
voiceware and the 1.7 firmware's own prompts."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import millennium_voice as voice
import millennium_z180 as diagnostic
from millennium_rom_inventory import VW_SHA256
from z180.build_millennium_z180 import build


def bank(samples, last=None):
    """A 128 KiB bank holding `samples` (each the bytes after the skipped
    dummy), laid out from 0x100 on word boundaries."""
    data = bytearray(voice.BANK_SIZE)
    data[0] = len(samples) - 1 if last is None else last
    data[1:5] = voice.SIGNATURE
    at = 0x100
    for n, body in enumerate(samples):
        data[5 + 2 * n:7 + 2 * n] = (at // 2).to_bytes(2, 'big')
        data[at] = 0x00                       # the dummy
        data[at + 1:at + 1 + len(body)] = body
        at += 2 + len(body) + (len(body) & 1)
    return bytes(data)


# The same three samples the worker's self-test times (voice_contracts):
# 256 nibbles at N=2; silence then a 4-nibble block at N=1; a block twice.
CONTRACT = [bytes([0x41]) + bytes(128) + b'\x00',
            bytes([0x03, 0x80, 3, 0, 0, 0x00]),
            bytes([0xc1, 0x80, 1, 0, 0x00])]


class Decoder(unittest.TestCase):
    def test_lengths_agree_with_the_worker_contract(self):
        b = bank(CONTRACT)
        self.assertEqual([voice.sample_clocks(b, n) for n in range(3)],
                         [218 + 36 + 256 * 8 + 36, 218 + 4096 + 36 + 16 + 36, 218 + 2 * (36 + 8) + 36])
        self.assertEqual(voice.sample_clocks(b, 3), 218 + 36)     # past the last: nothing
        self.assertEqual(voice.blocks(b, 3), [])

    def test_known_step_vector(self):
        # From state 0: +10 (state 3), +19 (6), +33 (9), +1 (8), -1 (7), -43 (10).
        b = bank([bytes([0x80, 5, 0x77, 0x70, 0x8f, 0x00])])
        values = [value for value, _ in voice.segments(b, 0)]
        self.assertEqual(values[2:8], [10, 29, 62, 63, 62, 19])

    def test_the_accumulator_stops_at_the_nine_bit_rails(self):
        b = bank([bytes([0x80, 31]) + bytes([0x77] * 16) + bytes([0x80, 31]) + bytes([0xff] * 16) + b'\x00'])
        values = [value for value, _ in voice.segments(b, 0)]
        self.assertEqual((max(values), min(values)), (255, -256))

    def test_silence_resets_value_and_state(self):
        b = bank([bytes([0x80, 1, 0x77, 0x01, 0x80, 0, 0x70, 0x00])])
        values = [value for value, _ in voice.segments(b, 0)]
        # After the silence the first +7 nibble steps from state 0 again: +10.
        self.assertEqual(values[-2:], [10, 10])
        self.assertIn(0, values[3:-2])

    def test_eight_khz_rendering_holds_one_output_per_nibble_at_n20(self):
        b = bank([bytes([0x93, 1, 0x77, 0x00])])                 # N = 20: 80 clocks per nibble
        pcm = voice.render([(v, c) for v, c in voice.segments(b, 0) if c == 80])
        self.assertEqual(pcm, [10 * 128, 29 * 128])
        self.assertEqual(voice.render([(1, 40), (3, 40)]), [2 * 128])   # a window is a mean
        with self.assertRaises(voice.VoiceError):
            voice.render([(0, 1)], rate=7000)

    def test_headers_and_pointers_are_checked(self):
        good = bank(CONTRACT)
        for data in (b'', good[:-1], bytes(voice.BANK_SIZE)):
            with self.assertRaises(voice.VoiceError):
                voice.banks(data)
        bad = bytearray(good)
        bad[5:7] = (1).to_bytes(2, 'big')                        # into the header
        with self.assertRaises(voice.VoiceError):
            voice.blocks(bytes(bad), 0)
        endless = bytearray(bank([b'\x40']))                     # runs off the bank
        endless[0x102:] = b'\x40' * (len(endless) - 0x102)
        with self.assertRaises(voice.VoiceError):
            voice.blocks(bytes(endless), 0)

    def test_bank_switching_and_mirroring(self):
        rom = bank([bytes([0x80, 1, 0x77, 0x00])]) + bank([bytes([0x80, 1, 0xff, 0x00])])
        self.assertEqual(voice.decode(rom, 0, 0)[-1] > 0, True)
        self.assertEqual(voice.decode(rom, 1, 0)[-1] < 0, True)
        self.assertEqual(voice.decode(rom, 3, 0), voice.decode(rom, 1, 0))   # A18 not decoded

    def test_wav_is_8khz_mono_16_bit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'x.wav'
            voice.write_wav(path, [0, 1000, -1000])
            with wave.open(str(path)) as w:
                self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()),
                                 (8000, 1, 2, 3))


class StreamAndWrapper(unittest.TestCase):
    def test_voice_events_decode_and_are_in_the_stream_table(self):
        self.assertEqual(diagnostic.STREAM_LENGTHS[b'V'], 7)
        self.assertEqual(diagnostic.voice_event(bytes([1, 1, 5]) + (1234).to_bytes(4, 'little')),
                         ('start', 1, 5, 1234))
        self.assertEqual(diagnostic.voice_event(bytes([2, 0, 0, 0, 0, 0, 0]))[0], 'cut')
        messages, rest = diagnostic.split_stream(b'V' + bytes([0, 3, 9, 1, 0, 0, 0]) + b'Z\x01')
        self.assertEqual((messages, rest), ([('V', bytes([0, 3, 9, 1, 0, 0, 0]))], b'Z\x01'))
        with self.assertRaises(diagnostic.DiagnosticError):
            diagnostic.voice_event(bytes([3, 0, 0, 0, 0, 0, 0]))

    def test_the_chip_needs_its_voiceware_and_nothing_else_takes_one(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / 'fw.bin'
            image.write_bytes(bytes(262144))
            rom = Path(temp) / 'vw.bin'
            rom.write_bytes(bank(CONTRACT))
            base = dict(profile='lab-board-startup', status_e0_fixture=0x40, int2_period_clocks=61440,
                        telephony_profile='standin')
            with patch.object(diagnostic, 'EXECUTABLE_FIRMWARE', (hashlib.sha256(bytes(262144)).hexdigest(),)):
                for fixtures in (dict(speech_profile='chip'), dict(speech_profile='silent', voiceware=rom),
                                 dict(voiceware=rom), dict(speech_profile='chip', voiceware=image)):
                    with self.subTest(fixtures=fixtures), self.assertRaises(diagnostic.DiagnosticError):
                        diagnostic.run_diagnostic(image, '/worker', **base, **fixtures)


@unittest.skipUnless(os.environ.get('MILLENNIUM_Z180_SOURCE'), 'external pinned Z180 source not supplied')
class Worker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.worker = Path(cls.temp.name) / 'z180-worker'
        build(Path(os.environ['MILLENNIUM_Z180_SOURCE']), cls.worker)
        cls.rom = Path(cls.temp.name) / 'voiceware.bin'
        cls.rom.write_bytes(bank([b'\x01\x00']) + bank(CONTRACT))

    def execute(self, program, **fixtures):
        image = Path(self.temp.name) / 'synthetic.bin'
        data = bytes(program) + bytes(262144 - len(program))
        image.write_bytes(data)
        base = dict(profile='lab-board-startup', instructions=400000, status_e0_fixture=0x40,
                    int2_period_clocks=61440, telephony_profile='standin', status80_contacts=15)
        with patch.object(diagnostic, 'EXECUTABLE_FIRMWARE', (hashlib.sha256(data).hexdigest(),)):
            return diagnostic.run_diagnostic(image, self.worker, **dict(base, **fixtures))

    # The firmware's player in miniature: bank 1 on PB3-1, latch bit 3, a
    # sample number to C0, then INT0 (IM 1, RST 38) writes the next until a
    # count in RAM reaches three.
    PLAYER = ([0x3e,0x85,0xed,0x39,0x3a, 0x3e,0x38,0xed,0x39,0x38, 0x31,0xf0,0xff, 0xed,0x56,
               0x3e,0x80,0xd3,0x43, 0x3e,0x02,0xd3,0x41, 0x3e,0x28,0xed,0x39,0x60,
               0x3e,0x01,0xed,0x39,0xc0, 0xfb, 0x18,0xfe])
    HANDLER = [0x3a,0x00,0x80, 0x3c, 0x32,0x00,0x80, 0xfe,0x03, 0x28,0x07,
               0x3e,0x02,0xed,0x39,0xc0, 0xfb,0xc9, 0xaf,0xed,0x39,0x34, 0x76]

    def program(self, player=None):
        player = self.PLAYER if player is None else player
        return player + [0] * (0x38 - len(player)) + self.HANDLER

    def test_int0_asks_for_each_next_sample_when_busy_drops(self):
        result = self.execute(self.program(), speech_profile='chip', voiceware=self.rom, peek=(0x8000,))
        self.assertEqual(result['stop_reason'], 'halt_or_sleep')
        self.assertEqual(result['ram_peek'], [[0x8000, 3]])
        self.assertEqual((result['voice_starts'], result['voice_stops'], result['int0_interrupts']), (3, 3, 3))
        log = result['voice_log']
        self.assertEqual([entry[1:] for entry in log],
                         [[1, 1, 1], [0, 1, 1], [1, 1, 2], [0, 1, 2], [1, 1, 2], [0, 1, 2]])
        # BUSY lasts what the MIT decoder says the sample lasts.
        clocks = voice.sample_clocks(voice.bank_of(self.rom.read_bytes(), 1), 1)
        self.assertLessEqual(abs((log[1][0] - log[0][0]) - clocks * 1000 / voice.CHIP_CLOCK_HZ), 1)

    def test_reset_cuts_a_sample_and_the_sink_never_asks(self):
        # Clear latch bit 3 straight after the start: a cut, and no INT0.
        player = self.PLAYER[:-3] + [0x3e,0x20,0xed,0x39,0x60, 0x76]
        result = self.execute(self.program(player), speech_profile='chip', voiceware=self.rom)
        self.assertEqual((result['stop_reason'], result['voice_cuts'], result['int0_interrupts']),
                         ('halt_or_sleep', 1, 0))
        result = self.execute(self.program(), speech_profile='silent', instructions=100000)
        self.assertEqual((result['stop_reason'], result['speech_writes'], result['int0_interrupts']),
                         ('budget', 1, 0))


# The voiceware the 1.7 firmware is paired with.
VOICEWARE = os.environ.get('MILLENNIUM_VOICEWARE')
# The 1.7 phrase tables' (bank, sample) pairs (18:5810, tables 3448/3452/353A).
# Bank 0 sample 44 is a 50 ms pause between words, and is tested as one.
PHRASE_SAMPLES = ({(0, n) for n in list(range(18)) + [18, 19, 20, 49, 66]}
                  | {(1, n) for n in range(48)} | {(2, n) for n in list(range(16)) + [18]}
                  | {(3, n) for n in range(49)})


@unittest.skipUnless(VOICEWARE, 'MILLENNIUM_VOICEWARE not set')
class OriginalVoiceware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = Path(VOICEWARE).read_bytes()

    def test_the_off_hook_prompts_last_what_the_emulator_timed(self):
        # Measured with --speech chip on 06CAA17: start to BUSY drop, in ms.
        for sample, ms in ((6, 2579), (1, 1918), (7, 2330)):
            self.assertLessEqual(abs(voice.summary(self.rom, 0, sample)['ms'] - ms), 1)

    def test_identity_and_bank_layout(self):
        self.assertEqual(hashlib.sha256(self.rom).hexdigest(), VW_SHA256)
        self.assertEqual([voice.last_sample(b) + 1 for b in voice.banks(self.rom)], [68, 48, 20, 49])

    def test_every_sample_the_firmware_names_decodes_to_sound(self):
        for bank_number, sample in sorted(PHRASE_SAMPLES):
            with self.subTest(bank=bank_number, sample=sample):
                info = voice.summary(self.rom, bank_number, sample)
                self.assertTrue(20 <= info['ms'] <= 25000, info)
                self.assertGreater(info['voiced_ms'], 5, info)
        # Bank 0's tone-like samples do not read as ADPCM; every word does.
        tones = {(0, 18), (0, 19), (0, 20), (0, 49)}
        for bank_number, sample in sorted(PHRASE_SAMPLES):
            plausible = voice.adpcm_plausible(voice.bank_of(self.rom, bank_number), sample)
            self.assertEqual(plausible, (bank_number, sample) not in tones, (bank_number, sample))
        pause = voice.summary(self.rom, 0, 44)
        self.assertEqual((pause['voiced_ms'], pause['rms']), (0, 0))


if __name__ == '__main__':
    unittest.main()
