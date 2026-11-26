/* SPDX-License-Identifier: GPL-2.0-or-later
 * A stand-in for the Millennium's coin mechanism on ASCI0. INFERRED: what
 * the control firmware's coin task sends and waits for
 *, not a real
 * mechanism. Opt-in.
 *
 * The control board sends single letters. `r` (and `g` before it) starts a
 * 250-tick reply timer that only a `B s1 s2` status cancels; ten timeouts
 * raise alarm 33, Coin Hardware Jam. So the stand-in answers every `r`
 * with `B 00 00`: no fault bits. A coin is the channel's letter, `1`..`9`
 * then `a`..`g` for channels 1..16.
 *
 * `q a b` is a query the firmware collects as a four-byte `FF` message:
 * it sets that message up itself, sends `q 01 FC` .. `q 01 FF`, and copies
 * the four bytes it gets back. So each query gets one byte, and the
 * stand-in's are zero: what a real mechanism keeps there is unknown.
 * `d`/`e` carry one channel letter after them.
 *
 * `g` is answered with a hello, `A` by default. INSTALL's coin step and
 * menu 264 (09:6B55, 09:67CF) send `g` and take `A`, `D` or `I` as a
 * mechanism that is there, `E` as an EEPROM checksum fault (Failed: 66)
 * and silence as no mechanism (Failed: 61) [code]. Which letter a real,
 * healthy mechanism sends is [inferred]: `A` is the one the second `g`
 * of the calibration step insists on. The stand-in says nothing to `l`
 * and `k`, which start calibration [inferred], because any byte while the
 * step waits after them fails it (61). That includes the `B 00 00` it
 * answers `r` with: the coin task forwards every `B` to the craft task
 * (12:70EC posts 42 to task 2) and the step's states after `l` (09:6CB7)
 * and `k` (09:6CF0) take any coin byte as a fault. The coin task keeps
 * polling `r` through those 3.25 s, so a mechanism that passes must be
 * deaf to `r` from `l` to the next `g`: the stand-in is, and its first
 * build, which answered every `r`, failed INSTALL with 61
 * [inferred: what a real mechanism does while it calibrates is unknown].
 * The state waiting for the hello (09:6BD0) fails on a `B` too, and the
 * coin task's own `A` handler sends `z` before the step sends `l`, so a
 * poll that crosses `g` on the line failed the second build on its second
 * run. So `g` also drops any reply not yet started and leaves `r`
 * unanswered for 100 ms [policy]: a mechanism that restarts on `g`.
 * `hello` < 0 makes it silent, and `mm_coin_raw` puts any byte on the
 * line, which is how the step's other fail codes (63 `0`, 64 `G`,
 * 67 `I`/`J`, 68 `D`) are reached.
 *
 * The escrow test's own traffic (coin-task commands 3 and 11, 12:72C6 and
 * 12:743F) is `z`, `f`, `r` polls, `n`, `f` and the four `q` queries:
 * all already answered or ignored here [code for the bytes].
 */
#ifndef MILLENNIUM_COIN_H
#define MILLENNIUM_COIN_H

#include <string.h>

#define MM_COIN_QUEUE 32u
#define MM_COIN_LOG 64u
#define MM_COIN_REPLY_CLOCKS 61440u /* 10 ms before a reply starts [inferred] */
#define MM_COIN_HELLO_QUIET_CLOCKS 614400u /* 100 ms deaf to `r` after `g` [policy] */

struct mm_coin {
    unsigned enabled;
    unsigned char out[MM_COIN_QUEUE];
    unsigned char rest[MM_COIN_QUEUE]; /* 1: not the first byte of its message */
    unsigned out_head, out_count, wait;
    unsigned char command;            /* a command still collecting its arguments */
    unsigned arguments;
    unsigned char log[MM_COIN_LOG];   /* the last bytes from the control board */
    unsigned log_count, status_replies, query_replies, coins, sent;
    int hello;                        /* the answer to `g`, -1 for none */
    unsigned hello_replies, raw;
    unsigned calibrating;             /* from `l` to the next `g`: deaf to `r` */
    unsigned quiet;                   /* clocks left deaf to `r` after a `g` */
};

static void mm_coin_queue(struct mm_coin *c, const unsigned char *bytes, unsigned n)
{
    unsigned i;
    if (!c->out_count) c->wait = MM_COIN_REPLY_CLOCKS;
    for (i = 0; i < n && c->out_count < MM_COIN_QUEUE; i++) {
        c->out[(c->out_head + c->out_count) % MM_COIN_QUEUE] = bytes[i];
        c->rest[(c->out_head + c->out_count) % MM_COIN_QUEUE] = i != 0;
        c->out_count++;
    }
}

/* A byte the control board sent. */
static void mm_coin_command(struct mm_coin *c, unsigned char byte)
{
    static const unsigned char status[] = {'B', 0, 0};
    static const unsigned char zero = 0;
    c->log[c->log_count++ % MM_COIN_LOG] = byte;
    if (c->arguments) {
        if (--c->arguments || c->command != 'q') return;
        c->query_replies++;
        mm_coin_queue(c, &zero, 1);
        return;
    }
    c->command = byte;
    if (byte == 'q') { c->arguments = 2; return; }
    if (byte == 'd' || byte == 'e') { c->arguments = 1; return; }
    if (byte == 'l') c->calibrating = 1;
    if (byte == 'g') {
        c->calibrating = 0;
        /* A reply not yet on the line is dropped; the rest of one already
         * started is finished, or the firmware would frame a torn `B`. */
        unsigned keep = 0;
        while (keep < c->out_count && c->rest[(c->out_head + keep) % MM_COIN_QUEUE]) keep++;
        c->out_count = keep;
        c->quiet = MM_COIN_HELLO_QUIET_CLOCKS;
    }
    if (byte == 'g' && c->hello >= 0) {
        unsigned char hello = (unsigned char)c->hello;
        c->hello_replies++;
        mm_coin_queue(c, &hello, 1);
    }
    if (byte == 'r' && !c->calibrating && !c->quiet) {
        c->status_replies++;
        mm_coin_queue(c, status, sizeof(status));
    }
}

/* A coin on channel 1..16. */
static int mm_coin_insert(struct mm_coin *c, unsigned channel)
{
    unsigned char letter;
    if (channel < 1 || channel > 16) return 0;
    letter = channel <= 9 ? '0' + channel : 'a' + channel - 10;
    c->coins++;
    mm_coin_queue(c, &letter, 1);
    return 1;
}

/* Any byte from the mechanism: a fault letter, a stray reply. */
static void mm_coin_raw(struct mm_coin *c, unsigned char byte)
{
    c->raw++;
    mm_coin_queue(c, &byte, 1);
}

static void mm_coin_tick(struct mm_coin *c, unsigned clocks)
{
    if (c->wait) c->wait = clocks < c->wait ? c->wait - clocks : 0;
    if (c->quiet) c->quiet = clocks < c->quiet ? c->quiet - clocks : 0;
}

/* The next byte to start on the line, or -1. The caller asks only when the
 * receiver is idle and its data register empty, so nothing overruns. */
static int mm_coin_next(struct mm_coin *c)
{
    int byte;
    if (!c->out_count || c->wait) return -1;
    byte = c->out[c->out_head];
    c->out_head = (c->out_head + 1) % MM_COIN_QUEUE;
    c->out_count--;
    c->sent++;
    return byte;
}

#endif
