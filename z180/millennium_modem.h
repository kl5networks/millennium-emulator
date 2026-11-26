/* SPDX-License-Identifier: GPL-2.0-or-later
 * The 73K212 modem's serial control bus on the Millennium control board,
 * as the control firmware drives it through PPI port A ($40) with the read
 * data on $E0 bit 4. INFERRED wiring, read from the firmware's own
 * register routines (1.7: 00:697E read, 00:69CC write):
 *
 *   PA0  DATA out, inverted (the routine clears PA0 to send a one)
 *   PA1  EXCLK; a write bit is taken on the falling edge, a read advances
 *        on the falling edge. The write routine sets the bit and raises
 *        EXCLK in one OUT and lowers it in the next, then raises it again
 *        with WR: sampling on the rise loses bit 0 and keeps that last
 *        rise as a ninth bit, which is how the first reading of this bus
 *        came out wrong (every TR value read as C0|d>>1).
 *   PA2  RD, active low
 *   PA3  WR, active low; the rising edge commits the eight bits shifted in
 *   PA4  A0, PA5 A1: register 0..3 (CR0, CR1, DR, TR)
 *   E0.4 DATA in, not inverted
 *
 * The register semantics (CR1 bit 2 resets, DR read-only) follow the SSI
 * 73K212L data sheet; nothing
 * of that model is copied here. No line, carrier or data is produced: DR
 * holds whatever an event, or a streamed run's far end, last set.
 */
#ifndef MILLENNIUM_MODEM_H
#define MILLENNIUM_MODEM_H

#define MM_MODEM_LOG 64u

struct mm_modem {
    unsigned enabled;
    unsigned char reg[4];             /* CR0, CR1, DR, TR */
    unsigned port;                    /* last port A value seen */
    unsigned shift, bits;             /* a write being shifted in */
    unsigned reading, read_value, read_index;
    unsigned reads, writes, resets;
    unsigned short log[MM_MODEM_LOG]; /* writes only: address << 8 | value */
    unsigned log_count;
    unsigned char sent[MM_MODEM_LOG]; /* ASCI1 bytes, toward the manager */
    unsigned sent_count;
};

/* A byte the control board put on ASCI1, the modem's data side [inferred]. */
static void mm_modem_transmit(struct mm_modem *m, unsigned char byte)
{
    m->sent[m->sent_count++ % MM_MODEM_LOG] = byte;
}

static void mm_modem_register_write(struct mm_modem *m, unsigned address, unsigned value)
{
    m->writes++;
    m->log[m->log_count++ % MM_MODEM_LOG] = (unsigned short)(address << 8 | value);
    if (address == 2) return;                     /* DR is read-only */
    m->reg[address] = value;
    if (address == 1 && (value & 4)) {            /* CR1 D2: reset command */
        m->reg[0] = m->reg[1] = m->reg[3] = 0;
        m->resets++;
    }
}

/* The control board wrote port A. */
static void mm_modem_port(struct mm_modem *m, unsigned value)
{
    unsigned old = m->port, address = (value >> 4) & 3;
    m->port = value;
    if (!m->enabled) return;
    if ((old & 4) && !(value & 4)) {              /* RD falls: present the register */
        m->reading = 1;
        m->read_value = m->reg[address];
        m->read_index = 0;
        m->reads++;
    } else if (!(old & 4) && (value & 4)) m->reading = 0;
    if (!m->reading && (old & 2) && !(value & 2)) {
        m->shift = (m->shift >> 1) | ((value & 1) ? 0 : 0x80);
        if (m->bits < 8) m->bits++;
    }
    if (m->reading && (old & 2) && !(value & 2)) m->read_index++;
    if (!(old & 8) && (value & 8) && !m->reading) {   /* WR rises */
        if (m->bits == 8) mm_modem_register_write(m, address, m->shift);
        m->bits = 0;
    }
}

/* $E0 bit 4 as the control board reads it. Idle high [inferred]. */
static unsigned mm_modem_data_line(const struct mm_modem *m)
{
    if (!m->reading) return 0x10;
    return m->read_index < 8 && ((m->read_value >> m->read_index) & 1) ? 0x10 : 0;
}

#endif
