/* SPDX-License-Identifier: GPL-2.0-or-later
 * Provisional CU20026SCPB-T23A write subset. Protocol: John Warga's
 * unofficial, experimentally derived V1.0 documentation, 2014-12-08,
 * pp. 9-12, 15.
 * No readable status, electrical strobe timing or font ROM is invented.
 */
#ifndef MILLENNIUM_PANEL_H
#define MILLENNIUM_PANEL_H

#include <string.h>

struct mm_panel {
    unsigned enabled, writes, data_writes, command_writes, ignored_writes;
    unsigned cooldown, cursor, cursor_visible, pending, pending_bytes, blink, status_reads;
    unsigned custom_count;
    int brightness;   /* the last 1A level byte, -1 before any */
    unsigned char cells[40], custom_code[2], custom_bitmap[2][5], packet[6];
};

enum mm_panel_result { MM_PANEL_OK, MM_PANEL_TIMING, MM_PANEL_PROTOCOL };

/* Conservative fixed-delay lab policies, rounded up at 6.144 MHz phi.
 * These are acceptance intervals, not measured busy flags or hardware timing.
 */
#define MM_PANEL_POWER_CLOCKS 921600u  /* 150 ms */
#define MM_PANEL_RESET_CLOCKS 614400u  /* 100 ms */
#define MM_PANEL_WRITE_CLOCKS 738u     /* 120 us */
#define MM_PANEL_CURSOR_CLOCKS 246u    /* 40 us */
#define MM_PANEL_CLEAR_CLOCKS 3441u    /* 560 us */

static void mm_panel_reset(struct mm_panel *panel, unsigned cooldown)
{
    /* Bus counters describe the run and survive a software reset. */
    panel->cooldown = cooldown;
    panel->cursor = panel->pending = panel->pending_bytes = panel->custom_count = 0;
    panel->cursor_visible = 1; /* Explicit lab reset policy; not a board input. */
    memset(panel->cells, 0x20, sizeof(panel->cells));
    memset(panel->custom_code, 0, sizeof(panel->custom_code));
    memset(panel->custom_bitmap, 0, sizeof(panel->custom_bitmap));
    memset(panel->packet, 0, sizeof(panel->packet));
    panel->brightness = -1;
    panel->blink = 0;
}

static void mm_panel_tick(struct mm_panel *panel, unsigned clocks)
{
    if (!panel->enabled) return;
    panel->cooldown = clocks < panel->cooldown ? panel->cooldown - clocks : 0;
}

static enum mm_panel_result mm_panel_write(struct mm_panel *panel,
                                          unsigned command, unsigned char value)
{
    unsigned i, slot;
    if (panel->cooldown) return MM_PANEL_TIMING;
    if (command) {
        /* A command interleaved with an incomplete data packet is unresolved.
         * Reset is the supported exception and cancels the packet. */
        if (value == 0x40) mm_panel_reset(panel, MM_PANEL_RESET_CLOCKS);
        else if (panel->pending_bytes || value >= 40) return MM_PANEL_PROTOCOL;
        else { panel->cursor = value; panel->cooldown = MM_PANEL_CURSOR_CLOCKS; }
        panel->command_writes++;
    } else if (panel->pending == 0x1a) {
        /* Brightness level byte. What the firmware sends (menu 274,
         * 09:5EDD: data 1A, then (level - 1) * 16 for keys 1..3) is [code];
         * that the module takes it as a brightness is [inferred]: the
         * experiment did not cover it, so only the four levels a high
         * nibble can carry are accepted, and the cells are untouched. */
        if (value & 0x0f || value > 0x30) return MM_PANEL_PROTOCOL;
        panel->brightness = value;
        panel->pending = panel->pending_bytes = 0;
        panel->data_writes++;
        panel->cooldown = MM_PANEL_WRITE_CLOCKS;
    } else if (panel->pending == 0x16) {
        /* The byte after $16. The firmware's display-mode routine (00:710F,
         * modes $11..$17) sends $16 and then one argument; the arguments
         * seen are $00, $13, $14 and $27 (06:5536..5565) [code]. That it is
         * a cell to blink is [inferred] from the values (0, 19, 20, 39 are
         * the corners of a 2x20 panel), so only a cell 0..39 is accepted,
         * and the cells themselves are untouched. */
        if (value >= 40) return MM_PANEL_PROTOCOL;
        panel->pending = panel->pending_bytes = 0;
        panel->data_writes++;
        panel->cooldown = MM_PANEL_WRITE_CLOCKS;
    } else if (panel->pending_bytes) {
        slot = 6 - panel->pending_bytes;
        if (!slot) {
            /* The experiment reports a redefinition/rotating-buffer bug.
             * Until its exact behavior is verified, support two distinct
             * definitions only. Reject before consuming the disputed code. */
            if (panel->custom_count == 2) return MM_PANEL_PROTOCOL;
            for (i = 0; i < panel->custom_count; i++)
                if (panel->custom_code[i] == value) return MM_PANEL_PROTOCOL;
        }
        panel->packet[slot] = value;
        if (!--panel->pending_bytes) {
            slot = panel->custom_count++;
            panel->custom_code[slot] = panel->packet[0];
            memcpy(panel->custom_bitmap[slot], panel->packet + 1, 5);
            panel->pending = 0;
        }
        panel->data_writes++;
        panel->cooldown = MM_PANEL_WRITE_CLOCKS;
    } else {
        /* Custom definitions take precedence over control and null codes. */
        for (i = 0; i < panel->custom_count; i++)
            if (panel->custom_code[i] == value) break;
        /* A1..FF are the module's built-in extended font. The cell keeps
         * the code; no glyph is invented for it. */
        if (i < panel->custom_count || (value >= 0x20 && value <= 0x7e) || value >= 0xa1) {
            panel->cells[panel->cursor] = value;
            panel->cursor = (panel->cursor + 1) % 40;
        } else if (value == 0x0c) memset(panel->cells, 0x20, sizeof(panel->cells));
        else if (value == 0x0a) panel->cursor = (panel->cursor + 20) % 40;
        else if (value == 0x11) { /* Normal increment mode only in this subset. */ }
        else if (value == 0x13 || value == 0x14) panel->cursor_visible = value == 0x13;
        else if (value == 0x15 || value == 0x17) {
            /* $17 cancels blinking. $15 is a mode with no argument that the
             * INSTALL keypad step sets before echoing keys (09:55B6) [code];
             * that it is a blink mode is [inferred]. Neither changes a cell
             * here: no blinking is rendered. */
            panel->blink = value == 0x15;
        }
        else if (value == 0x16) { panel->pending = 0x16; panel->pending_bytes = 1; }
        else if (value == 0x1b) { panel->pending = 0x1b; panel->pending_bytes = 6; }
        else if (value == 0x1a) { panel->pending = 0x1a; panel->pending_bytes = 1; }
        /* The exact-panel experiment explicitly identifies unlisted codes as
         * null (p. 10). Listed but unimplemented controls stay guarded. */
        else if (value <= 9 || value == 0x0b || (value >= 0x0d && value <= 0x10)
                 || value == 0x18 || value == 0x19 || (value >= 0x1c && value <= 0x1f)) { }
        else if (value >= 0x7f && value <= 0xa0) { /* Documented null range. */ }
        else return MM_PANEL_PROTOCOL;
        panel->data_writes++;
        panel->cooldown = value == 0x0c && i == panel->custom_count
            ? MM_PANEL_CLEAR_CLOCKS : MM_PANEL_WRITE_CLOCKS;
    }
    panel->writes++;
    return MM_PANEL_OK;
}

#endif
