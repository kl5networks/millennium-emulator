"""Intake refuses swapped/modified images and never reports private file data."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import millennium_rom_inventory as inventory


def bank(offsets):
    data = bytearray(inventory.VOICE_BANK_SIZE)
    data[0] = len(offsets)-1
    data[1:5] = bytes.fromhex('5aa56955')
    for i, offset in enumerate(offsets):
        data[5+2*i:7+2*i] = (offset//2).to_bytes(2, 'big')
    return bytes(data)


class InventoryTest(unittest.TestCase):
    def test_voice_word_addresses_aliases_and_unordered_samples(self):
        banks = inventory.voice_banks(bank([100,200,100])+bank([300,120]))
        self.assertEqual([b['sample_count'] for b in banks], [3,2])
        self.assertEqual(banks[0]['first_sample_offset'],100)
        self.assertEqual(banks[1]['base'],inventory.VOICE_BANK_SIZE)
        self.assertFalse(banks[0]['offsets_monotonic'])
        self.assertFalse(banks[1]['offsets_monotonic'])

    def test_incomplete_bad_header_and_pointer_into_header_are_refused(self):
        for data in (b'', bank([100])[:-1], bytes(inventory.VOICE_BANK_SIZE),bank([2])):
            with self.subTest(length=len(data)):
                with self.assertRaises(inventory.InventoryError):
                    inventory.voice_banks(data)

    def test_unknown_bytes_are_not_extractable_or_executable(self):
        data=b'PRIVATE SENTINEL VERSION PIN 9876'
        report=inventory.inspect_image(data)
        self.assertFalse(report['recognized'])
        self.assertFalse(report['ready_for_execution'])
        self.assertFalse(report['board_verified'])
        self.assertNotIn('SENTINEL',str(report))
        with self.assertRaises(inventory.InventoryError):
            inventory.inspect_pair(data,data)

    def test_roles_are_checked_and_pair_identity_is_not_board_acceptance(self):
        fw={'recognized':True,'role':'firmware'}
        vw={'recognized':True,'role':'voiceware'}
        with patch.object(inventory,'inspect_image',side_effect=[fw,vw]):
            result=inventory.inspect_pair(b'a',b'b')
        self.assertTrue(result['archive_pair_verified'])
        self.assertFalse(result['compatibility_verified'])
        self.assertFalse(result['ready_for_execution'])
        for results in ([vw,fw],[fw,fw],[vw,vw]):
            with patch.object(inventory,'inspect_image',side_effect=results):
                with self.assertRaises(inventory.InventoryError):
                    inventory.inspect_pair(b'a',b'b')

    def test_firmware_identity_needs_the_embedded_identifier_too(self):
        data = bytearray(bytes.fromhex('f30e01c30002') + bytes(0x2000))
        data[0x201:0x203] = (0xffec).to_bytes(2, 'little')
        data[0xfec:0xff7] = b'06CAA17V1.0'
        import hashlib
        entry = dict(inventory.IMAGES[inventory.FW17_SHA256])
        with patch.dict(inventory.IMAGES, {hashlib.sha256(bytes(data)).hexdigest(): entry}):
            report = inventory.inspect_image(bytes(data))
            self.assertEqual((report['identifier'], report['archive_catalog']), ('06CAA17', 'MTR 1.7'))
            self.assertEqual(report['reset']['initial_stack'], 0xffec)
        data[0xfec] = ord('X')
        with patch.dict(inventory.IMAGES, {hashlib.sha256(bytes(data)).hexdigest(): entry}):
            with self.assertRaises(inventory.InventoryError):
                inventory.inspect_image(bytes(data))
        self.assertIn(inventory.FW17_SHA256, inventory.EXECUTABLE_FIRMWARE)

    def test_cli_refusal_has_no_private_path_or_contents_and_bounded_read(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'private-secret-name.bin'
            path.write_bytes(b'PRIVATE BYTES')
            out,err=io.StringIO(),io.StringIO()
            with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                self.assertEqual(inventory.main(['--firmware',str(path),'--voiceware',str(path)]),1)
            self.assertEqual(out.getvalue(),'')
            self.assertNotIn(td,err.getvalue())
            self.assertNotIn('private-secret',err.getvalue())
            self.assertNotIn('PRIVATE BYTES',err.getvalue())
            path.write_bytes(bytes(inventory.MAX_IMAGE_SIZE+1))
            with self.assertRaises(inventory.InventoryError):
                inventory.read_image(path)


class VoicewarePairing(unittest.TestCase):
    def test_only_the_spanish_voiceware_pairs_with_1_7(self):
        entry = inventory.IMAGES[inventory.VW_SHA256]
        self.assertEqual(entry['identifier'], '06EAJ01')
        self.assertIn('06CAA17', entry['pairs_with'])
        french = [e for e in inventory.IMAGES.values() if e['role'] == 'voiceware' and 'French' in e['archive_catalog']]
        self.assertEqual(len(french), 2)
        self.assertTrue(all('06CAA17' not in e['pairs_with'] for e in french))


if __name__ == '__main__':
    unittest.main()
