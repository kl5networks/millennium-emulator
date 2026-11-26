/* SPDX-License-Identifier: GPL-2.0-or-later
 * A stand-in for the Millennium's telephony board, on the control board's
 * CSI/O. INFERRED throughout: no image of the telephony board's processor
 * exists in either public archive, so this says what the control firmware
 * sends and waits for, not
 * what a real board does. Opt-in, like every other fixture.
 *
 * Electrical model, all inferred:
 *   latch $60 bit 5  telephony reset, low = held
 *   latch $60 bit 1  raised by the control board after it hears 70
 *   latch $60 bit 7  control strobe; each falling edge moves one byte
 *   port  $E0 bit 6  telephony board wants to send, active low
 * On a strobe the stand-in clocks eight CSI/O symbols. Whether a byte goes
 * in or out is whatever the control board armed (TE or RE): that is the
 * direction the protocol has agreed, so the stand-in follows it.
 */
#ifndef MILLENNIUM_TELEPHONY_H
#define MILLENNIUM_TELEPHONY_H

#include <string.h>

#define MM_TEL_TICK_CLOCKS 61440u      /* 10 ms at 6.144 MHz, one INT2 tick */
#define MM_TEL_ANNOUNCE_CLOCKS (20u * MM_TEL_TICK_CLOCKS) /* reset release -> 70 */
#define MM_TEL_STROBE_CLOCKS 256u      /* strobe edge -> the byte's clocks */
#define MM_TEL_QUEUE 64u
#define MM_TEL_LOG 64u

struct mm_telephony {
    unsigned enabled;
    unsigned latch;                   /* last $60 seen */
    unsigned announce;                /* clocks until 70 is queued */
    unsigned strobe;                  /* clocks until the strobed byte moves */
    unsigned char out[MM_TEL_QUEUE];  /* telephony -> control */
    unsigned out_head, out_count;
    unsigned char frame[160];         /* control -> telephony, being assembled */
    unsigned frame_length;
    unsigned off_hook;                /* the handset, as the stand-in reports it */
    /* Report: what crossed the link. */
    unsigned resets, announces, commands, replies, frames, frame_errors;
    unsigned link_up_queries;         /* status queries answered (cold start: two) */
    unsigned keepalives;
    unsigned char log[MM_TEL_LOG];    /* the last single-byte commands, oldest first */
    unsigned log_count;
};

static void mm_tel_reset(struct mm_telephony *t)
{
    unsigned enabled = t->enabled;
    memset(t, 0, sizeof(*t));
    t->enabled = enabled;
}

static void mm_tel_send(struct mm_telephony *t, const unsigned char *bytes, unsigned n)
{
    unsigned i;
    for (i = 0; i < n && t->out_count < MM_TEL_QUEUE; i++) {
        t->out[(t->out_head + t->out_count) % MM_TEL_QUEUE] = bytes[i];
        t->out_count++;
    }
}

static void mm_tel_send1(struct mm_telephony *t, unsigned char byte)
{
    mm_tel_send(t, &byte, 1);
}

/* $E0 bit 6 as the control board reads it: low while there is a byte to send. */
static unsigned mm_tel_request_line(const struct mm_telephony *t)
{
    return t->out_count ? 0 : 0x40;
}

/* One whole message from the control board. Replies per the protocol
 * page's section 8; anything else needs none (inferred). */
static void mm_tel_command(struct mm_telephony *t, const unsigned char *m, unsigned n)
{
    static const unsigned char error_report[] = {0xc4, 0x04, 0x00, 0xc8};
    static const unsigned char status[] = {0xc0, 0x08, 0, 0, 0, 0, 0, 0xc8};
    t->commands++;
    if (n > 1) {                      /* a framed message: configuration */
        t->frames++;
        if (m[0] == 0xc0) mm_tel_send1(t, 0x74);
        return;
    }
    switch (m[0]) {
    case 0x30: mm_tel_send1(t, t->off_hook ? 0x6e : 0x6c); break;
    case 0x33: mm_tel_send1(t, 0x7a); break;
    case 0x38: t->keepalives++; mm_tel_send(t, error_report, sizeof(error_report)); break;
    case 0x31: t->link_up_queries++; mm_tel_send(t, status, sizeof(status)); break;
    case 0x3a: mm_tel_send1(t, 0x8c); break;
    case 0x3b: mm_tel_send1(t, 0x88); break;
    default: break;
    }
}

/* A byte the control board clocked out to us. */
static void mm_tel_receive(struct mm_telephony *t, unsigned char byte)
{
    unsigned sum, i;
    if (t->frame_length < sizeof(t->frame)) t->frame[t->frame_length] = byte;
    t->frame_length++;
    if (t->frame[0] < 0xc0) {
        t->log[t->log_count++ % MM_TEL_LOG] = byte;
        mm_tel_command(t, t->frame, 1);
        t->frame_length = 0;
        return;
    }
    if (t->frame_length < 2 || t->frame_length < t->frame[1]) return;
    if (t->frame[1] < 3 || t->frame_length > sizeof(t->frame)) {
        t->frame_errors++;
        t->frame_length = 0;
        return;
    }
    /* The control board's own frames carry the sum of the data bytes only,
     * where the frames it accepts also count the opcode and length: the
     * configuration frame it builds ends 78 00 03 7B. [observed, 1.7 and 1.20] */
    for (sum = 0, i = 2; i + 1 < t->frame_length; i++) sum += t->frame[i];
    if ((sum & 0xff) != t->frame[t->frame_length - 1]) t->frame_errors++;
    else mm_tel_command(t, t->frame, t->frame_length);
    t->frame_length = 0;
}

/* The control board wrote latch $60. Returns nonzero when a strobe fell. */
static int mm_tel_latch(struct mm_telephony *t, unsigned value)
{
    unsigned old = t->latch;
    t->latch = value;
    if (!t->enabled) return 0;
    if (!(value & 0x20)) {            /* held in reset */
        if (old & 0x20) t->resets++;
        /* A board in reset forgets what it was saying; counters survive. */
        t->announce = t->strobe = 0;
        t->out_head = t->out_count = t->frame_length = 0;
        return 0;
    }
    if (!(old & 0x20)) t->announce = MM_TEL_ANNOUNCE_CLOCKS;
    if ((value & 2) && !(old & 2) && t->announces) mm_tel_send1(t, 0x72);
    if ((old & 0x80) && !(value & 0x80)) { t->strobe = MM_TEL_STROBE_CLOCKS; return 1; }
    return 0;
}

/* Advance the stand-in's own clock. Returns nonzero when a strobed byte is
 * due to move now; the caller clocks the CSI/O and reports back. */
static int mm_tel_tick(struct mm_telephony *t, unsigned clocks)
{
    int due = 0;
    if (!t->enabled) return 0;
    if (t->announce) {
        if (clocks >= t->announce) {
            t->announce = 0;
            t->announces++;
            mm_tel_send1(t, 0x70);
        } else t->announce -= clocks;
    }
    if (t->strobe) {
        if (clocks >= t->strobe) { t->strobe = 0; due = 1; }
        else t->strobe -= clocks;
    }
    return due;
}

/* The byte the stand-in shifts out when the control board is receiving. */
static unsigned mm_tel_next_out(const struct mm_telephony *t)
{
    return t->out_count ? t->out[t->out_head] : 0xff;
}

static void mm_tel_sent(struct mm_telephony *t)
{
    if (!t->out_count) return;
    t->out_head = (t->out_head + 1) % MM_TEL_QUEUE;
    t->out_count--;
    t->replies++;
}

#endif
