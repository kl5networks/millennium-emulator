/* SPDX-License-Identifier: GPL-2.0-or-later
 * Z80180 external-clock CSI/O symbol model (UM005004 pp.148-156).
 * A symbol call represents a bit at the documented shift/sample boundary.
 * It is not a CKS electrical edge: synchronizer and fractional-phi delays
 * must be supplied by a future board clock/pin model. No default clock/data.
 */
#ifndef MILLENNIUM_CSIO_H
#define MILLENNIUM_CSIO_H

struct mm_csio {
    unsigned remaining;
    unsigned mode;
    unsigned shift;
};

static void mm_csio_reset(struct mm_csio *s)
{
    s->remaining = s->mode = s->shift = 0;
}

static void mm_csio_control(struct mm_csio *s, unsigned old, unsigned value,
                            unsigned data)
{
    unsigned mode = value & 0x30;
    /* Clearing TE/RE aborts immediately; setting an already-set enable bit
     * does not restart a frame. Simultaneous TE/RE is guarded by the adapter. */
    if (mode != (old & 0x30)) {
        s->mode = mode;
        s->remaining = mode ? 8 : 0;
        s->shift = data;
    }
}

static void mm_csio_data_write(struct mm_csio *s, unsigned value)
{
    /* TRDR is not buffered. This also supports this firmware's external-clock
     * ordering: TE first, then TRDR, before any external clock symbol. */
    s->shift = value;
}

static int mm_csio_symbol(struct mm_csio *s, unsigned char *control,
                          unsigned char *data, unsigned sampled_bit)
{
    int transmitted;
    if ((*control & 7) != 7 || !s->remaining || !s->mode) return -1;
    transmitted = s->mode == 0x10 ? s->shift & 1 : -1;
    if (s->mode == 0x20)
        s->shift = (s->shift >> 1) | ((sampled_bit & 1) << 7);
    else s->shift >>= 1;
    if (!--s->remaining) {
        if (s->mode == 0x20) *data = s->shift;
        *control = (*control & ~0x30) | 0x80;
        s->mode = 0;
    }
    return transmitted;
}

#endif
