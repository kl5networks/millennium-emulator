/* SPDX-License-Identifier: GPL-2.0-or-later
 * Provisional NPA1S01 board decode, inferred from the unchanged firmware.
 * 82C55A mode-0 register behavior follows Renesas FN2969 rev. 11, pp. 4-9.
 * Input wiring and downstream devices remain unresolved, never ready by fiat.
 */
#ifndef MILLENNIUM_BOARD_STARTUP_H
#define MILLENNIUM_BOARD_STARTUP_H

#include <string.h>
#include "millennium_panel.h"

struct mm_board_startup {
    unsigned char mode, latch[3], control_latch;
    unsigned mode_written, power_on_port_reads;
    unsigned writes, reads;
    unsigned eeprom_command, eeprom_bits, eeprom_read_bit, eeprom_reads;
    unsigned eeprom_selected, eeprom_clock, eeprom_reading, eeprom_write_enabled;
    unsigned eeprom_data_bits, eeprom_data, eeprom_program_all, eeprom_busy;
    unsigned eeprom_poll, eeprom_pending_address, eeprom_pending_data, eeprom_writes;
    unsigned short eeprom[64];
    int status80_contacts;
    int status_e0_fixture;
    struct mm_panel panel;
};

enum mm_board_result {
    MM_BOARD_UNKNOWN, MM_BOARD_OK, MM_BOARD_INPUT, MM_BOARD_MODE,
    MM_BOARD_PANEL_READ, MM_BOARD_PANEL_TIMING, MM_BOARD_PANEL_PROTOCOL
};

/* Explicit lab latency: 10 ms at the provisional 6.144 MHz phi clock, the
 * AT93C46 maximum tWP at 5 V. This is a model policy, not measured timing. */
#define MM_EEPROM_PROGRAM_CLOCKS 61440u

static void mm_board_tick(struct mm_board_startup *board, unsigned clocks)
{
    unsigned i;
    mm_panel_tick(&board->panel, clocks);
    if (!board->eeprom_busy) return;
    if (clocks < board->eeprom_busy) { board->eeprom_busy -= clocks; return; }
    board->eeprom_busy = 0;
    if (board->eeprom_program_all)
        for (i = 0; i < 64; i++) board->eeprom[i] = board->eeprom_pending_data;
    else board->eeprom[board->eeprom_pending_address] = board->eeprom_pending_data;
    board->eeprom_writes++;
}

static void mm_board_program(struct mm_board_startup *board, unsigned address,
                              unsigned data, unsigned all)
{
    if (!board->eeprom_write_enabled) return;
    board->eeprom_pending_address = address;
    board->eeprom_pending_data = data;
    board->eeprom_program_all = all;
    board->eeprom_busy = MM_EEPROM_PROGRAM_CLOCKS;
}

static void mm_board_reset(struct mm_board_startup *board)
{
    memset(board, 0, sizeof(*board));
    board->mode = 0x9b; /* All pins input; no assumed external input levels. */
    board->status80_contacts = -1;
    board->status_e0_fixture = -1;
    /* Explicit erased 64x16 Microwire candidate; no factory/configuration data. */
    memset(board->eeprom, 0xff, sizeof(board->eeprom));
}

static unsigned mm_board_input_mask(const struct mm_board_startup *board, unsigned port)
{
    if (port == 0) return board->mode & 0x10 ? 0xff : 0;
    if (port == 1) return board->mode & 2 ? 0xff : 0;
    return (board->mode & 8 ? 0xf0 : 0) | (board->mode & 1 ? 0x0f : 0);
}

/* Candidate wiring from executed firmware: PA0=DI, PB0=SK, latch60 bit6=CS.
 * AT93C46 x16 READ: start=1, opcode=10, six address bits; dummy 0, MSB first.
 * Programming follows EWEN and commits only after modeled tWP. Actual part,
 * board wiring and programming timing still require hardware confirmation.
 */
static enum mm_board_result mm_board_eeprom_lines(struct mm_board_startup *board)
{
    unsigned selected = !!(board->control_latch & 0x40);
    unsigned clock = board->latch[1] & 1;
    unsigned opcode, address;
    if (selected && (mm_board_input_mask(board, 0) || mm_board_input_mask(board, 1)))
        return MM_BOARD_INPUT;
    if (selected != board->eeprom_selected) {
        board->eeprom_bits = board->eeprom_command = board->eeprom_reading = 0;
        board->eeprom_read_bit = 0;
        board->eeprom_data_bits = board->eeprom_data = 0;
        board->eeprom_poll = selected && board->eeprom_busy;
    }
    board->eeprom_selected = selected;
    if (selected && clock && !board->eeprom_clock && !board->eeprom_busy && !board->eeprom_poll) {
        if (board->eeprom_reading) {
            if (++board->eeprom_read_bit == 16) board->eeprom_reads++;
        } else if (board->eeprom_bits < 9) {
            unsigned data = board->latch[0] & 1;
            if (board->eeprom_bits || data) {
                board->eeprom_command = (board->eeprom_command << 1) | data;
                if (++board->eeprom_bits == 9) {
                    opcode = (board->eeprom_command >> 6) & 3;
                    address = board->eeprom_command & 63;
                    if (opcode == 2) board->eeprom_reading = 1;
                    else if (opcode == 3) mm_board_program(board, address, 0xffff, 0);
                    else if (opcode == 0) {
                        if ((address >> 4) == 0 || (address >> 4) == 3)
                            board->eeprom_write_enabled = (address >> 4) == 3;
                        else if ((address >> 4) == 2) mm_board_program(board, 0, 0xffff, 1);
                    }
                }
            }
        } else {
            opcode = (board->eeprom_command >> 6) & 3;
            address = board->eeprom_command & 63;
            if ((opcode == 1 || (opcode == 0 && (address >> 4) == 1))
                    && board->eeprom_data_bits < 16) {
                board->eeprom_data = (board->eeprom_data << 1) | (board->latch[0] & 1);
                if (++board->eeprom_data_bits == 16)
                    mm_board_program(board, address, board->eeprom_data, opcode == 0);
            }
        }
    }
    board->eeprom_clock = clock;
    return MM_BOARD_OK;
}

static enum mm_board_result mm_board_read(struct mm_board_startup *board,
                                         unsigned address, unsigned char *value)
{
    unsigned port = address & 0xff;
    if (port == 0xa0 && board->panel.enabled) {
        /* One read is answered: the status read with AD high (00:6EDB),
         * which 0F:67E2 polls off hook and tests for bit 1 alone; after
         * five in a row with it set it calls 03:5BB2 with 9 [code], which
         * reads as raising alarm 9 [inferred]. The panel here
         * never reports that fault: it reads 00 [inferred: the module's
         * status byte was not obtained by the experiment, so every other
         * bit is a guess and no other read is served]. */
        if (mm_board_input_mask(board, 1) || (board->latch[1] & 0x60) != 0x20) return MM_BOARD_PANEL_READ;
        *value = 0;
        board->panel.status_reads++;
        board->reads++;
        return MM_BOARD_OK;
    }
    /* Explicit whole-byte input hypothesis, never a device ready/ACK model.
     * All eight bits are explicit if a later consumer examines more than 6. */
    if (port == 0xe0 && board->status_e0_fixture >= 0) {
        *value = board->status_e0_fixture;
        board->reads++;
        return MM_BOARD_OK;
    }
    if (port == 0x80) {
        /* DO is available during READ or a real programming busy poll. The
         * upper four contact levels require an explicit fixture outside a
         * transaction. Bits 2..0 are unmodeled and zero in this lab profile. */
        *value = board->status80_contacts < 0 ? 0 : board->status80_contacts << 4;
        if (board->eeprom_selected) {
            if (board->eeprom_poll) {
                if (!board->eeprom_busy) *value |= 8;
            } else if (!board->eeprom_reading || board->eeprom_read_bit > 16) return MM_BOARD_INPUT;
            else if (board->eeprom_read_bit)
                *value |= ((board->eeprom[board->eeprom_command & 63] >> (16 - board->eeprom_read_bit)) & 1) << 3;
        } else if (board->status80_contacts < 0) return MM_BOARD_INPUT;
        board->reads++;
        return MM_BOARD_OK;
    }
    if (port < 0x40 || port > 0x43) return MM_BOARD_UNKNOWN;
    port -= 0x40;
    if (port == 3) *value = board->mode;
    else if (mm_board_input_mask(board, port)) {
        /* MTR 1.7 pulses PB7 (a read-modify-write of port B) before it has
         * written any PPI mode word, so port B is still a power-on input.
         * The value read is only written back to an input-mode latch, which
         * drives no pin, and the first mode word clears every latch. So
         * before that first mode word an input port reads as zero; after it
         * an input port is a real board net and still stops. [inferred] */
        if (board->mode_written || port != 1) return MM_BOARD_INPUT;
        *value = 0;
        board->power_on_port_reads++;
    } else *value = board->latch[port];
    board->reads++;
    return MM_BOARD_OK;
}

static enum mm_board_result mm_board_write(struct mm_board_startup *board,
                                          unsigned address, unsigned char value)
{
    unsigned port = address & 0xff;
    if (port == 0xa0 && board->panel.enabled) {
        enum mm_panel_result result;
        /* Explicit provisional board profile: PB5=AD, PB6=/CS. Both must
         * be outputs. A CPU OUT supplies one byte-boundary write event. */
        if (mm_board_input_mask(board, 1)) return MM_BOARD_INPUT;
        if (board->latch[1] & 0x40) board->panel.ignored_writes++;
        else {
            result = mm_panel_write(&board->panel, !!(board->latch[1] & 0x20), value);
            if (result == MM_PANEL_TIMING) return MM_BOARD_PANEL_TIMING;
            if (result == MM_PANEL_PROTOCOL) return MM_BOARD_PANEL_PROTOCOL;
        }
        board->writes++;
        return MM_BOARD_OK; /* A0 does not change any Microwire line. */
    } else if (port == 0x60) board->control_latch = value;
    else if (port >= 0x40 && port <= 0x43) {
        port -= 0x40;
        if (port < 3) board->latch[port] = value;
        else if (value & 0x80) {
            /* Handshake modes need real devices and separate contracts. */
            if (value & 0x64) return MM_BOARD_MODE;
            board->mode = value;
            board->mode_written = 1;
            memset(board->latch, 0, sizeof(board->latch));
        } else {
            unsigned bit = 1u << ((value >> 1) & 7);
            if (value & 1) board->latch[2] |= bit;
            else board->latch[2] &= ~bit;
        }
    } else return MM_BOARD_UNKNOWN;
    {
        enum mm_board_result result = mm_board_eeprom_lines(board);
        if (result != MM_BOARD_OK) return result;
    }
    board->writes++;
    return MM_BOARD_OK;
}

#endif
