/* SPDX-License-Identifier: GPL-2.0-or-later
 * The voice chip: a uPD7759 in stand-alone mode, as the 1.7 firmware drives
 * it. Timing only: which sample plays and
 * how long BUSY is held. The audio itself is decoded on the MIT side
 * (../millennium_voice.py) from the same voiceware; the two must agree on
 * a sample's length, and test_millennium_voice checks that they do.
 *
 * The firmware's side, read from its code [code]:
 *   - PPI port B bits 3..1 select one of eight 128 KiB voice banks;
 *   - control latch (port 60) bit 3 is set before a prompt and cleared
 *     when it ends or is abandoned;
 *   - a write to port C0 is a sample number;
 *   - INT0, enabled only while a prompt plays, asks for the next one.
 *     Nothing ever reads the chip: there is no BUSY poll.
 * The board's side, inferred from that: latch bit 3 is the chip's /RESET
 * (clearing it mid-sample is how a prompt is cut off); a C0 write latches
 * the byte and strobes /ST; INT0 is asserted while the chip is out of reset
 * and not busy (an inverted /BUSY), so the handler's write that starts the
 * next sample is also what takes the request away.
 * The chip clock is 640 kHz [inferred: the part's standard clock].
 */
#ifndef MILLENNIUM_VOICE_H
#define MILLENNIUM_VOICE_H

#define MM_VOICE_BANK 131072u
#define MM_VOICE_MAX (8u * MM_VOICE_BANK)
#define MM_VOICE_LOG 64
/* Chip clocks from /ST to the first block, and per header [inferred]; the
 * same constants as millennium_voice.START_CLOCKS / HEADER_CLOCKS. */
#define MM_VOICE_START_CLOCKS 218u
#define MM_VOICE_HEADER_CLOCKS 36u
/* Board clocks are 6.144 MHz, the chip's 640 kHz: 9.6 to one. */
#define MM_VOICE_BOARD_PER_CHIP_NUM 48u
#define MM_VOICE_BOARD_PER_CHIP_DEN 5u

enum { MM_VOICE_STOP = 0, MM_VOICE_START = 1, MM_VOICE_CUT = 2 };

struct mm_voice {
    int enabled;
    unsigned char *rom;
    unsigned size;               /* whole banks */
    int running;                 /* out of reset: control latch bit 3 */
    int busy;
    unsigned bank, sample;
    unsigned long long left;     /* board clocks until BUSY drops */
    unsigned starts, stops, cuts, ignored, beyond, errors;
    unsigned log_count;
    struct { unsigned ms; unsigned char op, bank, sample; } log[MM_VOICE_LOG];
};

/* How long the chip is busy with `sample` of `bank`, in chip clocks: the
 * block walk of millennium_voice.blocks, timing only. A sample past the
 * bank's last is the header read and nothing more. A walk that runs off
 * the bank ends there and counts as an error. */
static unsigned long mm_voice_clocks(struct mm_voice *v, unsigned bank, unsigned sample)
{
    const unsigned char *b = v->rom + (bank % (v->size / MM_VOICE_BANK)) * MM_VOICE_BANK;
    unsigned long total = MM_VOICE_START_CLOCKS, offset;
    unsigned repeat = 0, repeat_offset = 0, blocks = 0, seen = 0;
    if (sample > b[0]) { v->beyond++; return total; }
    offset = 2ul * ((b[5 + 2 * sample] << 8) | b[6 + 2 * sample]) + 1;
    while (blocks++ < 65536) {
        unsigned header, count;
        if (repeat) { repeat--; offset = repeat_offset; }
        if (offset >= MM_VOICE_BANK) break;
        header = b[offset++];
        if ((header & 0xc0) == 0) {
            if (!header && seen) return total + MM_VOICE_HEADER_CLOCKS;
            total += 1024ul * ((header & 0x3f) + 1);
        } else if ((header & 0xc0) == 0xc0) {
            repeat = (header & 7) + 1;
            repeat_offset = offset;
        } else {
            if ((header & 0xc0) == 0x40) count = 256;
            else {
                if (offset >= MM_VOICE_BANK) break;
                count = b[offset++] + 1u;
            }
            total += MM_VOICE_HEADER_CLOCKS + count * 4ul * ((header & 0x3f) + 1);
            offset += (count + 1) / 2;
        }
        if (header) seen = 1;
    }
    v->errors++;
    return total;
}

static void mm_voice_note(struct mm_voice *v, unsigned ms, unsigned op)
{
    unsigned i = v->log_count++ % MM_VOICE_LOG;
    v->log[i].ms = ms;
    v->log[i].op = (unsigned char)op;
    v->log[i].bank = (unsigned char)v->bank;
    v->log[i].sample = (unsigned char)v->sample;
}

/* Whether the chip is asking for the next sample (INT0) [inferred]. */
static int mm_voice_request(const struct mm_voice *v)
{
    return v->enabled && v->running && !v->busy;
}

/* Control latch bit 3: releasing reset starts nothing; asserting it stops
 * whatever is playing. */
static void mm_voice_reset_line(struct mm_voice *v, unsigned latch, unsigned ms)
{
    int running = !!(latch & 8);
    if (!v->enabled) return;
    if (!running && v->busy) {
        v->busy = 0;
        v->left = 0;
        v->cuts++;
        mm_voice_note(v, ms, MM_VOICE_CUT);
    }
    v->running = running;
}

/* A C0 write: the sample number, and /ST. Ignored in reset and while busy
 * (the part starts only from idle). `port_b` is the PPI port B latch. */
static void mm_voice_start(struct mm_voice *v, unsigned value, unsigned port_b, unsigned ms)
{
    unsigned long long clocks;
    if (!v->running || v->busy) { v->ignored++; return; }
    v->bank = (port_b >> 1) & 7;
    v->sample = value & 0xff;
    clocks = mm_voice_clocks(v, v->bank, v->sample);
    v->left = (clocks * MM_VOICE_BOARD_PER_CHIP_NUM + MM_VOICE_BOARD_PER_CHIP_DEN - 1)
              / MM_VOICE_BOARD_PER_CHIP_DEN;
    v->busy = 1;
    v->starts++;
    mm_voice_note(v, ms, MM_VOICE_START);
}

/* One board clock. Returns 1 when BUSY has just dropped. */
static int mm_voice_tick(struct mm_voice *v, unsigned ms)
{
    if (!v->busy || --v->left) return 0;
    v->busy = 0;
    v->stops++;
    mm_voice_note(v, ms, MM_VOICE_STOP);
    return 1;
}

#endif
