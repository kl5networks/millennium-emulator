"""The Millennium's voice chip: a uPD7759 in stand-alone mode, decoded.

The 1.7 firmware never sends audio. It sends a SAMPLE NUMBER to port C0
and a bank on PPI port B bits 3..1; the chip finds the sample in its own
ROM (the "voiceware") and plays it, and the firmware hears that it has
finished through INT0.
So hearing a prompt needs only the voiceware and this decoder; the Z180
worker (z180/, GPL) reports which sample started when, on its clock.

A voiceware image is 128 KiB banks, each laid out the way the chip reads
it [datasheet layout, as every public decoder reads it]:

    byte 0          the last sample number in the bank
    bytes 1..4      5A A5 69 55
    5 + 2n, 6 + 2n  sample n's start, a big-endian WORD address

At a sample's byte address the first byte is skipped (the chip reads it
as a dummy; it is 00 in every bank measured here), then blocks follow:

    00              end, once any non-zero header has been seen
    01..3F          silence: (h + 1) * 1024 chip clocks
    40..7F          256 nibbles, each held (h & 3F) + 1 times 4 clocks
    80..BF  c       c + 1 nibbles, the same rate rule
    C0..FF          repeat the next block (h & 7) + 1 times

Each nibble moves a 9-bit sample by a step chosen from a 16 x 16 table
by the nibble and an adaptive state. The step and state tables below are
the chip's published constants (the same values every uPD7759 decoder
carries, including the public-domain one in muccc/millennium); the code
is written for this repository and is MIT, like the rest of it.

The chip clock is 640 kHz: NEC's family user guide lists only 640 kHz
resonators, and the speech samples' divider of 20 then gives exactly
8 kHz [the board's own resonator is not measured]. A sample period is then 4 * N clocks,
and the voiceware here uses N = 20 throughout: exactly 8 kHz. Output is
rendered at 8 kHz signed 16-bit by averaging the chip's held output over
each 80-clock window, which is exact for N = 20 and a box filter for any
other rate.
"""

import argparse
import json
from pathlib import Path
import struct
import sys
import wave


BANK_SIZE = 131072
SIGNATURE = bytes.fromhex('5aa56955')
CHIP_CLOCK_HZ = 640000          # the family user guide lists only 640 kHz resonators
OUTPUT_RATE = 8000
# From ST to the first block header: the chip reads the last-sample byte,
# a dummy, the two address bytes and the skipped byte. About 0.3 ms
# [inferred: a few hundred clocks; the datasheet gives no figure here].
START_CLOCKS = 218
# Clocks spent reading a block header or a nibble count [inferred].
HEADER_CLOCKS = 36
# The nibble table: step[state][nibble]. Rows grow about 1.2x per state.
STEP = (
    (0, 0, 1, 2, 3, 5, 7, 10, 0, 0, -1, -2, -3, -5, -7, -10),
    (0, 1, 2, 3, 4, 6, 8, 13, 0, -1, -2, -3, -4, -6, -8, -13),
    (0, 1, 2, 4, 5, 7, 10, 15, 0, -1, -2, -4, -5, -7, -10, -15),
    (0, 1, 3, 4, 6, 9, 13, 19, 0, -1, -3, -4, -6, -9, -13, -19),
    (0, 2, 3, 5, 8, 11, 15, 23, 0, -2, -3, -5, -8, -11, -15, -23),
    (0, 2, 4, 7, 10, 14, 19, 29, 0, -2, -4, -7, -10, -14, -19, -29),
    (0, 3, 5, 8, 12, 16, 22, 33, 0, -3, -5, -8, -12, -16, -22, -33),
    (1, 4, 7, 10, 15, 20, 29, 43, -1, -4, -7, -10, -15, -20, -29, -43),
    (1, 4, 8, 13, 18, 25, 35, 53, -1, -4, -8, -13, -18, -25, -35, -53),
    (1, 6, 10, 16, 22, 31, 43, 64, -1, -6, -10, -16, -22, -31, -43, -64),
    (2, 7, 12, 19, 27, 37, 51, 76, -2, -7, -12, -19, -27, -37, -51, -76),
    (2, 9, 16, 24, 34, 46, 64, 96, -2, -9, -16, -24, -34, -46, -64, -96),
    (3, 11, 19, 29, 41, 57, 79, 117, -3, -11, -19, -29, -41, -57, -79, -117),
    (4, 13, 24, 36, 50, 69, 96, 143, -4, -13, -24, -36, -50, -69, -96, -143),
    (4, 16, 29, 44, 62, 85, 118, 175, -4, -16, -29, -44, -62, -85, -118, -175),
    (6, 20, 36, 54, 76, 104, 144, 214, -6, -20, -36, -54, -76, -104, -144, -214),
)
# How the adaptive state moves after each nibble (magnitude bits only).
STATE_DELTA = (-1, -1, 0, 0, 1, 2, 2, 3, -1, -1, 0, 0, 1, 2, 2, 3)
# The 9-bit output: the accumulator is held inside it [inferred: a 9-bit
# DAC]. Speech never reaches the rails (it stays inside +-130); bank 0's
# samples 18-39 and 49-64 do not decode as ADPCM at all (see
# adpcm_plausible) and would drift without bound otherwise.
DAC_MIN, DAC_MAX = -256, 255
# A runaway guard: no sample in a 128 KiB bank can need more blocks.
MAX_BLOCKS = 65536


class VoiceError(ValueError):
    pass


def banks(rom):
    """The 128 KiB banks of a voiceware image, each checked for the header."""
    if not rom or len(rom) % BANK_SIZE:
        raise VoiceError('voiceware must be whole 128 KiB banks')
    out = []
    for base in range(0, len(rom), BANK_SIZE):
        bank = rom[base:base + BANK_SIZE]
        if bank[1:5] != SIGNATURE:
            raise VoiceError('bank %d has no voice header' % (base // BANK_SIZE))
        out.append(bank)
    return out


def bank_of(rom, number):
    """Bank `number` as the chip sees it: an image smaller than the eight
    banks port B can select repeats [inferred: the top address lines are
    not decoded]."""
    all_banks = banks(rom)
    return all_banks[number % len(all_banks)]


def last_sample(bank):
    return bank[0]


def sample_address(bank, sample):
    """Byte address of `sample`'s data, or None past the bank's last sample
    (the chip then plays nothing and goes idle)."""
    if not 0 <= sample <= 255:
        raise VoiceError('a sample number is one byte')
    if sample > bank[0]:
        return None
    address = int.from_bytes(bank[5 + 2 * sample:7 + 2 * sample], 'big') * 2
    if address < 5 + 2 * (bank[0] + 1) or address >= len(bank):
        raise VoiceError('sample %d points outside its bank' % sample)
    return address


def blocks(bank, sample):
    """Sample `sample` as the chip walks it: a list of
    ('silence', clocks) and ('adpcm', clocks_per_nibble, nibbles)."""
    address = sample_address(bank, sample)
    if address is None:
        return []
    offset = address + 1                   # the dummy byte
    seen_header = False
    repeat_count, repeat_offset = 0, 0
    out = []

    def byte():
        nonlocal offset
        if offset >= len(bank):
            raise VoiceError('sample %d runs off its bank' % sample)
        value = bank[offset]
        offset += 1
        return value

    for _ in range(MAX_BLOCKS):
        if repeat_count:
            repeat_count -= 1
            offset = repeat_offset
        header = byte()
        kind = header & 0xc0
        if kind == 0x00:
            if header == 0 and seen_header:
                return out
            out.append(('silence', 1024 * ((header & 0x3f) + 1)))
        elif kind == 0xc0:
            repeat_count, repeat_offset = (header & 7) + 1, offset
        else:
            count = 256 if kind == 0x40 else byte() + 1
            data = []
            for i in range(count):
                if not i & 1:
                    pair = byte()
                    data.append(pair >> 4)
                else:
                    data.append(pair & 15)
            out.append(('adpcm', 4 * ((header & 0x3f) + 1), tuple(data)))
        if header:
            seen_header = True
    raise VoiceError('sample %d never ends' % sample)


def sample_clocks(bank, sample):
    """How long the chip is busy with `sample`, in chip clocks: what the
    worker's BUSY model must agree with (test_millennium_voice)."""
    total = START_CLOCKS
    for block in blocks(bank, sample):
        total += HEADER_CLOCKS if block[0] == 'adpcm' else 0
        total += block[1] if block[0] == 'silence' else block[1] * len(block[2])
    return total + HEADER_CLOCKS        # the closing 00


def segments(bank, sample):
    """The chip's output as (value, chip clocks) runs: a 9-bit sample held
    for each nibble's period, zero through silence (which also resets the
    adaptive state)."""
    value, state = 0, 0
    out = [(0, START_CLOCKS)]
    for block in blocks(bank, sample):
        if block[0] == 'silence':
            value, state = 0, 0
            out.append((0, block[1]))
            continue
        out.append((value, HEADER_CLOCKS))
        for nibble in block[2]:
            value = min(DAC_MAX, max(DAC_MIN, value + STEP[state][nibble]))
            state = min(15, max(0, state + STATE_DELTA[nibble]))
            out.append((value, block[1]))
    out.append((value, HEADER_CLOCKS))
    return out


def render(runs, rate=OUTPUT_RATE, clock=CHIP_CLOCK_HZ, gain=128):
    """(value, clocks) runs to signed 16-bit PCM at `rate`: each output
    sample is the mean of the chip's output over its window."""
    if clock % rate:
        raise VoiceError('the chip clock must be a whole multiple of the rate')
    window = clock // rate
    out = []
    acc, filled = 0, 0
    for value, clocks in runs:
        while clocks:
            take = min(clocks, window - filled)
            acc += value * take
            filled += take
            clocks -= take
            if filled == window:
                out.append(max(-32768, min(32767, acc * gain // window)))
                acc, filled = 0, 0
    if filled:
        out.append(max(-32768, min(32767, acc * gain // window)))
    return out


def decode(rom, bank_number, sample, rate=OUTPUT_RATE):
    """Sample `sample` of bank `bank_number`, as 16-bit PCM at `rate`."""
    return render(segments(bank_of(rom, bank_number), sample), rate)


def write_wav(path, pcm, rate=OUTPUT_RATE):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(struct.pack('<%dh' % len(pcm), *pcm))


def adpcm_plausible(bank, sample):
    """Whether a sample reads as uPD7759 ADPCM. Encoded speech keeps the
    adaptive state low most of the time; bank 0's tone-like samples (18 is
    the one an off-hook set plays first) pin it at 14-15 for most nibbles,
    and their bytes read as 8-bit sine fragments instead. What the real
    board makes of them is [unknown]."""
    state, high, total = 0, 0, 0
    for block in blocks(bank, sample):
        if block[0] == 'silence':
            state = 0
            continue
        for nibble in block[2]:
            state = min(15, max(0, state + STATE_DELTA[nibble]))
            high += state >= 14
            total += 1
    return total == 0 or high * 4 < total


def summary(rom, bank_number, sample):
    """What a sample is, without its audio: duration and loudness."""
    bank = bank_of(rom, bank_number)
    pcm = render(segments(bank, sample))
    voiced = [s for s in pcm if abs(s) > 512]
    rms = (sum(s * s for s in pcm) / len(pcm)) ** 0.5 if pcm else 0.0
    rates = sorted({b[1] for b in blocks(bank, sample) if b[0] == 'adpcm'})
    return {'bank': bank_number, 'sample': sample,
            'ms': round(sample_clocks(bank, sample) * 1000 / CHIP_CLOCK_HZ),
            'voiced_ms': len(voiced) * 1000 // OUTPUT_RATE,
            'rms': round(rms), 'clocks_per_nibble': rates,
            'adpcm_plausible': adpcm_plausible(bank, sample)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--voiceware', required=True)
    parser.add_argument('--bank', type=int, choices=range(8))
    parser.add_argument('--sample', type=int, choices=range(256))
    parser.add_argument('--wav', help='write the sample (or, with --out-dir, every sample) as 8 kHz WAV')
    parser.add_argument('--out-dir', help='write every sample of every bank here as b<bank>_s<sample>.wav')
    args = parser.parse_args(argv)
    try:
        rom = Path(args.voiceware).read_bytes()[:8 * BANK_SIZE + 1]
        if len(rom) > 8 * BANK_SIZE:
            raise VoiceError('voiceware larger than eight banks')
        all_banks = banks(rom)
        if args.bank is not None and args.sample is not None:
            if args.wav:
                write_wav(args.wav, decode(rom, args.bank, args.sample))
            print(json.dumps(summary(rom, args.bank, args.sample)))
            return 0
        listing = []
        for number, bank in enumerate(all_banks):
            for sample in range(last_sample(bank) + 1):
                listing.append(summary(rom, number, sample))
                if args.out_dir:
                    write_wav(Path(args.out_dir) / ('b%d_s%02d.wav' % (number, sample)),
                              decode(rom, number, sample))
        print(json.dumps(listing, indent=1))
    except (OSError, VoiceError) as error:
        print('voiceware refused: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
