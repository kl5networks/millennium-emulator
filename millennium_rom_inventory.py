"""Identify private Millennium images without executing or exporting ROM data.

Recognition establishes archive identity only. It does not select a verified
board map, initialize NVRAM, or imply that a terminal can boot or make a call.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys


FW_SHA256 = 'c04d14fbe063529eb7969310b34712da884cbc3aaa1bbd6491fe8e230fbf9488'
VW_SHA256 = 'b1024bf1a81d1bacad4f1022eee5c3162f69846ac28e2fdf347f1e63b2ac890d'
# MTR 1.7 06CAA17 is what the bench phone runs, so it is the primary target.
FW17_SHA256 = '3536fa1fe06d7dff818af663d3114299b874e8a3954edc24aefcf639fb5fa309'
VOICE_BANK_SIZE = 131072
MAX_IMAGE_SIZE = 1048576
ARCHIVE = 'muccc/millennium:firmware/NPA1S01/dump1'
ARMENIKI = 'armeniki/Nortel-Millennium:Firmware/MTR_1.7'
IMAGES = {
    FW_SHA256: {'role': 'firmware', 'size': 262144, 'identifier': 'NPA1S01',
                'archive_catalog': 'MTR 1.20', 'embedded_identifier': 'NPA1S01V1.0',
                'embedded_identifier_offset': 0x1FA3, 'cpu_candidate': 'Z180',
                'archive': ARCHIVE},
    # The same bytes are armeniki's Voiceware/English-Spanish/O6EAJ01.bin and
    # NPA1S01 dump2's NT_VW_06EAJ01 chip: English in banks 0-1, Spanish in
    # 2-3. It is the voiceware the 1.7 emulator plays (MILLENNIUM_VOICE.md):
    # 06CAA17's second language is Spanish, and every sample its phrase
    # tables name is inside these banks' sample counts.
    VW_SHA256: {'role': 'voiceware', 'size': 524288, 'identifier': '06EAJ01',
                'archive_catalog': 'English/Spanish voiceware', 'pairs_with': ['06CAA17', 'NPA1S01'],
                'format_candidate': 'uPD7759 standalone banked voice ROM', 'archive': ARCHIVE},
    # Two English/French images with the same bank layout. Recognised so an
    # intake names them; neither matches 1.7's Spanish display text.
    '9fa4717a8803985402032bbc543e9720053588e9c0e69f67d4b0a9bdc3ff1782': {
        'role': 'voiceware', 'size': 524288, 'identifier': 'LM5U09BA',
        'archive_catalog': 'English/French voiceware', 'pairs_with': [],
        'format_candidate': 'uPD7759 standalone banked voice ROM',
        'archive': 'armeniki/Nortel-Millennium:Voiceware/English-French'},
    '7b92e886b72b044b35b3b30fe39284017a9119e51053a1fe7578118489cbd981': {
        'role': 'voiceware', 'size': 524288, 'identifier': '06FEN01',
        'archive_catalog': 'English/French voiceware', 'pairs_with': ['06JAA03'],
        'format_candidate': 'uPD7759 standalone banked voice ROM',
        'archive': 'muccc/millennium:firmware/06JAA03'},
    FW17_SHA256: {'role': 'firmware', 'size': 262144, 'identifier': '06CAA17',
                  'archive_catalog': 'MTR 1.7', 'embedded_identifier': '06CAA17V1.0',
                  'embedded_identifier_offset': 0xFEC, 'cpu_candidate': 'Z180',
                  'archive': 'muccc/millennium:firmware/M06CAA17'},
    # The four armeniki 1.7 builds are different link maps of the same
    # release, not byte variants of 06CAA17. Recognised for identity; only
    # the multipay build has been run (it reaches the same panel stage).
    # The two coin builds write port C0 at start-up, which nothing models.
    '7ae8f0847b1f1d96690a13afdefb6f6fbd0b0f14ac52e066765cea6e1af7a17b': {
        'role': 'firmware', 'size': 262144, 'identifier': 'NAA1S05', 'archive_catalog': 'MTR 1.7 multipay',
        'embedded_identifier': 'NAA1S05V1.0', 'embedded_identifier_offset': 0x101D,
        'cpu_candidate': 'Z180', 'archive': ARMENIKI + '/MULTIPAY'},
    '9dfb1388c43747f141ac8e380c8e7fe9b4c9c2a0a73cc328cb2772003072b1ce': {
        'role': 'firmware', 'size': 262144, 'identifier': 'NAD4K02', 'archive_catalog': 'MTR 1.7 coin',
        'embedded_identifier': 'NAD4K02V1.0', 'embedded_identifier_offset': 0xFEC,
        'cpu_candidate': 'Z180', 'archive': ARMENIKI + '/Coin'},
    '8be05875ba6496069a8142ba7dc13e7c48da6cf6dc9a1e2e1941aff3a441f7a0': {
        'role': 'firmware', 'size': 262144, 'identifier': 'NAD4S02', 'archive_catalog': 'MTR 1.7 coin, no display',
        'embedded_identifier': 'NAD4S02V1.0', 'embedded_identifier_offset': 0xFEC,
        'cpu_candidate': 'Z180', 'archive': ARMENIKI + '/COIN_NO_DISPLAY'},
    '32551c4a16461ccbff5747dc9af3eaf50b65717972eca047c0ef707ec3a639c0': {
        'role': 'firmware', 'size': 262144, 'identifier': 'NAJ2S05', 'archive_catalog': 'MTR 1.7 desk',
        'embedded_identifier': 'NAJ2S05V1.0', 'embedded_identifier_offset': 0x103A,
        'cpu_candidate': 'Z180', 'archive': ARMENIKI + '/DESK'},
    # The owner's own image, uploaded 2026-11-11: a TTY build. Same reset
    # layout as 1.7, English plus a Bosnian/Serbo-Croatian second language,
    # and an English-only TTY block at 0x37400 ("Dial your TTY number",
    # "Connecting to TTY"). A "V2.2" string follows the identifier; at one
    # 256 KiB chip it does not look like a two-chip MTR 2.x, so the MTR
    # release is unconfirmed. Recognised for identity only, never run.
    'e552b1ba938c5fb36b2448866eb7c259f2e9fb967e4d7ebc2d768977a15ed9b7': {
        'role': 'firmware', 'size': 262144, 'identifier': '06CNB03', 'archive_catalog': 'TTY build, MTR unconfirmed',
        'embedded_identifier': '06CNB03V1.0', 'embedded_identifier_offset': 0x1277,
        'cpu_candidate': 'Z180', 'archive': 'owner upload 2026-11-11 (private Library)'},
}
# Images the Z180 diagnostic will run. Recognition alone is not permission.
EXECUTABLE_FIRMWARE = (FW17_SHA256, FW_SHA256)


class InventoryError(ValueError):
    pass


def voice_banks(data):
    """Inspect standalone speech headers; offsets are word-addressed in a bank.

    MAME upd7759.cpp reads the last sample at byte zero and big-endian word
    offsets at 5+2*sample, shifted by one. Samples may alias or be unordered;
    neither increasing offsets nor decoded language is assumed here.
    """
    if not data or len(data) % VOICE_BANK_SIZE:
        raise InventoryError('voiceware must contain complete 128 KiB banks')
    banks = []
    for base in range(0, len(data), VOICE_BANK_SIZE):
        block = data[base:base + VOICE_BANK_SIZE]
        if block[1:5] != bytes.fromhex('5aa56955'):
            raise InventoryError('unrecognized voice-bank header')
        count = block[0] + 1
        table_end = 5 + count * 2
        offsets = [int.from_bytes(block[5+2*i:7+2*i], 'big') * 2 for i in range(count)]
        if any(a < table_end or a >= VOICE_BANK_SIZE for a in offsets):
            raise InventoryError('voice sample pointer outside bank data')
        banks.append({'base': base, 'size': VOICE_BANK_SIZE, 'sample_count': count,
                      'first_sample_offset': offsets[0], 'largest_sample_offset': max(offsets),
                      'offsets_monotonic': offsets == sorted(offsets)})
    return banks


def inspect_image(data):
    if not data or len(data) > MAX_IMAGE_SIZE:
        raise InventoryError('image size is outside the inspection budget')
    digest = hashlib.sha256(data).hexdigest()
    known = IMAGES.get(digest)
    result = {'sha256': digest, 'size': len(data), 'recognized': known is not None,
              'implemented': False, 'board_verified': False, 'ready_for_execution': False}
    if known is None:
        result['role'] = 'unknown'
        return result
    result.update(known)
    if known['role'] == 'firmware':
        # Describe the short actual reset sequence, not a generic disassembler.
        if data[:6] != bytes.fromhex('f30e01c30002'):
            raise InventoryError('recognized firmware reset layout is inconsistent')
        offset, ident = known['embedded_identifier_offset'], known['embedded_identifier'].encode()
        if data[offset:offset + len(ident)] != ident:
            raise InventoryError('recognized firmware identifier is inconsistent')
        result['reset'] = {'entry': 0, 'interrupts_disabled': True,
                           'initial_c': 1, 'jump_target': 0x0200,
                           'initial_stack': int.from_bytes(data[0x201:0x203], 'little')}
    else:
        result['banks'] = voice_banks(data)
    return result


def inspect_pair(firmware, voiceware):
    fw, vw = inspect_image(firmware), inspect_image(voiceware)
    if (not fw['recognized'] or fw['role'] != 'firmware'
            or not vw['recognized'] or vw['role'] != 'voiceware'):
        raise InventoryError('pair requires the recognized firmware and voiceware in their correct roles')
    # Both recognised is not a pair: a French voiceware under a Spanish
    # firmware speaks the wrong second language.
    return {'schema_version': 1, 'family': 'millennium', 'archive_pair_verified': True,
            'voiceware_pairs_with_firmware': fw.get('identifier') in vw.get('pairs_with', ()),
            'compatibility_verified': False, 'ready_for_execution': False,
            'firmware': fw, 'voiceware': vw,
            'next_gate': 'verified Z180 CPU/reset/memory and matching board peripherals'}


def read_image(path):
    # Bound reads even if a supplied file grows or its stat is misleading.
    with Path(path).open('rb') as source:
        data = source.read(MAX_IMAGE_SIZE + 1)
    if not data or len(data) > MAX_IMAGE_SIZE:
        raise InventoryError('image size is outside the inspection budget')
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--firmware', required=True)
    parser.add_argument('--voiceware', required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect_pair(read_image(args.firmware), read_image(args.voiceware))
    except (OSError, InventoryError):
        print('Millennium intake refused: check image identity, roles and size.', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
