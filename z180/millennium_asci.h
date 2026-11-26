/* SPDX-License-Identifier: GPL-2.0-or-later
 * Base Z80180 status logic; include after z180.h. UM005004 pp. 120-133,
 * 138-140. Physical pin levels are independent of written prescaler bits.
 * No receive producer, electrical sampling or board wiring is supplied.
 */
#ifndef MILLENNIUM_ASCI_H
#define MILLENNIUM_ASCI_H

static unsigned mm_asci_status(struct z180asci_channel *ch)
{
    unsigned status = ch->m_stat;
    /* Keep the underlying TDR occupancy when CTS suppresses visible TDRE.
     * CTS inhibits TDRE, not a frame already in progress (p. 138). */
    if (ch->m_cts && (!ch->m_index || (status & 4))) status &= ~2u;
    if (!ch->m_index && (status & 4)) status &= ~0xf0u;
    return status;
}

static int mm_asci_request(struct z180asci_channel *ch)
{
    unsigned status = mm_asci_status(ch);
    return (status & 3) == 3
        || ((status & 8) && (status & (ch->m_index ? 0xf0 : 0xf4)));
}

static void mm_asci_set_pins(struct z180asci_channel *ch, unsigned cts, unsigned dcd)
{
    ch->m_cts = !!cts;
    if (!ch->m_index) {
        ch->m_dcd = !!dcd;
        if (dcd) {
            ch->m_stat = (ch->m_stat & ~0xf0u) | 4;
            ch->rx_bits_rem = 0;
        }
        /* Low retains the DCD latch until a STAT0 read. That first read
         * returns the old high latch, then releases receive inhibition. */
    }
}

static unsigned mm_asci_read_status(struct z180asci_channel *ch)
{
    unsigned status = mm_asci_status(ch);
    if (!ch->m_index && !ch->m_dcd) ch->m_stat &= ~4u;
    return status;
}
#endif
