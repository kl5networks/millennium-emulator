/* SPDX-License-Identifier: GPL-2.0-or-later
 * A stand-in for the Millennium's magnetic-stripe card reader. INFERRED:
 * what the 1.7 firmware's card driver (bank 24) programs and samples, read
 * from its code, not a real reader.
 * Opt-in.
 *
 * The driver points DMA channel 0 at I/O port $80 (SAR0 $000080, I/O
 * fixed to memory incrementing, DMODE $0C), edge-sensed on /DREQ0 (DCNTL
 * $F4), 255 bytes, end interrupt on (DSTAT $64). Every strobe from the
 * reader stores one whole port-$80 byte; bit 0 is the data bit. Bit 1 and
 * bit 2 are two card sensors, read by 24:5946 into 8AA1 and 8AA0. The
 * classifier at 24:5CCE shows which is which: inserting, both set at rest
 * means a card is in; 8AA1 alone at rest means "partly in" and, coming
 * out, "partly out"; neither means gone. So bit 1 is the slot (card
 * present) and bit 2 the back of the reader (fully seated) [code for the
 * reading, inferred for the physical placement].
 *
 * The stripe is ABA track 2 [inferred from the format: 24:5999 wants
 * 5-bit characters, LSB first, odd parity, `;` start, `=` separator, `?`
 * end at position 6 or later, at most 40 characters, then an XOR LRC]. A
 * card is read going in (the buffer forward, 24:5999 mode 1) and, if that
 * failed, coming out (mode 3: the buffer backward from 255 - BCR0, which
 * is the same bits in their written order because the head sees the
 * stripe end first).
 *
 * A seated card also makes the firmware look for a chip: 27:5098 clocks
 * ten bytes in through PPI port C bit 2 (24:71D5, eight IN0 (42h) per
 * byte, PC6/PC7 the clock and reset outputs), and only if 27:64AC finds
 * them in the downloaded issuer table does it set 80DA bits 1-2 (chip
 * card). The stand-in has no chip: with the lower half of port C an input
 * it reads bit 2 high, an open I/O contact on its pull-up, and bits 0, 1
 * and 3 low [inferred: no schematic; the routine tests bit 2 alone].
 *
 * A swipe here is a person inserting the card fully, pausing, and pulling
 * it out. Speeds are a policy, not a measurement: a bit every 4,096 phi
 * (0.67 ms, 1,500 bit/s: 75 bit/inch at 20 inch/s).
 */
#ifndef MILLENNIUM_CARD_H
#define MILLENNIUM_CARD_H

#include <string.h>

#define MM_CARD_CARDS 8u
#define MM_CARD_BODY 37u          /* between the sentinels: 40 with ; ? and LRC */
#define MM_CARD_BITS 255u
#define MM_CARD_CLOCKING 12u      /* zero bits before and after the characters */
#define MM_CARD_BIT_CLOCKS 4096u  /* 0.67 ms a bit [policy] */
#define MM_CARD_SLOT_CLOCKS 122880u   /* 20 ms from the slot sensor to the head */
#define MM_CARD_DWELL_CLOCKS 12288000u /* 2 s fully in before pulling it out */

/* Port $80 bits [code for the reading, inferred for the wiring]. */
#define MM_CARD_DATA 1u
#define MM_CARD_PRESENT 2u
#define MM_CARD_SEATED 4u
#define MM_CARD_CHIP_IO 4u    /* PPI port C bit 2 */

enum mm_card_phase { MM_CARD_IDLE, MM_CARD_ENTERING, MM_CARD_INSERTING, MM_CARD_IN,
                     MM_CARD_WITHDRAWING, MM_CARD_LEAVING };

struct mm_card {
    unsigned enabled, count;
    /* Per card: the bits on the stripe in the order the head meets them
     * going in, and how the swipe reads ('b' both strokes good, 'i' only
     * going in, 'o' only coming out: going in it reads damaged, 'n' no
     * stripe at all). */
    unsigned char bits[MM_CARD_CARDS][MM_CARD_BITS];
    unsigned nbits[MM_CARD_CARDS];
    char how[MM_CARD_CARDS];
    /* The swipe in progress. */
    unsigned phase, current, wait, next_bit, port;
    unsigned swipes, strobes, latched, lost, overruns, busy;
    unsigned chip_reads;          /* PPI port C reads answered "no chip" */
};

/* One character: four data bits LSB first, then odd parity. */
static unsigned mm_card_char(unsigned char *bits, unsigned at, unsigned value)
{
    unsigned i, ones = 0;
    for (i = 0; i < 4; i++) {
        bits[at + i] = (value >> i) & 1;
        ones += bits[at + i];
    }
    bits[at + 4] = !(ones & 1);
    return at + 5;
}

/* ABA track 2 for `body` (digits and '=', 1..37 of them): clocking zeros,
 * `;`, the body, `?`, the LRC (the XOR of every character's data bits,
 * sentinels included, with its own odd parity), clocking zeros. Returns
 * the bit count, or 0 for a body that is not a track-2 string. */
static unsigned mm_card_encode(const char *body, unsigned char *bits)
{
    unsigned n = strlen(body), at = MM_CARD_CLOCKING, i, value, lrc = 0x0b;
    if (!n || n > MM_CARD_BODY) return 0;
    memset(bits, 0, MM_CARD_BITS);
    at = mm_card_char(bits, at, 0x0b);
    for (i = 0; i < n; i++) {
        if (body[i] >= '0' && body[i] <= '9') value = body[i] - '0';
        else if (body[i] == '=') value = 0x0d;
        else return 0;
        lrc ^= value;
        at = mm_card_char(bits, at, value);
    }
    lrc ^= 0x0f;
    at = mm_card_char(bits, at, 0x0f);
    at = mm_card_char(bits, at, lrc);
    return at + MM_CARD_CLOCKING;
}

/* "b4761...=2512" -> card `index`. Returns 0 if refused. */
static int mm_card_add(struct mm_card *c, const char *spec)
{
    unsigned char *bits;
    if (c->count == MM_CARD_CARDS || !strchr("bion", spec[0])) return 0;
    bits = c->bits[c->count];
    c->nbits[c->count] = mm_card_encode(spec + 1, bits);
    if (!c->nbits[c->count]) return 0;
    if (spec[0] == 'o') {
        /* Damage two characters' parity going in: one parity error the
         * firmware corrects (24:5E44), two it cannot. Bits 0 of the 2nd
         * and 3rd characters. */
        bits[MM_CARD_CLOCKING + 5] ^= 1;
        bits[MM_CARD_CLOCKING + 10] ^= 1;
    }
    c->how[c->count++] = spec[0];
    return 1;
}

/* Start a swipe of card `index`; 0 if one is already under way. */
static int mm_card_swipe(struct mm_card *c, unsigned index)
{
    if (!c->enabled || index >= c->count) return 0;
    if (c->phase != MM_CARD_IDLE) { c->busy++; return 0; }
    c->current = index;
    c->phase = MM_CARD_ENTERING;
    c->port = MM_CARD_PRESENT;
    c->wait = MM_CARD_SLOT_CLOCKS;
    c->next_bit = 0;
    c->swipes++;
    return 1;
}

/* The bit the head sees next on this stroke, or -1 at the end of it. */
static int mm_card_bit(struct mm_card *c)
{
    unsigned n = c->nbits[c->current], index;
    if (c->next_bit >= n) return -1;
    index = c->phase == MM_CARD_INSERTING ? c->next_bit : n - 1 - c->next_bit;
    c->next_bit++;
    if (c->phase == MM_CARD_WITHDRAWING && c->how[c->current] == 'o')
        /* the damaged bits read correctly coming out */
        return c->bits[c->current][index] ^ (index == MM_CARD_CLOCKING + 5
                                              || index == MM_CARD_CLOCKING + 10);
    return c->bits[c->current][index];
}

/* Whether this stroke strobes at all: no stripe never does, and a card
 * read only going in is pulled out without a flux change the reader
 * takes [policy: it is what makes 'i' a test of the forward read alone]. */
static int mm_card_reads(const struct mm_card *c)
{
    char how = c->how[c->current];
    return how != 'n' && !(c->phase == MM_CARD_WITHDRAWING && how == 'i');
}

/* Advance one phi clock. Returns 1 when the reader strobes /DREQ0 now,
 * with the data bit already on port $80 bit 0. */
static int mm_card_tick(struct mm_card *c)
{
    int bit;
    if (c->phase == MM_CARD_IDLE || --c->wait) return 0;
    switch (c->phase) {
    case MM_CARD_ENTERING:
        c->phase = MM_CARD_INSERTING;
        c->next_bit = 0;
        c->wait = 1;
        return 0;
    case MM_CARD_INSERTING:
    case MM_CARD_WITHDRAWING:
        bit = mm_card_bit(c);
        if (bit >= 0) {
            c->wait = MM_CARD_BIT_CLOCKS;
            if (!mm_card_reads(c)) return 0;    /* no stripe: no flux, no strobe */
            c->port = (c->port & ~MM_CARD_DATA) | (unsigned)bit;
            c->strobes++;
            return 1;
        }
        c->port &= ~MM_CARD_DATA;
        if (c->phase == MM_CARD_INSERTING) {
            c->phase = MM_CARD_IN;
            c->port |= MM_CARD_SEATED;
            c->wait = MM_CARD_DWELL_CLOCKS;
        } else {
            c->phase = MM_CARD_LEAVING;
            c->wait = MM_CARD_SLOT_CLOCKS;
        }
        return 0;
    case MM_CARD_IN:
        c->phase = MM_CARD_WITHDRAWING;
        c->port &= ~MM_CARD_SEATED;
        c->next_bit = 0;
        c->wait = MM_CARD_SLOT_CLOCKS;
        return 0;
    case MM_CARD_LEAVING:
        c->phase = MM_CARD_IDLE;
        c->port = 0;
        return 0;
    }
    return 0;
}

#endif
