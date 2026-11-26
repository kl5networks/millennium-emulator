/* SPDX-License-Identifier: GPL-2.0-or-later
 * Standalone diagnostic adapter for mtdev79/z180emu, never linked into KL5.
 * Stop at unsupported bus access rather than manufacturing a board response.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include "z180.h"
#include "millennium_asci.h"
#include "millennium_board_startup.h"
#include "millennium_telephony.h"
#include "millennium_coin.h"
#include "millennium_modem.h"
#include "millennium_card.h"
#include "millennium_voice.h"

/* Upstream exposes these functions in its C unit, not its public header.
 * Direct register access is used only by original synthetic self-tests. */
struct z180_state;
extern void z180_writecontrol(struct z180_state *, offs_t, UINT8);
extern UINT8 z180_readcontrol(struct z180_state *, offs_t);
extern void clock_timers(struct z180_state *);
extern void handle_io_timers(struct z180_state *, int);
extern int check_interrupts(struct z180_state *);
extern int mm_prt_output(device_t *);
extern int mm_csio_external_symbol(device_t *, unsigned);
extern unsigned mm_csio_remaining(device_t *);
extern int mm_dma0_request(device_t *);

static unsigned char image[262144], ram[32768], testmem[1048576];
static size_t image_size;
static device_t *cpu;
static jmp_buf stopped;
static const char *reason = "budget";
static unsigned limit, attempts, last_pc, fault_address;
static int lab_ram, lab_board, testing;
static struct mm_board_startup board;
static unsigned long long modeled_clocks;
static unsigned timer_interrupts[2];
static unsigned int2_period, int2_countdown, clock_input_events, external2_interrupts;
static int asci0_pin_fixture = -1;
static unsigned asci_interrupts[2];
static struct mm_telephony telephony;
static struct mm_coin coin;
static struct mm_modem modem;
static struct mm_card card;
static unsigned dma0_interrupts;
static int coin_hello = 'A';
/* The voice-prompt hardware, opt-in, two ways. The player (18:54E0,
 * 00:6AC1) sets the bank on PB3-1 and control-latch bit 3, writes a SAMPLE
 * NUMBER to external port C0, and writes the next one each time INT0 asks
 * [code] (millennium_voice.h). `speech=silent` takes every C0 write and
 * never asks again, so a prompt starts and is never heard. `speech=chip`
 * is the uPD7759 [inferred] with the voiceware passed as `voice=PATH`:
 * BUSY lasts as long as the sample does on the chip's clock, and INT0
 * asks for the next. Without either, a C0 write still stops the run. */
static unsigned speech_sink, speech_writes;
static struct mm_voice voice;
static unsigned char voice_rom[MM_VOICE_MAX];
static unsigned int0_interrupts, stream_sent_voice;
static int int0_level;
static unsigned tel_unarmed_strobes;

/* Scheduled inputs: a person at the phone, in modeled milliseconds. */
#define MM_EVENTS 64
static struct { unsigned ms; char kind; unsigned value; } events[MM_EVENTS];
static unsigned event_count, event_next, events_applied;

/* Panel history: each distinct screen once it has held still for 50 ms. */
#define MM_HISTORY 40
static unsigned char history[MM_HISTORY][40], history_seen[40];
static unsigned history_ms[MM_HISTORY], history_count, history_dropped;
static unsigned history_tick, history_still;

/* Logical addresses whose RAM bytes the report shows at the end of a run:
 * firmware state, never ROM contents. */
static unsigned peek[8], peek_count;

static unsigned modeled_ms(void) { return (unsigned)(modeled_clocks / 6144u); }

/* INT0 follows the voice chip's request; set only on a change. */
static void voice_int0(void)
{
    int level = mm_voice_request(&voice);
    if (level == int0_level) return;
    int0_level = level;
    z180_set_irq_line(cpu, Z180_IRQ0, level ? ASSERT_LINE : CLEAR_LINE);
}

/* An IM 1 acknowledge: nothing drives the bus, so the vector is FF (RST 38,
 * which IM 1 takes anyway). Counted, because an INT0 nobody asked for
 * would otherwise run the speech player unseen. */
static int irq_acknowledge(device_t *device, int irqnum)
{
    (void)device;
    if (!irqnum && !testing) int0_interrupts++;
    return 0xff;
}

/* Streaming mode: ASCI1 joined to a far end while the firmware runs.
 *
 * The batch run can only record what the firmware sends; it cannot answer.
 * An install talks to a manager for minutes, and the manager's replies
 * depend on what the phone just said, so the far end has to be live. A
 * descriptor the parent passes (`stream=FD`, a socket) carries tagged
 * messages both ways:
 *
 *   worker -> parent  'D' b      a byte the firmware put on ASCI1
 *                     'W' a v    a 73K212 register write (address, value)
 *                     'C' op     a single-byte telephony command
 *                     'S' 4 40   a panel screen that held still 50 ms: its
 *                                modelled ms (little endian), then its cells
 *                     'Z' 4      modelled milliseconds, little endian, each second
 *                     'H' v      a scheduled hook event: 1 lifted, 0 hung up
 *                     'V' o b n 4  the voice chip (speech=chip only): o is 1
 *                                when sample n of bank b starts, 0 when it
 *                                plays out, 2 when reset cuts it off; then
 *                                modelled ms, little endian
 *   parent -> worker  'R' b      a byte for ASCI1's receiver, at line rate
 *                     'M' v      the 73K212 detect register (DR) becomes v
 *                     'X' op     a raw opcode from the telephony board, as an
 *                                event's xHH (an answer report, for one)
 *                     'E' k v    a scheduled-event kind and its byte value,
 *                                applied now: a person reacting to the panel
 *                                (h, k, s, c, w, b; see apply_event)
 *
 * The modelled clock is paced to the wall clock: the firmware is never
 * allowed ahead of real time, because the far end is a real program with
 * real timeouts (mm_manager waits 10 s for a byte; the firmware waits
 * 10 s for an answer, by its own clock). Unpaced, the emulator runs
 * several times faster than real time and the phone's timers would expire
 * while the manager was still answering. Behind real time is tolerated
 * (a slow host), ahead never is. */
static int stream_fd = -1, stream_profile;
static unsigned char stream_rx[4096];
static unsigned stream_rx_head, stream_rx_count;
static unsigned stream_rx_bytes, stream_overruns, stream_messages_in;
static unsigned stream_sent_tel, stream_sent_modem, stream_sent_history, stream_tick;
static unsigned stream_in_tag, stream_in_kind, stream_lag_ms;
static int event_allowed(char kind, unsigned value);
static void apply_event(char kind, unsigned value);
static struct timespec stream_start;
static void stop(const char *why, unsigned address);

static unsigned wall_ms(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (unsigned)((now.tv_sec - stream_start.tv_sec) * 1000
                      + (now.tv_nsec - stream_start.tv_nsec) / 1000000);
}

static void stream_write(const unsigned char *data, size_t n)
{
    while (n) {
        ssize_t done = write(stream_fd, data, n);
        if (done < 0 && errno == EINTR) continue;
        if (done <= 0) { stream_fd = -1; stop("stream_closed", 0); }
        data += done;
        n -= (size_t)done;
    }
}

static void stream_read(int wait_ms)
{
    struct pollfd p;
    unsigned char buffer[512];
    ssize_t n, i;
    p.fd = stream_fd;
    p.events = POLLIN;
    if (poll(&p, 1, wait_ms) <= 0) return;
    n = read(stream_fd, buffer, sizeof(buffer));
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) return;
    if (n <= 0) { stream_fd = -1; stop("stream_closed", 0); }
    for (i = 0; i < n; i++) {
        if (!stream_in_tag) {
            if (buffer[i] != 'R' && buffer[i] != 'M' && buffer[i] != 'X' && buffer[i] != 'E')
                stop("stream_protocol", buffer[i]);
            stream_in_tag = buffer[i];
            stream_in_kind = 0;
            continue;
        }
        if (stream_in_tag == 'E' && !stream_in_kind) {
            stream_in_kind = buffer[i] ? buffer[i] : 0x100;
            continue;
        }
        stream_messages_in++;
        if (stream_in_tag == 'M') modem.reg[2] = buffer[i];
        else if (stream_in_tag == 'X') mm_tel_send1(&telephony, buffer[i]);
        else if (stream_in_tag == 'E') {
            if (stream_in_kind > 0xff || !event_allowed((char)stream_in_kind, buffer[i]))
                stop("stream_protocol", stream_in_kind);
            apply_event((char)stream_in_kind, buffer[i]);
        }
        else {
            if (stream_rx_count == sizeof(stream_rx)) stop("stream_receive_overflow", 0);
            stream_rx[(stream_rx_head + stream_rx_count++) % sizeof(stream_rx)] = buffer[i];
        }
        stream_in_tag = 0;
    }
}

/* Once per modelled millisecond: send what changed, take what arrived,
 * and hold the firmware back to real time. */
static void stream_service(void)
{
    unsigned char message[48];
    unsigned now, wall;
    while (stream_sent_tel < telephony.log_count) {
        message[0] = 'C';
        message[1] = telephony.log[stream_sent_tel++ % MM_TEL_LOG];
        stream_write(message, 2);
    }
    while (stream_sent_modem < modem.log_count) {
        unsigned entry = modem.log[stream_sent_modem++ % MM_MODEM_LOG];
        message[0] = 'W'; message[1] = entry >> 8; message[2] = entry & 0xff;
        stream_write(message, 3);
    }
    while (stream_sent_history < history_count) {
        unsigned at = history_ms[stream_sent_history % MM_HISTORY];
        message[0] = 'S';
        message[1] = at & 0xff; message[2] = (at >> 8) & 0xff;
        message[3] = (at >> 16) & 0xff; message[4] = at >> 24;
        memcpy(message + 5, history[stream_sent_history++ % MM_HISTORY], 40);
        stream_write(message, 45);
    }
    while (stream_sent_voice < voice.log_count) {
        unsigned k = stream_sent_voice++ % MM_VOICE_LOG, at = voice.log[k].ms;
        message[0] = 'V'; message[1] = voice.log[k].op;
        message[2] = voice.log[k].bank; message[3] = voice.log[k].sample;
        message[4] = at & 0xff; message[5] = (at >> 8) & 0xff;
        message[6] = (at >> 16) & 0xff; message[7] = at >> 24;
        stream_write(message, 8);
    }
    now = modeled_ms();
    if (++stream_tick >= 1000) {
        stream_tick = 0;
        message[0] = 'Z';
        message[1] = now & 0xff; message[2] = (now >> 8) & 0xff;
        message[3] = (now >> 16) & 0xff; message[4] = now >> 24;
        stream_write(message, 5);
    }
    stream_read(0);
    for (;;) {
        wall = wall_ms();
        if (now > wall) { stream_read((int)(now - wall)); continue; }
        break;
    }
    if (wall > now + stream_lag_ms) stream_lag_ms = wall - now;
}

/* Scheduled events are counted in events_applied, which the parent checks
 * against the schedule it passed; streamed ones are stream_messages_in. */
static void apply_event(char kind, unsigned value)
{
    if (kind == 'h') {          /* hook: 1 lifted, 0 hung up */
        telephony.off_hook = value;
        if (stream_fd >= 0) {
            unsigned char message[2];
            message[0] = 'H'; message[1] = value;
            stream_write(message, 2);
        }
        mm_tel_send1(&telephony, value ? 0x62 : 0x60);
    } else if (kind == 'k') {   /* a key: 1..9 are 22..32, then 0 * # [inferred] */
        static const char keys[] = "1234567890*#";
        mm_tel_send1(&telephony, 0x22 + 2 * (unsigned)(strchr(keys, (int)value) - keys));
    } else if (kind == 's') board.status80_contacts = value;   /* port 80 bits 7..4 */
    else if (kind == 'x') mm_tel_send1(&telephony, value);  /* raw opcode */
    else if (kind == 'm') modem.reg[2] = value;              /* 73K212 DR */
    else if (kind == 'c') mm_coin_insert(&coin, value);       /* coin channel */
    else if (kind == 'b') mm_coin_raw(&coin, value);          /* any mechanism byte */
    else if (kind == 'w') mm_card_swipe(&card, value);        /* swipe card N */
}

/* Whether an event may be applied in this run: its kind is known, its
 * value in range, and the stand-in it drives is present. */
static int event_allowed(char kind, unsigned value)
{
    if (!kind || !strchr("hxckswmb", kind)) return 0;
    if (kind == 'h') return value <= 1;
    if (kind == 'k') return value && strchr("1234567890*#", (int)value) != NULL;
    if (kind == 'c') return coin.enabled && value >= 1 && value <= 16;
    if (kind == 'b') return coin.enabled && value <= 0xff;
    if (kind == 's') return value <= 15 && board.status80_contacts >= 0;
    if (kind == 'm') return modem.enabled && value <= 0xff;
    if (kind == 'w') return card.enabled && value < card.count;
    return value <= 0xff;
}

static void history_sample(void)
{
    if (!board.panel.enabled) return;
    if (memcmp(history_seen, board.panel.cells, 40)) {
        memcpy(history_seen, board.panel.cells, 40);
        history_still = 0;
        return;
    }
    if (++history_still != 5) return;
    if (history_count && !memcmp(history[(history_count - 1) % MM_HISTORY], history_seen, 40)) return;
    if (history_count >= MM_HISTORY) history_dropped++;
    memcpy(history[history_count % MM_HISTORY], history_seen, 40);
    history_ms[history_count % MM_HISTORY] = modeled_ms();
    history_count++;
}

/* Clock one byte across the CSI/O in whichever direction the control board
 * armed. Eight symbols back to back: the CKS rate is unknown, and the
 * firmware only looks at the byte once EF is set. [inferred] */
static void telephony_transfer(void)
{
    unsigned mode = cpu_get_state_z180(cpu, Z180_CNTR) & 0x30, i, byte = 0;
    int bit;
    if (mode == 0x20 && telephony.out_count) {
        byte = mm_tel_next_out(&telephony);
        for (i = 0; i < 8; i++) mm_csio_external_symbol(cpu, (byte >> i) & 1);
        mm_tel_sent(&telephony);
    } else if (mode == 0x10) {
        for (i = 0; i < 8; i++) {
            bit = mm_csio_external_symbol(cpu, 1);
            if (bit > 0) byte |= 1u << i;
        }
        mm_tel_receive(&telephony, byte);
    } else tel_unarmed_strobes++;
}

void mm_asci_interrupt(unsigned channel)
{
    if (!testing) asci_interrupts[channel]++;
}

void mm_board_clock(void)
{
    if (!testing) modeled_clocks++;
    if (lab_board) mm_board_tick(&board, 1);
    if (telephony.enabled && mm_tel_tick(&telephony, 1)) telephony_transfer();
    if (coin.enabled) mm_coin_tick(&coin, 1);
    if (voice.enabled && mm_voice_tick(&voice, modeled_ms())) voice_int0();
    if (card.enabled && mm_card_tick(&card)) {
        int latched = testing ? 0 : mm_dma0_request(cpu);
        if (latched > 0) card.latched++;
        else if (!latched) card.lost++;
        else card.overruns++;
    }
    if (lab_board && ++history_tick == MM_TEL_TICK_CLOCKS) {
        history_tick = 0;
        history_sample();
        while (event_next < event_count && events[event_next].ms <= modeled_ms()) {
            apply_event(events[event_next].kind, events[event_next].value);
            event_next++;
            events_applied++;
        }
    }
    if (int2_period && !--int2_countdown) {
        clock_input_events++;
        z180_set_irq_line(cpu, Z180_IRQ2, ASSERT_LINE);
        int2_countdown = int2_period;
    }
    /* Last, so a stream that stops the run leaves this clock complete. */
    if (stream_fd >= 0 && !testing && modeled_clocks % 6144u == 0) stream_service();
}

void mm_external2_interrupt(void)
{
    if (!testing) external2_interrupts++;
}

void mm_dma0_interrupt(void)
{
    if (!testing) dma0_interrupts++;
}

void mm_timer_interrupt(unsigned channel)
{
    if (!testing) timer_interrupts[channel]++;
}
static unsigned serial_count[2], serial_last[2];
static unsigned trace[64], trace_count;
static unsigned tail_trace[64];

static void stop(const char *why, unsigned address)
{
    reason = why;
    fault_address = address;
    longjmp(stopped, 1);
}

static UINT8 mem_read(offs_t address)
{
    if (address >= 1048576) stop("invalid_physical_address", address);
    if (testing) return testmem[address];
    if (lab_ram && address >= 0x40000 && address <= 0x47fff)
        return ram[address - 0x40000];
    if (address < image_size) return image[address];
    stop("unmapped_memory_read", address);
    return 0;
}

static void mem_write(offs_t address, UINT8 value)
{
    if (address >= 1048576) stop("invalid_physical_address", address);
    if (testing) { testmem[address] = value; return; }
    if (lab_ram && address >= 0x40000 && address <= 0x47fff) {
#ifdef MM_DEBUG_RAM
        { static long watch = -2; if (watch == -2) watch = getenv("MMW") ? strtol(getenv("MMW"), 0, 16) : -1;
          if (watch >= 0 && address == (unsigned long)watch) fprintf(stderr, "W %05x=%02x pc %04x at %u ms\n", address, value, last_pc, modeled_ms()); }
#endif
        ram[address - 0x40000] = value;
        return;
    }
    stop(address < image_size ? "rom_write_requires_board_decode" : "unmapped_memory_write", address);
}

static unsigned char test_io_value; /* what a self-test's external read sees */
static void timer_irq_setup(unsigned il, unsigned high);

static UINT8 io_read(offs_t address)
{
    UINT8 value;
    enum mm_board_result result;
    if (testing) return test_io_value;
    if (lab_board) {
        if (card.enabled && (address & 0xff) == 0x42 && board.mode_written
                && mm_board_input_mask(&board, 2) == 0x0f) {
            /* Port C, lower half in: the chip contact (millennium_card.h). */
            card.chip_reads++;
            board.reads++;
            return (board.latch[2] & 0xf0) | MM_CARD_CHIP_IO;
        }
        result = mm_board_read(&board, address, &value);
        if (result == MM_BOARD_OK && telephony.enabled && (address & 0xff) == 0xe0)
            value = (value & ~0x40) | mm_tel_request_line(&telephony);
        if (result == MM_BOARD_OK && modem.enabled && (address & 0xff) == 0xe0)
            value = (value & ~0x10) | mm_modem_data_line(&modem);
        /* The card reader owns port 80 bits 2..0: the two sensors and the
         * data bit, read by the CPU and by DMA0 alike. */
        if (result == MM_BOARD_OK && card.enabled && (address & 0xff) == 0x80)
            value = (value & ~7) | card.port;
        if (result == MM_BOARD_OK) return value;
        if (result == MM_BOARD_INPUT) stop("unresolved_board_input", address);
        if (result == MM_BOARD_PANEL_READ) stop("unsupported_panel_read", address);
    }
    stop("unsupported_external_io_read", address);
    return 0;
}

static void io_write(offs_t address, UINT8 value)
{
    enum mm_board_result result;
    if (lab_board && (speech_sink || voice.enabled) && (address & 0xff) == 0xc0) {
        speech_writes++;
        if (voice.enabled) {
            mm_voice_start(&voice, value, board.latch[1], modeled_ms());
            voice_int0();
        }
        return;
    }
    if (lab_board) {
        result = mm_board_write(&board, address, value);
        if (result == MM_BOARD_OK && (address & 0xff) == 0x60) {
            mm_tel_latch(&telephony, value);
            mm_voice_reset_line(&voice, value, modeled_ms());
            voice_int0();
        }
        /* Port A reaches the modem only while the PPI drives it. */
        if (result == MM_BOARD_OK && (address & 0xfc) == 0x40 && !mm_board_input_mask(&board, 0)) {
#ifdef MM_DEBUG_RAM
            unsigned r0 = modem.reads, w0 = modem.writes;
#endif
            mm_modem_port(&modem, board.latch[0]);
#ifdef MM_DEBUG_RAM
            if (getenv("MMMODEM") && (r0 != modem.reads || w0 != modem.writes))
                fprintf(stderr, "MODEM %u %s %u=%02x pc %04x\n", modeled_ms(), r0 != modem.reads ? "R" : "W",
                        (board.latch[0] >> 4) & 3, r0 != modem.reads ? modem.read_value : modem.reg[(board.latch[0] >> 4) & 3], last_pc);
#endif
        }
        if (result == MM_BOARD_OK) return;
        if (result == MM_BOARD_MODE) stop("unsupported_ppi_mode", address);
        if (result == MM_BOARD_INPUT) stop("unresolved_board_input", address);
        if (result == MM_BOARD_PANEL_TIMING) stop("unsupported_panel_write_timing", address);
        if (result == MM_BOARD_PANEL_PROTOCOL) stop("unsupported_panel_protocol", address);
    }
    stop("unsupported_external_io_write", address);
}

/* Called by the two checked build-time hooks before internal I/O side effects.
 * Register readback is distinct from bus timing and active peripherals.
 */
void mm_internal_access(unsigned address, int writing, unsigned value)
{
    if (testing) return;
    if (lab_board && writing && address == Z180_STAT1 && (value & 4)
        && (cpu_get_state_z180(cpu, Z180_CNTR) & 0x20))
        stop("unsupported_csio_pin_mux", address);
    if (asci0_pin_fixture >= 0) {
        if (address == Z180_STAT0 || address == Z180_CNTLB0) return;
        if (coin.enabled && !writing && address == Z180_RDR0) return;
        if (writing && address == Z180_TDR0) {
            struct z180asci_channel *ch = ((struct z180_device *)cpu)->z180asci->m_chan0;
            /* Monitor only internally clocked, normal 8-bit frames. No RX
             * bytes or remote acknowledgment are produced by the fixture. */
            if (!(ch->m_cntla & 0x20) || !(ch->m_cntla & 4)
                || (ch->m_cntla & 0x80) || (ch->m_cntlb & 0x40)
                || (ch->m_cntlb & 7) == 7)
                stop("unsupported_asci_transmit_mode", address);
            if (!(mm_asci_status(ch) & 2)) stop("unsupported_asci_data_timing", address);
            return;
        }
    }
    /* The modem profile owns ASCI1, the 73K212's data side [inferred]: the
     * firmware may read its status, enable it and transmit. Bytes arrive
     * only in a streamed run, from the far end; CTS1 stays guarded above. */
    if (modem.enabled && lab_board) {
        struct z180asci_channel *ch = ((struct z180_device *)cpu)->z180asci->m_chan1;
        if (address == Z180_STAT1 || address == Z180_CNTLB1 || address == Z180_CNTLA1) return;
        if (!writing && address == Z180_RDR1) return;
        if (writing && address == Z180_TDR1) {
            if (!(ch->m_cntla & 0x20) || (ch->m_cntla & 0x80) || (ch->m_cntlb & 0x40)
                || (ch->m_cntlb & 7) == 7)
                stop("unsupported_asci_transmit_mode", address);
            if (!(mm_asci_status(ch) & 2)) stop("unsupported_asci_data_timing", address);
            return;
        }
    }
    /* Startup-only permission: no incoming bytes, data-register access or
     * serial pin readback. TIE stays guarded; RIE has no receive events.
     * This permits the firmware's configuration, not a serial endpoint. */
    if (lab_board && writing && (address == Z180_CNTLA0 || address == Z180_CNTLA1
        || address == Z180_CNTLB0 || address == Z180_CNTLB1)) return;
    if (lab_board && writing && (address == Z180_STAT0 || address == Z180_STAT1)
        && !(value & 1)) return;
    /* Both channels remain disabled. Status input pins have no verified board
     * connection; do not expose the backend's arbitrary CTS/DCD defaults. */
    if (address == Z180_CNTLA0 || address == Z180_CNTLA1) {
        if (writing && (value & 0x60)) stop("unsupported_asci_enable", address);
        return;
    }
    if (address == Z180_CNTLB0 || address == Z180_CNTLB1) {
        if (!writing) stop("unsupported_asci_pin_read", address);
        return;
    }
    if (address == Z180_STAT0 || address == Z180_STAT1) {
        if (!writing) stop("unsupported_asci_pin_read", address);
        if (value & 9) stop("unsupported_asci_interrupt_enable", address);
        return;
    }
    if (address >= Z180_TDR0 && address <= Z180_RDR1)
        stop("unsupported_asci_data_access", address);
    if (address == Z180_ITC) {
        /* External inputs are inactive unless the explicit INT2 fixture is
         * selected. No INT0 acknowledge or coprocessor reply is supplied. */
        if (!lab_board && writing && (value & 7)) stop("unsupported_external_interrupt_enable", address);
        /* Explicit lab source holds its request until firmware masks INT2.
         * This models the fixture's acknowledgment, not coprocessor data. */
        if (int2_period && writing && !(value & 4))
            z180_set_irq_line(cpu, Z180_IRQ2, CLEAR_LINE);
        return;
    }
    if (address == Z180_DSTAT) {
        /* The card reader's channel: DMA0 from I/O port 80, fixed, into
         * memory, incrementing, cycle steal, edge-sensed /DREQ0, with or
         * without the end interrupt -- exactly what 24:579B and 24:58BF
         * program. Channel 1 and every other shape stay guarded. */
        if (writing && card.enabled && !(value & 0x08) && (value & 0xa0) != 0x80) {
            if ((value & 0x50) != 0x40) return;
            if (cpu_get_state_z180(cpu, Z180_DMODE) == 0x0c
                && (cpu_get_state_z180(cpu, Z180_DCNTL) & 0x04)
                && cpu_get_state_z180(cpu, Z180_SAR0L) == 0x80
                && !cpu_get_state_z180(cpu, Z180_SAR0H) && !cpu_get_state_z180(cpu, Z180_SAR0B))
                return;
        }
        if (writing && ((value & 0x0c)
            || ((value & 0xa0) == 0x80) || ((value & 0x50) == 0x40)))
            stop("unsupported_dma_configuration", address);
        return;
    }
    /* The base Z80180 has 20-bit memory addresses, 16-bit I/O/counts.
     * IAR1B at 2d belongs to enhanced variants and stays guarded. */
    if ((address >= Z180_SAR0L && address <= Z180_IAR1H)
        || address == Z180_BCR1L || address == Z180_BCR1H
        || address == Z180_DMODE) return;
    if (address == Z180_TCR) {
        if (writing && (value & (lab_board ? 0x0c : 0x3f)))
            stop("unsupported_timer_configuration", address);
        return;
    }
    if ((address >= Z180_TMDR0L && address <= Z180_RLDR0H)
        || (address >= Z180_TMDR1L && address <= Z180_RLDR1H)) return;
    if (address == Z180_CNTR) {
        if (writing && (value & 0x30)) {
            /* Startup external-clock transfers may wait for actual CKS input.
             * No board producer is connected yet; CPU clocks cannot finish.
             * Internal-clock modes and simultaneous TE/RE remain guarded. */
            if (!lab_board || (value & 7) != 7 || (value & 0x30) == 0x30)
                stop("unsupported_csio_transfer", address);
            if ((value & 0x20) && (((struct z180_device *)cpu)->z180asci->m_chan1->m_stat & 4))
                stop("unsupported_csio_pin_mux", address);
        }
        return;
    }
    if (address == Z180_TRDR) {
        /* Permit access before the first external symbol and stopped access.
         * Mid-frame access is unsafe/unverified (TRDR unbuffered). The
         * firmware's link reset reads TRDR with RE still armed and then
         * clears RE; with an external clock no bit has moved yet, so that
         * read sees the stopped register. [inferred from UM005004 p.150] */
        if ((cpu_get_state_z180(cpu, Z180_CNTR) & 0x30) && mm_csio_remaining(cpu) < 8)
            stop("unsupported_csio_data_timing", address);
        return;
    }
    if (address == Z180_RCR) {
        if (writing && (value & 0x80)) stop("unsupported_refresh_enable", address);
        return;
    }
    if (address == Z180_OMCR) {
        if (writing && !(value & 0x40)) stop("unsupported_m1_temporary_enable", address);
        return;
    }
    if (address == Z180_IOCR && writing && (value & 0x20))
        stop("unsupported_iostop", address);
    if (address == Z180_CBR || address == Z180_BBR || address == Z180_CBAR
        || address == Z180_IOCR || address == Z180_DCNTL || address == Z180_IL) return;
    stop(writing ? "unsupported_internal_io_write" : "unsupported_internal_io_read", address);
}

#ifdef MM_DEBUG_RAM
static unsigned dbg_pc[16], dbg_hits[16], dbg_n = 99;
#endif

void debugger_instruction_hook(device_t *device, offs_t pc)
{
    (void)device;
#ifdef MM_DEBUG_RAM
    if (dbg_n == 99) {
        char *e = getenv("MMPC");
        dbg_n = 0;
        while (e && *e && dbg_n < 16) { dbg_pc[dbg_n++] = strtoul(e, &e, 16); if (*e == ',') e++; }
    }
    if (dbg_n && !testing) {
        unsigned k; offs_t phys = pc;
        cpu_translate_z180(cpu, AS_PROGRAM, 0, &phys);
        for (k = 0; k < dbg_n; k++) if (dbg_pc[k] == phys) {
            dbg_hits[k]++;
            if (getenv("MMPCV")) fprintf(stderr, "PC %05x at %u ms A=%02x\n", phys, modeled_ms(), (unsigned)cpu_get_state_z180(cpu, Z180_A));
        }
    }
#endif
    if (attempts >= limit) stop("budget", pc);
    last_pc = pc;
    attempts++;
    if (trace_count < 64) trace[trace_count++] = pc;
    tail_trace[(attempts - 1) % 64] = pc;
}

static int receive(device_t *device, int channel)
{
    (void)device;
    /* The far end's bytes, one frame time apart: what the modem would
     * deliver. A byte arriving with the receive FIFO full is an overrun,
     * as on the chip, and is counted. */
    if (channel == 1 && stream_rx_count && !testing) {
        struct z180asci_channel *ch = ((struct z180_device *)cpu)->z180asci->m_chan1;
        int byte = stream_rx[stream_rx_head];
        if ((ch->m_rx_fifo_wp + 1) % M_RX_FIFO_SZ == ch->m_rx_fifo_rp) stream_overruns++;
        stream_rx_head = (stream_rx_head + 1) % sizeof(stream_rx);
        stream_rx_count--;
        stream_rx_bytes++;
        return byte;
    }
    /* Only the coin stand-in produces receive data, and only into an empty
     * data register: it never has to model an overrun. */
    if (!channel && coin.enabled && !testing
        && !(((struct z180_device *)cpu)->z180asci->m_chan0->m_stat & 0x80)) {
        int byte = mm_coin_next(&coin);
#ifdef MM_DEBUG_RAM
        if (byte >= 0 && getenv("MMTX")) fprintf(stderr, "RX %u %02x\n", modeled_ms(), byte);
#endif
        return byte;
    }
#ifdef MM_DEBUG_RAM
    if (!channel && coin.enabled && coin.out_count && !coin.wait && getenv("MMRXB")) {
        static unsigned last; if ((unsigned)(modeled_clocks / 61440) != last) { last = (unsigned)(modeled_clocks / 61440);
        fprintf(stderr, "RXBLOCK %u stat %02x cntla %02x\n", modeled_ms(), ((struct z180_device *)cpu)->z180asci->m_chan0->m_stat, ((struct z180_device *)cpu)->z180asci->m_chan0->m_cntla); }
    }
#endif
    return -1;
}

static void transmit(device_t *device, int channel, UINT8 value)
{
    (void)device;
    if (testing) { serial_count[channel]++; serial_last[channel] = value; return; }
    if (!channel && asci0_pin_fixture >= 0) {
        serial_count[0]++; serial_last[0] = value;
        if (coin.enabled) mm_coin_command(&coin, value);
#ifdef MM_DEBUG_RAM
        if (getenv("MMTX")) fprintf(stderr, "TX %u %02x %c\n", modeled_ms(), value, value >= 32 && value < 127 ? value : 46);
#endif
        return;
    }
    if (channel == 1 && modem.enabled) {
        serial_count[1]++; serial_last[1] = value;
        mm_modem_transmit(&modem, value);
        if (stream_fd >= 0) {
            unsigned char message[2];
            message[0] = 'D'; message[1] = value;
            stream_write(message, 2);
        }
#ifdef MM_DEBUG_RAM
        if (getenv("MMTX")) fprintf(stderr, "TX1 %u %02x %c\n", modeled_ms(), value, value >= 32 && value < 127 ? value : 46);
#endif
        return;
    }
    stop("unsupported_serial_transmit", channel);
}

static struct address_space memory = {mem_read, mem_write, mem_read};
static struct address_space ports = {io_read, io_write, io_read};

static void run(void)
{
    if (!setjmp(stopped)) {
        for (;;) {
            cpu_execute_z180(cpu, 1);
            if (cpu_get_state_z180(cpu, Z180_HALT)) stop("halt_or_sleep", last_pc);
        }
    }
}

static void reset_test(const unsigned char *program, size_t size)
{
    memset(testmem, 0, sizeof(testmem));
    memcpy(testmem, program, size);
    attempts = trace_count = 0;
    limit = 100;
    mm_asci_set_pins(((struct z180_device *)cpu)->z180asci->m_chan0, 0, 0);
    mm_asci_set_pins(((struct z180_device *)cpu)->z180asci->m_chan1, 0, 0);
    cpu_reset_z180(cpu);
    run();
}

static void expect(int condition, const char *name)
{
    if (!condition) { fprintf(stderr, "Z180 contract failed: %s\n", name); exit(1); }
}

static void asci_inactive_contracts(void)
{
    struct z180asci_channel *ch;
    struct z180_device *device = (struct z180_device *)cpu;
    unsigned index, port;
    for (index = 0; index < 2; index++) {
        cpu_reset_z180(cpu);
        ch = index ? device->z180asci->m_chan1 : device->z180asci->m_chan0;
        port = index ? Z180_CNTLA1 : Z180_CNTLA0;
        expect(z180asci_channel_register_read(ch, port) == (index ? 0 : 0x10),
               "ASCI CNTLA reset/RTS bit position");
        expect(ch->m_cntlb == 7 && ch->m_stat == 2, "ASCI inactive reset");
        z180asci_channel_register_write(ch, port, 0x97);
        expect(z180asci_channel_register_read(ch, port) == 0x97,
               "ASCI inactive mode and RTS/CKA control readback");
        /* Inject original synthetic receive state to isolate EFR semantics. */
        ch->m_cntla |= 8;
        ch->m_stat = 0xf2;
        z180asci_channel_register_write(ch, port, 0x18);
        expect(ch->m_stat == 0xf2 && ch->m_cntla == 0x18, "ASCI EFR one preserves errors");
        z180asci_channel_register_write(ch, port, 0x10);
        expect(ch->m_stat == 0x82 && ch->m_cntla == 0x18,
               "ASCI EFR zero clears only errors, preserves received MPB");
        ch->m_stat = 2;
        z180asci_channel_register_write(ch, index ? Z180_STAT1 : Z180_STAT0, 0xf6);
        expect(ch->m_stat == (index ? 6 : 2), "ASCI status read-only mask");
        z180asci_channel_register_write(ch, index ? Z180_CNTLB1 : Z180_CNTLB0, 0x3e);
        expect(ch->m_cntlb == 0x3e, "ASCI inactive prescale/divisor latch");
    }
}

static void dma_register_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned initial, value, expected, enabled, interrupts;
    /* No instruction execution or DMA bus access: isolate write protection.
     * Cover all byte writes against both channels and interrupt combinations. */
    for (initial = 0; initial < 16; initial++) {
        enabled = (initial & 3) << 6;
        interrupts = (initial & 12);
        for (value = 0; value < 256; value++) {
            cpu_reset_z180(cpu);
            z180_writecontrol(state, Z180_DSTAT, enabled | interrupts);
            expected = 0x30 | (value & 12) | (enabled ? 1 : 0);
            expected |= (value & 0x20) ? (enabled & 0x80) : (value & 0x80);
            expected |= (value & 0x10) ? (enabled & 0x40) : (value & 0x40);
            if (((value & 0xa0) == 0x80) || ((value & 0x50) == 0x40)) expected |= 1;
            z180_writecontrol(state, Z180_DSTAT, value);
            expect(cpu_get_state_z180(cpu, Z180_DSTAT) == expected,
                   "DSTAT per-channel DWE protection/DME/read-only/reserved bits");
        }
    }
    cpu_reset_z180(cpu);
}

/* DMA channel 0 as the card reader uses it, against UM005004 pp. 95-104:
 * I/O source fixed, memory destination incrementing, cycle steal, /DREQ0
 * edge-sensed (DCNTL DMS0), BCR0 counting down, DE0 cleared at the end and
 * the end interrupt a level (DIE0 with DE0 clear). Synthetic memory and a
 * synthetic port value; no card, no ROM. */
static void dma0_setup(unsigned count, unsigned dcntl)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    z180_writecontrol(state, Z180_SAR0L, 0x80);
    z180_writecontrol(state, Z180_SAR0H, 0);
    z180_writecontrol(state, Z180_SAR0B, 0);
    z180_writecontrol(state, Z180_DAR0L, 0);
    z180_writecontrol(state, Z180_DAR0H, 0x10);
    z180_writecontrol(state, Z180_DAR0B, 0);
    z180_writecontrol(state, Z180_BCR0L, count & 0xff);
    z180_writecontrol(state, Z180_BCR0H, count >> 8);
    z180_writecontrol(state, Z180_DMODE, 0x0c);
    z180_writecontrol(state, Z180_DCNTL, dcntl);
    z180_writecontrol(state, Z180_DSTAT, 0x64);
}

static void dma0_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned i;
    /* Edge sensing: nothing moves without an edge, one edge moves one byte. */
    timer_irq_setup(0xa0, 0x20);
    for (i = 7; i < 64; i++) testmem[i] = 0;    /* NOPs after the setup's EI */
    testmem[7] = 0;                               /* the EI too: IFF stays clear */
    dma0_setup(3, 0xf4);
    expect(cpu_get_state_z180(cpu, Z180_DSTAT) == 0x75, "DMA0 enabled with DIE0, DWE reads one, DME set");
    for (i = 0; i < 5; i++) cpu_execute_z180(cpu, 1);
    expect(z180_readcontrol(state, Z180_BCR0L) == 3 && !testmem[0x1000],
           "DMA0 edge-sensed channel waits for /DREQ0");
    test_io_value = 0xa5;
    expect(mm_dma0_request(cpu) == 1, "DMA0 edge latched while DE0 is set");
    expect(mm_dma0_request(cpu) == -1, "DMA0 second edge before service is an overrun");
    cpu_execute_z180(cpu, 1);
    expect(testmem[0x1000] == 0xa5 && z180_readcontrol(state, Z180_BCR0L) == 2
           && z180_readcontrol(state, Z180_DAR0L) == 1 && z180_readcontrol(state, Z180_SAR0L) == 0x80,
           "DMA0 one byte per edge: I/O source fixed, memory destination incremented, BCR0 decremented");
    test_io_value = 0x5a;
    for (i = 0; i < 5; i++) cpu_execute_z180(cpu, 1);
    expect(!testmem[0x1001] && z180_readcontrol(state, Z180_BCR0L) == 2,
           "DMA0 edge consumed: no repeat without another edge");
    mm_dma0_request(cpu); cpu_execute_z180(cpu, 1);
    expect(testmem[0x1001] == 0x5a && (cpu_get_state_z180(cpu, Z180_DSTAT) & 0x40),
           "DMA0 still enabled before terminal count");
    mm_dma0_request(cpu); cpu_execute_z180(cpu, 1);
    expect(!z180_readcontrol(state, Z180_BCR0L) && !(cpu_get_state_z180(cpu, Z180_DSTAT) & 0x40),
           "DMA0 terminal count clears DE0");
    expect(mm_dma0_request(cpu) == 0, "DMA0 edge with DE0 clear is not latched");
    cpu_execute_z180(cpu, 1);
    expect(!testmem[0x1003], "DMA0 disabled channel transfers nothing");
    expect(check_interrupts(state) == 0, "DMA0 end request is held while IFF1 is clear");
    /* The end interrupt: a level, delivered once IFF allows, vector +8. */
    timer_irq_setup(0xa0, 0x20);                  /* PC 7 is EI, then NOPs */
    testmem[0x20a8] = 0; testmem[0x20a9] = 3;     /* I=20, IL=A0, DMA0 at +8 */
    dma0_setup(1, 0xf4);
    test_io_value = 0x3c;
    mm_dma0_request(cpu);
    cpu_execute_z180(cpu, 1);                     /* EI, then the transfer */
    expect(testmem[0x1000] == 0x3c && !(cpu_get_state_z180(cpu, Z180_DSTAT) & 0x40),
           "DMA0 single transfer ends the block");
    expect(check_interrupts(state) == 0, "DMA0 end request respects the EI delay");
    cpu_execute_z180(cpu, 1);
    expect(check_interrupts(state) > 0 && cpu_get_state_z180(cpu, Z180_PC) == 0x300,
           "DMA0 end interrupt vectors through I/IL offset 08");
    testmem[0x300] = 0xfb; testmem[0x301] = 0;
    cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
    expect(check_interrupts(state) > 0 && cpu_get_state_z180(cpu, Z180_PC) == 0x300,
           "DMA0 end request is a level: unacknowledged, it requests again");
    z180_writecontrol(state, Z180_DSTAT, 0x30);   /* DIE0 off, DE bits protected */
    testmem[0x300] = 0xfb; testmem[0x301] = 0;
    cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
    expect(check_interrupts(state) == 0, "DMA0 clearing DIE0 withdraws the end request");
    z180_writecontrol(state, Z180_DSTAT, 0x34);   /* DIE0 on with DE0 still clear */
    expect(check_interrupts(state) > 0, "DMA0 DIE0 with DE0 clear requests at once");
    /* Level sensing (DMS0 clear): a held request keeps transferring. */
    timer_irq_setup(0xa0, 0x20);
    testmem[7] = 0;
    dma0_setup(4, 0xf0);
    test_io_value = 0x11;
    mm_dma0_request(cpu);
    for (i = 0; i < 6; i++) cpu_execute_z180(cpu, 1);
    expect(!z180_readcontrol(state, Z180_BCR0L) && testmem[0x1003] == 0x11 && !testmem[0x1004],
           "DMA0 level-sensed request held low repeats to terminal count");
    test_io_value = 0;
    cpu_reset_z180(cpu);
    puts("DMA0 I/O-to-memory edge/level request, count, end and interrupt contracts passed");
}

/* The card stand-in alone: the track-2 bits against a hand-worked oracle,
 * and a swipe's sensor and strobe order. No CPU. */
static void card_contracts(void)
{
    static const unsigned char oracle[] = {   /* ; 1 ? LRC(5) */
        1,1,0,1,0, 1,0,0,0,0, 1,1,1,1,1, 1,0,1,0,1};
    struct mm_card c;
    unsigned char bits[MM_CARD_BITS];
    unsigned i, n, strobes, order[2][64], ports[8], seen = 0, clocks;
    n = mm_card_encode("1", bits);
    expect(n == 44, "card track: 12 clocking zeros, four characters, 12 zeros");
    for (i = 0; i < 44; i++)
        expect(bits[i] == (i >= 12 && i < 32 ? oracle[i - 12] : 0),
               "card track: LSB first, odd parity, sentinels and LRC");
    n = mm_card_encode("4761739001010010=25122011143804400000", bits);
    expect(n == 12 + 40 * 5 + 12, "card track: a 37-character body fills 40 characters");
    expect(!mm_card_encode("12345678901234567890123456789012345678", bits)
           && !mm_card_encode("", bits) && !mm_card_encode("12a", bits),
           "card track: overlong, empty or non-track-2 bodies refused");
    memset(&c, 0, sizeof(c));
    c.enabled = 1;
    expect(mm_card_add(&c, "b1") && mm_card_add(&c, "o1") && !mm_card_add(&c, "x1"),
           "card spec: how letter then body");
    expect(!mm_card_swipe(&c, 2) && mm_card_swipe(&c, 0) && !mm_card_swipe(&c, 0) && c.busy == 1,
           "card swipe: one at a time, known cards only");
    strobes = 0;
    for (clocks = 0; c.phase != MM_CARD_IDLE && clocks < 20000000; clocks++) {
        unsigned before = c.port & 6;
        if (mm_card_tick(&c)) {
            if (strobes < 88) order[strobes / 44][strobes % 44] = c.port & 1;
            expect((c.port & 6) == MM_CARD_PRESENT, "card strobes only between the sensors' changes");
            strobes++;
        }
        if ((c.port & 6) != before && seen < 8) ports[seen++] = c.port & 6;
    }
    expect(strobes == 88 && c.strobes == 88, "card swipe: every bit going in and coming out");
    expect(seen == 3 && ports[0] == 6 && ports[1] == 2 && ports[2] == 0,
           "card swipe: present, seated, unseated, gone");
    n = mm_card_encode("1", bits);
    for (i = 0; i < 44; i++)
        expect(order[0][i] == bits[i] && order[1][i] == bits[43 - i],
               "card swipe: the head meets the stripe forward going in, backward coming out");
    mm_card_swipe(&c, 1);
    strobes = 0;
    for (clocks = 0; c.phase != MM_CARD_IDLE && clocks < 20000000; clocks++)
        if (mm_card_tick(&c)) {
            if (strobes < 88) order[strobes / 44][strobes % 44] = c.port & 1;
            strobes++;
        }
    expect(order[0][17] != bits[17] && order[0][22] != bits[22] && order[1][43 - 17] == bits[17],
           "card 'o': two characters' parity damaged going in, clean coming out");
    puts("Card track-2 encoding and swipe sequence contracts passed");
}

/* The voice chip's timing walk, BUSY and reset, on a synthetic bank whose
 * lengths test_millennium_voice also computes with millennium_voice.py. */
static void voice_contracts(void)
{
    static unsigned char rom[MM_VOICE_BANK];
    struct mm_voice v;
    unsigned at, clocks;
    memset(rom, 0, sizeof(rom));
    memset(&v, 0, sizeof(v));
    rom[0] = 2;
    memcpy(rom + 1, "\x5a\xa5\x69\x55", 4);
    rom[5] = 0; rom[6] = 0x10;   /* sample 0 at 0x20: 256 nibbles at N=2, end */
    at = 0x21; rom[at++] = 0x41; at += 128; rom[at] = 0;
    rom[7] = 0; rom[8] = 0x80;   /* sample 1 at 0x100: silence 4096, 4 nibbles at N=1, end */
    at = 0x101; rom[at++] = 0x03; rom[at++] = 0x80; rom[at++] = 3; at += 2; rom[at] = 0;
    rom[9] = 0; rom[10] = 0x90;  /* sample 2 at 0x120: a block played twice, end */
    at = 0x121; rom[at++] = 0xc1; rom[at++] = 0x80; rom[at++] = 1; at++; rom[at] = 0;
    v.enabled = 1; v.rom = rom; v.size = sizeof(rom);
    expect(mm_voice_clocks(&v, 0, 0) == 218 + 36 + 256 * 8 + 36, "voice: 256-nibble block and end");
    expect(mm_voice_clocks(&v, 0, 1) == 218 + 4096 + 36 + 16 + 36, "voice: silence, counted block");
    expect(mm_voice_clocks(&v, 0, 2) == 218 + 2 * (36 + 8) + 36, "voice: a repeated block");
    expect(mm_voice_clocks(&v, 5, 3) == 218 && v.beyond == 1, "voice: past the last sample, banks mirror");
    expect(!mm_voice_request(&v), "voice: no request in reset");
    mm_voice_start(&v, 0, 0, 0);
    expect(v.ignored == 1 && !v.busy, "voice: a start in reset is ignored");
    mm_voice_reset_line(&v, 8, 0);
    expect(mm_voice_request(&v), "voice: out of reset and idle asks for a sample");
    mm_voice_start(&v, 1, 0x0e, 3);
    expect(v.busy && v.bank == 7 && v.sample == 1 && !mm_voice_request(&v), "voice: PB3-1 bank, BUSY");
    mm_voice_start(&v, 0, 0, 3);
    expect(v.ignored == 2, "voice: a start while busy is ignored");
    for (clocks = 1; !mm_voice_tick(&v, 9); clocks++) ;
    expect(clocks == ((218 + 4096 + 36 + 16 + 36) * 48 + 4) / 5 && mm_voice_request(&v),
           "voice: BUSY for the sample's chip clocks at 9.6 board clocks each");
    mm_voice_start(&v, 0, 0, 10);
    mm_voice_reset_line(&v, 0, 11);
    expect(!v.busy && v.cuts == 1 && !mm_voice_request(&v), "voice: reset cuts a sample off");
    expect(v.log_count == 4 && v.log[0].op == MM_VOICE_START && v.log[1].op == MM_VOICE_STOP
           && v.log[3].op == MM_VOICE_CUT && v.log[1].ms == 9 && v.log[1].bank == 7, "voice: event log");
    puts("Voice chip timing/BUSY/reset contracts passed");
}

static void timer_latch_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned channel, low, reload, tick;
    for (channel = 0; channel < 2; channel++) {
        low = channel ? Z180_TMDR1L : Z180_TMDR0L;
        reload = channel ? Z180_RLDR1L : Z180_RLDR0L;
        cpu_reset_z180(cpu);
        z180_writecontrol(state, reload, 0);
        z180_writecontrol(state, reload + 1, 1);
        z180_writecontrol(state, low, 1);
        z180_writecontrol(state, low + 1, 1);
        z180_writecontrol(state, Z180_TCR, 1 << channel);
        /* Existing core ticks set up an actual 0100 -> 00ff boundary. This
         * isolates coherent reads; it does not accept startup phase/timing. */
        for (tick = 0; tick < 20; tick++) clock_timers(state);
        expect(z180_readcontrol(state, low) == 0, "PRT low byte before rollover");
        for (tick = 0; tick < 20; tick++) clock_timers(state);
        expect(z180_readcontrol(state, low + 1) == 1, "PRT latched high survives rollover");
        expect(z180_readcontrol(state, low + 1) == 0, "PRT high latch consumed once");
    }
    cpu_reset_z180(cpu);
}

static void timer_enable_ack_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned channel, low, reload, phase, tick, byte;
    /* Figure 64: enabling preserves TMDR=4; the free-running /20 divider
     * reaches its next edge after 1..20 input clocks. Both channels and all
     * initial divider phases are checked with a different reload value. */
    for (channel = 0; channel < 2; channel++) {
        low = channel ? Z180_TMDR1L : Z180_TMDR0L;
        reload = channel ? Z180_RLDR1L : Z180_RLDR0L;
        for (phase = 0; phase < 20; phase++) {
            cpu_reset_z180(cpu);
            for (tick = 0; tick < phase; tick++) clock_timers(state);
            z180_writecontrol(state, low, 4);
            z180_writecontrol(state, low + 1, 0);
            z180_writecontrol(state, reload, 3);
            z180_writecontrol(state, reload + 1, 0);
            z180_writecontrol(state, Z180_TCR, 1 << channel);
            expect(z180_readcontrol(state, low) == 4, "PRT enable preserves programmed TMDR");
            for (tick = 0; tick < 19 - phase; tick++) clock_timers(state);
            expect(z180_readcontrol(state, low) == 4, "PRT no decrement before divider edge");
            clock_timers(state);
            expect(z180_readcontrol(state, low) == 3, "PRT first divider edge decrements TMDR");
            for (tick = 0; tick < 60; tick++) clock_timers(state);
            expect(z180_readcontrol(state, low) == 0, "PRT countdown reaches zero");
            for (tick = 0; tick < 20; tick++) clock_timers(state);
            expect(z180_readcontrol(state, low) == 3, "PRT next divider edge reloads RLDR");
        }
    }
    /* Generate flags through the core, then freeze both counters. This tests
     * ordered acknowledgment only; it does not accept the flag-set edge. */
    for (channel = 0; channel < 2; channel++) {
        low = channel ? Z180_TMDR1L : Z180_TMDR0L;
        for (byte = 0; byte < 2; byte++) {
            cpu_reset_z180(cpu);
            z180_writecontrol(state, Z180_TMDR0L, 1);
            z180_writecontrol(state, Z180_TMDR0H, 0);
            z180_writecontrol(state, Z180_TMDR1L, 1);
            z180_writecontrol(state, Z180_TMDR1H, 0);
            z180_writecontrol(state, Z180_TCR, 3);
            for (tick = 0; tick < 40; tick++) clock_timers(state);
            z180_writecontrol(state, Z180_TCR, 0);
            z180_readcontrol(state, low + byte);
            z180_readcontrol(state, low + byte);
            expect(z180_readcontrol(state, Z180_TCR) == 0xc0, "PRT repeated unarmed TMDR cannot clear");
            expect(z180_readcontrol(state, Z180_TCR) == 0xc0, "PRT second TCR preserves flags");
            expect(z180_readcontrol(state, Z180_TCR) == 0xc0, "PRT repeated TCR cannot clear");
            z180_readcontrol(state, low + byte);
            expect(z180_readcontrol(state, Z180_TCR) == (channel ? 0x40 : 0x80),
                   "PRT TCR then either TMDR byte clears only corresponding flag");
            z180_readcontrol(state, channel ? Z180_TMDR0L : Z180_TMDR1L);
            expect(z180_readcontrol(state, Z180_TCR) == 0, "PRT channel acknowledgment independent");
        }
    }
    cpu_reset_z180(cpu);
}

static void timer_flag_edge_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned channel, phase, tick, low, reload, flag, remaining;
    for (channel = 0; channel < 2; channel++) {
        low = channel ? Z180_TMDR1L : Z180_TMDR0L;
        reload = channel ? Z180_RLDR1L : Z180_RLDR0L;
        flag = channel ? 0x80 : 0x40;
        for (phase = 0; phase < 20; phase++) {
            cpu_reset_z180(cpu);
            for (tick = 0; tick < phase; tick++) clock_timers(state);
            z180_writecontrol(state, low, 2);
            z180_writecontrol(state, low + 1, 0);
            z180_writecontrol(state, reload, 3);
            z180_writecontrol(state, reload + 1, 0);
            z180_writecontrol(state, Z180_TCR, 1 << channel);
            for (tick = 0; tick < 39 - phase; tick++) clock_timers(state);
            expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == 0,
                   "PRT flag absent one clock before decrement-to-zero");
            expect(z180_readcontrol(state, low) == 1, "PRT counter one before zero edge");
            clock_timers(state);
            expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == flag,
                   "PRT flag set exactly at decrement-to-zero, other channel clear");
            expect(z180_readcontrol(state, low) == 0, "PRT zero-edge acknowledgment");
            expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == 0,
                   "PRT acknowledged flag clear before reload");
            for (tick = 0; tick < 20; tick++) clock_timers(state);
            expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == 0,
                   "PRT reload alone must not set flag");
            expect(z180_readcontrol(state, low) == 3, "PRT nonzero reload value");
            for (remaining = 2; remaining > 0; remaining--) {
                for (tick = 0; tick < 20; tick++) clock_timers(state);
                expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == 0,
                       "PRT flag remains clear on nonzero countdown");
                expect(z180_readcontrol(state, low) == remaining, "PRT reloaded countdown");
            }
            for (tick = 0; tick < 20; tick++) clock_timers(state);
            expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == flag,
                   "PRT flag rearms at next decrement-to-zero");
            /* No TMDR read: a flag remains sticky across reload and disable. */
            for (tick = 0; tick < 20; tick++) clock_timers(state);
            expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == flag,
                   "PRT uncleared flag survives reload");
            z180_writecontrol(state, Z180_TCR, 0);
            expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == flag,
                   "PRT disabling does not acknowledge flag");
            z180_readcontrol(state, low + 1);
            expect(z180_readcontrol(state, Z180_TCR) == 0,
                   "PRT frozen flag cleared by ordered high-byte acknowledgment");
        }
    }
    cpu_reset_z180(cpu);
}

static void timer_stop_restart_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned channel, low, other, tick, phase, pause, edge;
    for (channel = 0; channel < 2; channel++) {
        low = channel ? Z180_TMDR1L : Z180_TMDR0L;
        other = channel ? Z180_TMDR0L : Z180_TMDR1L;
        /* A running snapshot cannot leak into freely accessed stopped data.
         * Stopping one channel must preserve the other's pending snapshot. */
        cpu_reset_z180(cpu);
        z180_writecontrol(state, low, 0);
        z180_writecontrol(state, low + 1, 1);
        z180_writecontrol(state, other, 0);
        z180_writecontrol(state, other + 1, 2);
        z180_writecontrol(state, Z180_TCR, 3);
        expect(z180_readcontrol(state, low) == 0, "PRT snapshot before stop");
        expect(z180_readcontrol(state, other) == 0, "PRT other channel snapshot");
        for (tick = 0; tick < 20; tick++) clock_timers(state);
        z180_writecontrol(state, Z180_TCR, 1 << (1 - channel));
        expect(z180_readcontrol(state, low + 1) == 0, "PRT stopped high ignores old running snapshot");
        expect(z180_readcontrol(state, other + 1) == 2, "PRT stop preserves other running snapshot");
        expect(z180_readcontrol(state, other + 1) == 1, "PRT other snapshot consumed once");
        /* Stop/reprogram/restart without consuming the old high snapshot. */
        cpu_reset_z180(cpu);
        z180_writecontrol(state, low, 0);
        z180_writecontrol(state, low + 1, 1);
        z180_writecontrol(state, Z180_TCR, 1 << channel);
        z180_readcontrol(state, low);
        z180_writecontrol(state, Z180_TCR, 0);
        z180_writecontrol(state, low + 1, 0x12);
        z180_writecontrol(state, low, 0x34);
        z180_writecontrol(state, Z180_TCR, 1 << channel);
        expect(z180_readcontrol(state, low + 1) == 0x12, "PRT restarted high cannot reuse obsolete snapshot");
        expect(z180_readcontrol(state, low) == 0x34, "PRT restart preserves reprogrammed low");
        for (phase = 0; phase < 20; phase++) {
            for (pause = 0; pause < 40; pause++) {
                cpu_reset_z180(cpu);
                z180_writecontrol(state, low, 4);
                z180_writecontrol(state, low + 1, 0);
                z180_writecontrol(state, Z180_TCR, 1 << channel);
                for (tick = 0; tick < phase; tick++) clock_timers(state);
                z180_writecontrol(state, Z180_TCR, 0);
                for (tick = 0; tick < pause; tick++) clock_timers(state);
                expect(z180_readcontrol(state, low) == 4, "PRT disabled counter frozen");
                expect(z180_readcontrol(state, low + 1) == 0, "PRT disabled high frozen");
                expect(z180_readcontrol(state, Z180_TCR) == 0, "PRT disabled clocks cannot set flag");
                z180_writecontrol(state, Z180_TCR, 1 << channel);
                edge = 20 - ((phase + pause) % 20);
                for (tick = 1; tick < edge; tick++) clock_timers(state);
                expect(z180_readcontrol(state, low) == 4, "PRT restart waits for model divider edge");
                clock_timers(state);
                expect(z180_readcontrol(state, low) == 3, "PRT restart decrements on model edge");
            }
        }
    }
    cpu_reset_z180(cpu);
}

static void timer_transition_model_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned channel, low, reload, flag, byte, tick, before;
    for (channel = 0; channel < 2; channel++) {
        low = channel ? Z180_TMDR1L : Z180_TMDR0L;
        reload = channel ? Z180_RLDR1L : Z180_RLDR0L;
        flag = channel ? 0x80 : 0x40;
        for (byte = 0; byte < 2; byte++) {
            for (before = 0; before < 2; before++) {
                cpu_reset_z180(cpu);
                z180_writecontrol(state, low, 1);
                z180_writecontrol(state, low + 1, 0);
                z180_writecontrol(state, reload, 3);
                z180_writecontrol(state, reload + 1, 0);
                if (before)
                    expect(z180_readcontrol(state, Z180_TCR) == 0, "PRT model arm before flag");
                else
                    z180_readcontrol(state, low + byte);
                z180_writecontrol(state, Z180_TCR, 1 << channel);
                for (tick = 0; tick < 20; tick++) clock_timers(state);
                z180_writecontrol(state, Z180_TCR, 0);
                z180_readcontrol(state, low + byte);
                expect(z180_readcontrol(state, Z180_TCR) == (before ? 0 : flag),
                       "PRT model only prior TCR arms acknowledgment across flag transition");
                z180_readcontrol(state, low + byte);
                expect(z180_readcontrol(state, Z180_TCR) == 0, "PRT model ordered clear after transition");
                z180_readcontrol(state, low + byte); /* consume clear-state arm */
                z180_writecontrol(state, Z180_TCR, 1 << channel);
                for (tick = 0; tick < 80; tick++) clock_timers(state);
                z180_writecontrol(state, Z180_TCR, 0);
                z180_readcontrol(state, low + byte);
                expect(z180_readcontrol(state, Z180_TCR) == flag,
                       "PRT consumed acknowledgment does not clear a later flag");
            }
        }
    }
    cpu_reset_z180(cpu);
}

static void timer_instruction_clock_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    static const unsigned clocks[] = {3, 6, 4}; /* NOP; LD A,n; LD B,A */
    static const unsigned lengths[] = {1, 2, 1};
    unsigned channel, phase, step, tick, elapsed, pc, low;
    memset(testmem, 0, sizeof(testmem));
    for (step = 0; step < 20; step++) {
        testmem[step * 4] = 0x00;
        testmem[step * 4 + 1] = 0x3e;
        testmem[step * 4 + 2] = 0x5a;
        testmem[step * 4 + 3] = 0x47;
    }
    for (channel = 0; channel < 2; channel++) {
        low = channel ? Z180_TMDR1L : Z180_TMDR0L;
        for (phase = 0; phase < 20; phase++) {
            attempts = trace_count = 0;
            limit = 100;
            cpu_reset_z180(cpu);
            z180_writecontrol(state, Z180_DCNTL, 0); /* no inserted waits */
            z180_writecontrol(state, Z180_RCR, 0);   /* refresh disabled */
            z180_writecontrol(state, Z180_ITC, 0);   /* external interrupts disabled */
            for (tick = 0; tick < phase; tick++) clock_timers(state);
            z180_writecontrol(state, low, 40);
            z180_writecontrol(state, low + 1, 0);
            z180_writecontrol(state, Z180_TCR, 1 << channel);
            elapsed = phase;
            pc = 0;
            for (step = 0; step < 60; step++) {
                cpu_execute_z180(cpu, 1); /* instruction completes despite one-clock slice */
                elapsed += clocks[step % 3];
                pc += lengths[step % 3];
                expect(attempts == step + 1 && cpu_get_state_z180(cpu, Z180_PC) == pc,
                       "PRT clock test executes one complete synthetic instruction");
                expect(z180_readcontrol(state, low) == 40 - elapsed / 20,
                       "PRT consumes documented instruction clocks, not slice budget or attempt count");
                expect(z180_readcontrol(state, low + 1) == 0, "PRT instruction-clock high byte");
                expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == 0,
                       "PRT instruction-clock test does not reach zero");
            }
            expect(cpu_get_state_z180(cpu, Z180_A) == 0x5a
                   && cpu_get_state_z180(cpu, Z180_B) == 0x5a,
                   "PRT mixed instruction register results");
        }
    }
    cpu_reset_z180(cpu);
}

static void timer_irq_setup(unsigned il, unsigned high)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned i;
    static const unsigned char setup[] = {0x31,0x00,0xf0,0x3e,0x20,0xed,0x47,0xfb,0x00};
    memset(testmem, 0, sizeof(testmem));
    memcpy(testmem, setup, sizeof(setup));
    testmem[4] = high;
    attempts = trace_count = 0;
    limit = 100;
    mm_asci_set_pins(((struct z180_device *)cpu)->z180asci->m_chan0, 0, 0);
    mm_asci_set_pins(((struct z180_device *)cpu)->z180asci->m_chan1, 0, 0);
    cpu_reset_z180(cpu);
    z180_writecontrol(state, Z180_DCNTL, 0);
    z180_writecontrol(state, Z180_RCR, 0);
    z180_writecontrol(state, Z180_ITC, 0);
    z180_writecontrol(state, Z180_IL, il);
    for (i = 0; i < 3; i++) cpu_execute_z180(cpu, 1);
    expect(cpu_get_state_z180(cpu, Z180_PC) == 7, "PRT IRQ setup PC");
    for (i = 0; i < 2; i++) {
        unsigned address = (high << 8) | il | (i ? 6 : 4);
        testmem[address] = 0;
        testmem[address + 1] = i ? 2 : 1; /* PRT0 -> 0100; PRT1 -> 0200 */
        z180_writecontrol(state, i ? Z180_TMDR1L : Z180_TMDR0L, 1);
        z180_writecontrol(state, i ? Z180_TMDR1H : Z180_TMDR0H, 0);
    }
}

static void timer_irq_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned channel, il, high, tick, action, low, control, mode, following, handler;
    for (channel = 0; channel < 2; channel++) {
        low = channel ? Z180_TMDR1L : Z180_TMDR0L;
        control = (1 << channel) | (0x10 << channel);
        for (il = 0; il < 256; il += 32) {
            for (high = 0x20; high < 0x22; high++) {
                for (mode = 0; mode < 3; mode++) {
                    timer_irq_setup(il, high);
                    following = mode ? 11 : 9;
                    if (mode) {
                        testmem[7] = 0xed;
                        testmem[8] = mode == 1 ? 0x56 : 0x5e;
                        testmem[9] = 0xfb;
                        testmem[10] = 0;
                        cpu_execute_z180(cpu, 1);
                    }
                    z180_writecontrol(state, Z180_TCR, control);
                    for (tick = 0; tick < 20; tick++) clock_timers(state);
                    expect(check_interrupts(state) == 0, "PRT request cannot vector with global IFF clear");
                    cpu_execute_z180(cpu, 1); /* EI */
                    expect(cpu_get_state_z180(cpu, Z180_PC) == following - 1, "PRT EI executes before delivery");
                    expect(check_interrupts(state) == 0, "PRT EI inhibits immediate arbitration");
                    cpu_execute_z180(cpu, 1); /* instruction following EI */
                    expect(cpu_get_state_z180(cpu, Z180_PC) == following, "PRT EI allows following instruction");
                    expect(check_interrupts(state) > 0, "PRT pending request delivered after EI delay");
                    expect(cpu_get_state_z180(cpu, Z180_PC) == (channel ? 0x200 : 0x100),
                           "PRT vector uses I/IL and fixed channel offset in all IM modes");
                    expect(cpu_get_state_z180(cpu, Z180_SP) == 0xeffe
                           && testmem[0xeffe] == following && testmem[0xefff] == 0,
                           "PRT interrupt pushes following PC in little-endian stack");
                    expect(cpu_get_state_z180(cpu, Z180_IFF1) == 0
                           && cpu_get_state_z180(cpu, Z180_IFF2) == 0,
                           "PRT delivery clears global interrupt flip-flops");
                    expect(z180_readcontrol(state, Z180_TCR) & (0x40 << channel),
                           "PRT delivery does not acknowledge TIF");
                    z180_readcontrol(state, low);
                    z180_writecontrol(state, Z180_TCR, 0);
                    handler = channel ? 0x200 : 0x100;
                    testmem[handler] = 0xed;
                    testmem[handler + 1] = 0x4d;
                    cpu_execute_z180(cpu, 1); /* RETI */
                    expect(cpu_get_state_z180(cpu, Z180_PC) == following
                           && cpu_get_state_z180(cpu, Z180_SP) == 0xf000,
                           "PRT RETI restores exact interrupted PC and stack");
                }
            }
        }
        for (action = 0; action < 2; action++) {
            timer_irq_setup(0xa0, 0x20);
            z180_writecontrol(state, Z180_TCR, control);
            for (tick = 0; tick < 20; tick++) clock_timers(state);
            cpu_execute_z180(cpu, 1); /* EI */
            cpu_execute_z180(cpu, 1); /* delayed instruction */
            /* Requests have already been observed while globally inhibited. */
            if (action == 0) {
                z180_readcontrol(state, Z180_TCR);
                z180_readcontrol(state, low);
            } else
                z180_writecontrol(state, Z180_TCR, 1 << channel);
            expect(check_interrupts(state) == 0 && cpu_get_state_z180(cpu, Z180_PC) == 9
                   && cpu_get_state_z180(cpu, Z180_SP) == 0xf000,
                   "PRT clear or TIE disable cancels stale request before delivery");
            if (action) {
                z180_writecontrol(state, Z180_TCR, control);
                expect(check_interrupts(state) > 0, "PRT reenable delivers still-set TIF immediately");
            }
        }
    }
    timer_irq_setup(0xa0, 0x20);
    z180_writecontrol(state, Z180_TCR, 0x33);
    for (tick = 0; tick < 20; tick++) clock_timers(state);
    cpu_execute_z180(cpu, 1);
    cpu_execute_z180(cpu, 1);
    expect(check_interrupts(state) > 0 && cpu_get_state_z180(cpu, Z180_PC) == 0x100,
           "PRT0 takes priority over simultaneous PRT1");
    z180_readcontrol(state, Z180_TCR);
    z180_readcontrol(state, Z180_TMDR0L);
    testmem[0x100] = 0xfb;
    cpu_execute_z180(cpu, 1);
    cpu_execute_z180(cpu, 1);
    expect(check_interrupts(state) > 0 && cpu_get_state_z180(cpu, Z180_PC) == 0x200,
           "PRT1 survives PRT0 service and vectors after reenable");
    cpu_reset_z180(cpu);
}

static void timer_low_power_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned sleep, enabled, tick, count, held_pc;
    for (sleep = 0; sleep < 2; sleep++) {
        timer_irq_setup(0xa0, 0x20);
        testmem[7] = sleep ? 0xed : 0x76;
        if (sleep) testmem[8] = 0x76;
        z180_writecontrol(state, Z180_TMDR0L, 20);
        z180_writecontrol(state, Z180_TCR, 1);
        cpu_execute_z180(cpu, 1);
        held_pc = cpu_get_state_z180(cpu, Z180_PC);
        count = z180_readcontrol(state, Z180_TMDR0L);
        for (tick = 0; tick < 20; tick++) cpu_execute_z180(cpu, 1);
        expect(z180_readcontrol(state, Z180_TMDR0L) == count - 3,
               "PRT continues while HALT or SLP holds instruction execution");
        expect(cpu_get_state_z180(cpu, Z180_PC) == held_pc
               && cpu_get_state_z180(cpu, Z180_HALT) == (sleep ? 2 : 1),
               "PRT individually masked source does not wake low-power state");
        for (enabled = 0; enabled < 2; enabled++) {
            timer_irq_setup(0xa0, 0x20);
            testmem[7] = sleep ? 0xed : 0x76;
            if (sleep) testmem[8] = 0x76;
            if (enabled) {
                testmem[7] = 0xfb;
                testmem[8] = 0;
                testmem[9] = sleep ? 0xed : 0x76;
                if (sleep) testmem[10] = 0x76;
                cpu_execute_z180(cpu, 1);
                cpu_execute_z180(cpu, 1);
            }
            cpu_execute_z180(cpu, 1);
            z180_writecontrol(state, Z180_TCR, 0x11);
            for (tick = 0; tick < 20; tick++) clock_timers(state);
            if (enabled) {
                expect(check_interrupts(state) > 0
                       && cpu_get_state_z180(cpu, Z180_PC) == 0x100,
                       "PRT globally enabled interrupt wakes HALT and SLP into vector");
                expect(testmem[0xeffe] == (sleep ? 11 : 10),
                       "PRT low-power interrupt saves exact following instruction PC");
            } else {
                expect(check_interrupts(state) == 0, "PRT global mask prevents vector entry");
                expect(cpu_get_state_z180(cpu, Z180_HALT) == (sleep ? 0 : 1),
                       "PRT global-mask wake differs between SLP and HALT");
                if (sleep) {
                    expect(cpu_get_state_z180(cpu, Z180_PC) == 9,
                           "PRT SLP wake resumes following instruction without skip");
                    expect(cpu_get_state_z180(cpu, Z180_SP) == 0xf000,
                           "PRT masked SLP wake does not push stack");
                }
            }
        }
    }
    cpu_reset_z180(cpu);
}

static unsigned test_mmu_address(unsigned logical, unsigned bank, unsigned common)
{
    unsigned offset = logical < 0x4000 ? 0 : logical < 0x8000 ? bank << 12 : common << 12;
    return (logical + offset) & 0xfffff;
}

static void timer_mmu_boundary_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    static const unsigned pages[] = {0x3f, 0x40, 0x80, 0xff};
    static const unsigned stacks[] = {0x4001, 0x8001, 1};
    unsigned channel, page, stack, wrap, tick, vector, physical, low, high, saved_sp;
    unsigned bank, common;
    for (channel = 0; channel < 2; channel++)
        for (page = 0; page < 4; page++)
            for (stack = 0; stack < 3; stack++)
                for (wrap = 0; wrap < 2; wrap++) {
                    bank = wrap ? 0xf8 : 0x20;
                    common = wrap ? 0xf8 : 0x40;
                    timer_irq_setup(0xa0, pages[page]);
                    testmem[7] = 0x31;
                    testmem[8] = stacks[stack] & 255;
                    testmem[9] = stacks[stack] >> 8;
                    testmem[10] = 0xfb;
                    testmem[11] = 0;
                    cpu_execute_z180(cpu, 1);
                    z180_writecontrol(state, Z180_CBAR, 0x84);
                    z180_writecontrol(state, Z180_BBR, bank);
                    z180_writecontrol(state, Z180_CBR, common);
                    vector = (pages[page] << 8) | 0xa0 | (channel ? 6 : 4);
                    physical = test_mmu_address(vector, bank, common);
                    testmem[physical] = 0;
                    testmem[physical + 1] = 1;
                    low = test_mmu_address((stacks[stack] - 2) & 0xffff, bank, common);
                    high = test_mmu_address((stacks[stack] - 1) & 0xffff, bank, common);
                    testmem[low] = testmem[high] = 0xcc;
                    z180_writecontrol(state, Z180_TCR, (1 << channel) | (0x10 << channel));
                    for (tick = 0; tick < 20; tick++) clock_timers(state);
                    cpu_execute_z180(cpu, 1); /* EI */
                    cpu_execute_z180(cpu, 1);
                    expect(check_interrupts(state) > 0 && cpu_get_state_z180(cpu, Z180_PC) == 0x100,
                           "PRT vector read uses independent MMU region/wrap mapping");
                    saved_sp = (stacks[stack] - 2) & 0xffff;
                    expect(cpu_get_state_z180(cpu, Z180_SP) == saved_sp
                           && testmem[low] == 12 && testmem[high] == 0,
                           "PRT stack bytes remap independently across bank/common/16-bit wrap");
                    z180_readcontrol(state, Z180_TCR);
                    z180_readcontrol(state, channel ? Z180_TMDR1L : Z180_TMDR0L);
                    z180_writecontrol(state, Z180_TCR, 0);
                    testmem[0x100] = 0xed;
                    testmem[0x101] = 0x4d;
                    cpu_execute_z180(cpu, 1);
                    expect(cpu_get_state_z180(cpu, Z180_PC) == 12
                           && cpu_get_state_z180(cpu, Z180_SP) == stacks[stack],
                           "PRT RETI remaps split stack and restores wrapped SP");
                }
    cpu_reset_z180(cpu);
}

static void timer_irq_clock_model_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned phase, tick;
    for (phase = 0; phase < 20; phase++) {
        timer_irq_setup(0xa0, 0x20);
        cpu_execute_z180(cpu, 1);
        cpu_execute_z180(cpu, 1); /* setup + EI + NOP = 27 clocks */
        z180_writecontrol(state, Z180_TCR, 1);
        for (tick = 0; tick < 20 + (phase + 20 - 7) % 20; tick++) clock_timers(state);
        z180_writecontrol(state, Z180_TMDR1L, 10);
        z180_writecontrol(state, Z180_TCR, 0x12);
        cpu_execute_z180(cpu, 1);
        expect(cpu_get_state_z180(cpu, Z180_PC) == 0x101,
               "PRT execute slice runs entry and first handler NOP in current model");
        expect(z180_readcontrol(state, Z180_TMDR1L) == 10 - (phase + 19) / 20,
               "PRT model advances 16 entry plus 3 handler clocks");
        z180_readcontrol(state, Z180_TCR);
        z180_readcontrol(state, Z180_TMDR0L);
        testmem[0x101] = 0xed;
        testmem[0x102] = 0x4d;
        cpu_execute_z180(cpu, 1);
        expect(cpu_get_state_z180(cpu, Z180_PC) == 9,
               "PRT timing RETI restores interrupted PC");
        expect(z180_readcontrol(state, Z180_TMDR1L) == 10 - (phase + 31) / 20,
               "PRT model accounts for 12-clock R0/R1 RETI");
    }
    cpu_reset_z180(cpu);
}

static void timer_output_model_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned forced, tick;
    cpu_reset_z180(cpu);
    expect(mm_prt_output(cpu) == -2, "PRT reset selects address function");
    z180_writecontrol(state, Z180_TCR, 4);
    expect(mm_prt_output(cpu) == -1, "PRT initial toggle baseline explicitly unknown");
    for (forced = 0; forced < 2; forced++) {
        cpu_reset_z180(cpu);
        z180_writecontrol(state, Z180_TCR, forced ? 12 : 8);
        expect(mm_prt_output(cpu) == forced, "PRT output forced level");
        z180_writecontrol(state, Z180_TMDR0L, 1);
        z180_writecontrol(state, Z180_TMDR0H, 0);
        z180_writecontrol(state, Z180_TMDR1L, 1);
        z180_writecontrol(state, Z180_TMDR1H, 0);
        z180_writecontrol(state, Z180_RLDR1L, 2);
        z180_writecontrol(state, Z180_RLDR1H, 0);
        z180_writecontrol(state, Z180_TCR, 5); /* toggle; only channel 0 counting */
        for (tick = 0; tick < 20; tick++) clock_timers(state);
        expect(mm_prt_output(cpu) == forced, "PRT0 zero cannot toggle PRT1 output");
        z180_writecontrol(state, Z180_TCR, 6);
        for (tick = 0; tick < 19; tick++) clock_timers(state);
        expect(mm_prt_output(cpu) == forced, "PRT output unchanged before zero edge");
        clock_timers(state);
        expect(mm_prt_output(cpu) == (forced ^ 1), "PRT1 zero toggles known baseline");
        for (tick = 0; tick < 20; tick++) clock_timers(state);
        expect(mm_prt_output(cpu) == (forced ^ 1), "PRT reload does not toggle output");
        for (tick = 0; tick < 40; tick++) clock_timers(state);
        expect(mm_prt_output(cpu) == forced, "PRT next zero toggles output again");
        z180_writecontrol(state, Z180_TCR, 4);
        for (tick = 0; tick < 40; tick++) clock_timers(state);
        expect(mm_prt_output(cpu) == forced, "PRT stopped channel cannot toggle output");
        z180_writecontrol(state, Z180_TCR, forced ? 14 : 10);
        for (tick = 0; tick < 100; tick++) clock_timers(state);
        expect(mm_prt_output(cpu) == forced, "PRT forced mode survives countdown");
        z180_writecontrol(state, Z180_TCR, 0);
        expect(mm_prt_output(cpu) == -2, "PRT address mode hides timer output");
    }
    cpu_reset_z180(cpu);
}

/* Logical interrupt inputs in isolated synthetic memory; no board pins. */
static void interrupt_interaction_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned action, tick, enabled, sleep, line;
    for (action = 0; action < 3; action++) {
        timer_irq_setup(0xa0, 0x20);
        testmem[0x20a0] = 0; testmem[0x20a1] = 3;
        testmem[0x20a2] = 0; testmem[0x20a3] = 4;
        z180_writecontrol(state, Z180_ITC, 6);
        z180_writecontrol(state, Z180_TCR, 0x33);
        for (tick = 0; tick < 20; tick++) clock_timers(state);
        z180_set_irq_line(cpu, 1, ASSERT_LINE);
        z180_set_irq_line(cpu, 2, ASSERT_LINE);
        cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
        expect(check_interrupts(state) > 0 && cpu_get_state_z180(cpu, Z180_PC) == 0x300,
               "INT1 wins over INT2 and both PRT requests");
        expect((z180_readcontrol(state, Z180_TCR) & 0xc0) == 0xc0,
               "External interrupt leaves both timer flags pending");
        z180_set_irq_line(cpu, 1, CLEAR_LINE);
        if (action == 1) z180_set_irq_line(cpu, 2, CLEAR_LINE);
        if (action == 2) z180_writecontrol(state, Z180_ITC, 0);
        testmem[0x300] = 0xfb; testmem[0x301] = 0;
        cpu_execute_z180(cpu, 1);
        expect(check_interrupts(state) == 0, "Nested external request respects EI delay");
        cpu_execute_z180(cpu, 1);
        expect(check_interrupts(state) > 0
               && cpu_get_state_z180(cpu, Z180_PC) == (action ? 0x100 : 0x400),
               "Released or masked INT2 cancels stale request before timer arbitration");
        if (!action) {
            z180_set_irq_line(cpu, 2, CLEAR_LINE);
            testmem[0x400] = 0xfb; testmem[0x401] = 0;
            cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
            expect(check_interrupts(state) > 0 && cpu_get_state_z180(cpu, Z180_PC) == 0x100,
                   "PRT0 follows higher-priority external requests");
        }
        expect(cpu_get_state_z180(cpu, Z180_SP) == (action ? 0xeffc : 0xeffa),
               "Nested external and timer entries each push one return address");
        z180_readcontrol(state, Z180_TCR); z180_readcontrol(state, Z180_TMDR0L);
        testmem[0x100] = 0xfb; testmem[0x101] = 0;
        cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
        expect(check_interrupts(state) > 0 && cpu_get_state_z180(cpu, Z180_PC) == 0x200,
               "PRT1 remains eligible after external and PRT0 nesting");
    }
    for (line = 1; line < 3; line++) {
        for (sleep = 0; sleep < 2; sleep++) {
            for (enabled = 0; enabled < 2; enabled++) {
                timer_irq_setup(0xa0, 0x20);
                testmem[0x20a0 + (line - 1) * 2] = 0;
                testmem[0x20a1 + (line - 1) * 2] = 3;
                if (enabled) { cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1); }
                testmem[enabled ? 9 : 7] = sleep ? 0xed : 0x76;
                if (sleep) testmem[enabled ? 10 : 8] = 0x76;
                cpu_execute_z180(cpu, 1);
                z180_set_irq_line(cpu, line, ASSERT_LINE);
                expect(check_interrupts(state) == 0
                       && cpu_get_state_z180(cpu, Z180_HALT) == (sleep ? 2 : 1),
                       "Individually masked external input cannot wake low-power state");
                z180_writecontrol(state, Z180_ITC, 1 << line);
                if (enabled) {
                    expect(check_interrupts(state) > 0 && cpu_get_state_z180(cpu, Z180_PC) == 0x300,
                           "Enabled external request vectors from HALT and SLP");
                    expect(testmem[0xeffe] == (sleep ? 11 : 10),
                           "External wake saves exact following PC");
                } else {
                    expect(check_interrupts(state) == 0
                           && cpu_get_state_z180(cpu, Z180_HALT) == (sleep ? 0 : 1),
                           "Global mask permits external SLP wake but retains HALT");
                    if (sleep) expect(cpu_get_state_z180(cpu, Z180_PC) == 9
                                      && cpu_get_state_z180(cpu, Z180_SP) == 0xf000,
                                      "Masked external SLP wake neither skips nor pushes");
                }
            }
        }
    }
    /* NMI entry/RETN uses original executed instructions, not direct delivery. */
    for (enabled = 0; enabled < 2; enabled++) {
        timer_irq_setup(0xa0, 0x20);
        if (enabled) { cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1); }
        z180_writecontrol(state, Z180_TCR, 0x11);
        for (tick = 0; tick < 20; tick++) clock_timers(state);
        testmem[0x66] = 0xed; testmem[0x67] = 0x45; /* RETN */
        z180_set_irq_line(cpu, INPUT_LINE_NMI, ASSERT_LINE);
        cpu_execute_z180(cpu, 1);
        expect(cpu_get_state_z180(cpu, Z180_PC) == 0x66
               && cpu_get_state_z180(cpu, Z180_SP) == 0xeffe,
               "NMI preempts pending timer regardless of global mask");
        expect(testmem[0xeffe] == (enabled ? 9 : 7)
               && cpu_get_state_z180(cpu, Z180_IFF1) == 0
               && cpu_get_state_z180(cpu, Z180_IFF2) == enabled,
               "NMI saves PC and pre-entry interrupt enable state");
        expect(z180_readcontrol(state, Z180_TCR) & 0x40,
               "NMI leaves timer flag pending");
        cpu_execute_z180(cpu, 1);
        expect(cpu_get_state_z180(cpu, Z180_PC) == (enabled ? 9 : 7)
               && cpu_get_state_z180(cpu, Z180_SP) == 0xf000
               && cpu_get_state_z180(cpu, Z180_IFF1) == enabled,
               "RETN restores exact PC stack and interrupt enable state");
        if (enabled) expect(check_interrupts(state) > 0
                            && cpu_get_state_z180(cpu, Z180_PC) == 0x100,
                            "Timer survives NMI and delivers after RETN");
        z180_writecontrol(state, Z180_TCR, 0);
        cpu_execute_z180(cpu, 1);
        expect(cpu_get_state_z180(cpu, Z180_PC) != 0x66,
               "Held NMI assertion does not retrigger without another edge");
        z180_set_irq_line(cpu, INPUT_LINE_NMI, CLEAR_LINE);
        z180_set_irq_line(cpu, INPUT_LINE_NMI, ASSERT_LINE);
        cpu_execute_z180(cpu, 1);
        expect(cpu_get_state_z180(cpu, Z180_PC) == 0x66,
               "Released then asserted NMI triggers a new entry");
    }
    for (sleep = 0; sleep < 2; sleep++) {
        for (enabled = 0; enabled < 2; enabled++) {
            timer_irq_setup(0xa0, 0x20);
            if (enabled) { cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1); }
            testmem[enabled ? 9 : 7] = sleep ? 0xed : 0x76;
            if (sleep) testmem[enabled ? 10 : 8] = 0x76;
            testmem[0x66] = 0xed; testmem[0x67] = 0x45;
            cpu_execute_z180(cpu, 1);
            z180_set_irq_line(cpu, INPUT_LINE_NMI, ASSERT_LINE);
            cpu_execute_z180(cpu, 1);
            expect(cpu_get_state_z180(cpu, Z180_PC) == 0x66
                   && cpu_get_state_z180(cpu, Z180_HALT) == 0
                   && testmem[0xeffe] == (enabled ? (sleep ? 11 : 10) : (sleep ? 9 : 8)),
                   "NMI wakes HALT or SLP and saves following PC regardless of IFF");
            cpu_execute_z180(cpu, 1);
            expect(cpu_get_state_z180(cpu, Z180_PC) == (enabled ? (sleep ? 11 : 10) : (sleep ? 9 : 8))
                   && cpu_get_state_z180(cpu, Z180_IFF1) == enabled,
                   "NMI RETN resumes following low-power instruction with original mask");
        }
    }
    timer_irq_setup(0xa0, 0x20);
    cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
    z180_writecontrol(state, Z180_TCR, 0x11);
    for (tick = 0; tick < 20; tick++) clock_timers(state);
    expect(check_interrupts(state) > 0, "Nested NMI setup enters timer ISR");
    z180_writecontrol(state, Z180_TCR, 0);
    testmem[0x100] = 0xfb; testmem[0x101] = 0;
    testmem[0x102] = 0xed; testmem[0x103] = 0x4d; /* RETI */
    testmem[0x66] = 0xed; testmem[0x67] = 0x45;
    cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
    z180_set_irq_line(cpu, INPUT_LINE_NMI, ASSERT_LINE);
    cpu_execute_z180(cpu, 1);
    expect(cpu_get_state_z180(cpu, Z180_SP) == 0xeffc
           && testmem[0xeffc] == 2 && testmem[0xeffd] == 1,
           "NMI nested in timer ISR saves handler return PC");
    cpu_execute_z180(cpu, 1);
    expect(cpu_get_state_z180(cpu, Z180_PC) == 0x102
           && cpu_get_state_z180(cpu, Z180_SP) == 0xeffe,
           "Nested RETN unwinds only NMI frame");
    cpu_execute_z180(cpu, 1);
    expect(cpu_get_state_z180(cpu, Z180_PC) == 9
           && cpu_get_state_z180(cpu, Z180_SP) == 0xf000,
           "Timer RETI unwinds original frame after nested NMI");
    cpu_reset_z180(cpu);
}

static void asci_clock_contracts(void)
{
    /* Independent base-Z80180 oracle: UM005004 table 19, phi-clock periods.
     * No backend divisor/rate helper supplies the expected values. */
    static const unsigned periods[2][7] = {
        {160,320,640,1280,2560,5120,10240},
        {480,960,1920,3840,7680,15360,30720}
    };
    static const unsigned clocks[] = {3,6,4}; /* NOP, LD A,n, LD B,A */
    static const unsigned lengths[] = {1,2,1};
    struct z180_device *device = (struct z180_device *)cpu;
    struct z180_state *state = device->m_token;
    struct z180asci_channel *ch, *other;
    unsigned channel, ps, dr, ss, period, mode, odd, bits, tick, elapsed, pc, step;
    for (channel = 0; channel < 2; channel++) {
        ch = channel ? device->z180asci->m_chan1 : device->z180asci->m_chan0;
        other = channel ? device->z180asci->m_chan0 : device->z180asci->m_chan1;
        for (ps = 0; ps < 2; ps++) for (dr = 0; dr < 2; dr++) for (ss = 0; ss < 7; ss++) {
            cpu_reset_z180(cpu);
            memset(serial_count, 0, sizeof(serial_count));
            period = periods[ps][ss] * (dr ? 4 : 1);
            z180_writecontrol(state, Z180_CNTLB0 + channel, (ps ? 32 : 0) | (dr ? 8 : 0) | ss);
            z180_writecontrol(state, Z180_CNTLA0 + channel, 0x24); /* 8N1, TE */
            expect(ch->m_brg_period == period && ch->m_brg_timer == period
                   && ch->m_brg_rate == 6144000 / period,
                   "ASCI base documented bit period/rate, including wide divisor");
            z180_writecontrol(state, Z180_TDR0 + channel, 0xa5);
            expect(ch->tx_bits_rem == 10 && (ch->m_stat & 2), "ASCI starts 8N1 frame and frees TDR");
            handle_io_timers(state, period - 1);
            expect(ch->tx_bits_rem == 10 && !serial_count[channel], "ASCI no bit before full period");
            handle_io_timers(state, 1);
            expect(ch->tx_bits_rem == 9, "ASCI bit at exact full-period edge");
            handle_io_timers(state, 8 * period);
            expect(ch->tx_bits_rem == 1 && !serial_count[channel], "ASCI no callback before final stop duration");
            handle_io_timers(state, period - 1);
            expect(!serial_count[channel], "ASCI no early frame callback");
            handle_io_timers(state, 1);
            expect(serial_count[channel] == 1 && serial_last[channel] == 0xa5
                   && !ch->tx_bits_rem && !serial_count[1 - channel] && !other->tx_bits_rem,
                   "ASCI exact frame callback and channel isolation");
        }
        for (mode = 0; mode < 8; mode++) for (odd = 0; odd < 2; odd++) {
            cpu_reset_z180(cpu);
            memset(serial_count, 0, sizeof(serial_count));
            z180_writecontrol(state, Z180_CNTLB0 + channel, odd ? 16 : 0);
            z180_writecontrol(state, Z180_CNTLA0 + channel, 0x20 | mode);
            bits = 9 + !!(mode & 4) + !!(mode & 2) + !!(mode & 1);
            expect(ch->m_bit_count == bits, "ASCI documented normal frame lengths");
            z180_writecontrol(state, Z180_TDR0 + channel, 0x25);
            handle_io_timers(state, 160 * bits - 1);
            expect(!serial_count[channel] && ch->tx_bits_rem == 1, "ASCI all formats wait for final frame clock");
            handle_io_timers(state, 1);
            expect(serial_count[channel] == 1, "ASCI all formats complete at expected duration");
        }
        cpu_reset_z180(cpu);
        memset(serial_count, 0, sizeof(serial_count));
        z180_writecontrol(state, Z180_CNTLB0 + channel, 0);
        z180_writecontrol(state, Z180_CNTLA0 + channel, 0x24);
        z180_writecontrol(state, Z180_TDR0 + channel, 0xa5);
        z180_writecontrol(state, Z180_TDR0 + channel, 0x5a);
        expect(!(ch->m_stat & 2), "ASCI queued TDR is full");
        handle_io_timers(state, 1600);
        expect(serial_count[channel] == 1 && serial_last[channel] == 0xa5
               && ch->tx_bits_rem == 10 && (ch->m_stat & 2), "ASCI queued transfer starts after first frame");
        handle_io_timers(state, 1599);
        expect(serial_count[channel] == 1, "ASCI queued frame cannot complete early");
        handle_io_timers(state, 1);
        expect(serial_count[channel] == 2 && serial_last[channel] == 0x5a, "ASCI queued frame duration and order");
        z180_writecontrol(state, Z180_TDR0 + channel, 0x33);
        handle_io_timers(state, 17);
        z180_writecontrol(state, Z180_CNTLA0 + channel, 0x2c);
        expect(ch->m_brg_timer == 143, "ASCI equal-period configuration retains model phase");
        z180_writecontrol(state, Z180_CNTLB0 + channel, 7);
        expect(!ch->m_brg_period && !ch->m_brg_timer && !ch->m_brg_rate, "ASCI external-clock selection disables CPU divider");
        handle_io_timers(state, 100000);
        expect(ch->tx_bits_rem == 10 && serial_count[channel] == 2, "ASCI external mode receives no fabricated CPU clock edges");
        z180_writecontrol(state, Z180_CNTLB0 + channel, 1);
        expect(ch->m_brg_timer == 320, "ASCI new speed restarts explicit model phase");
        handle_io_timers(state, 319);
        expect(ch->tx_bits_rem == 10, "ASCI changed speed no premature edge");
        handle_io_timers(state, 1);
        expect(ch->tx_bits_rem == 9, "ASCI changed speed exact model edge");
        ch->rx_bits_rem = 4; ch->m_stat |= 0x90;
        z180_writecontrol(state, Z180_CNTLA0 + channel, 0x0c); /* disable, EFR=1 */
        expect(!ch->tx_bits_rem && !ch->rx_bits_rem && (ch->m_stat & 0x90) == 0x90,
               "ASCI disable aborts operations while retaining status");
        z180_writecontrol(state, Z180_CNTLA0 + channel, 0x2c);
        handle_io_timers(state, 3200);
        expect(serial_count[channel] == 2, "ASCI re-enable cannot resume aborted frame");
        z180_writecontrol(state, Z180_TDR0 + channel, 0x66);
        handle_io_timers(state, 11);
        cpu_reset_z180(cpu);
        expect(!ch->tx_bits_rem && !ch->rx_bits_rem && !ch->m_brg_period
               && !ch->m_brg_timer && ch->m_tdr == 0x66, "ASCI reset aborts frame/phase and retains TDR");
        handle_io_timers(state, 100000);
        expect(serial_count[channel] == 2, "ASCI reset prevents pending callback");
    }
    /* Different speeds run concurrently on the real executed instruction path. */
    cpu_reset_z180(cpu);
    memset(serial_count, 0, sizeof(serial_count));
    memset(testmem, 0, sizeof(testmem));
    attempts = trace_count = 0; limit = 2000;
    z180_writecontrol(state, Z180_DCNTL, 0);
    z180_writecontrol(state, Z180_RCR, 0);
    z180_writecontrol(state, Z180_ITC, 0);
    for (step = 0; step < 1000; step++) {
        testmem[4 * step + 1] = 0x3e; testmem[4 * step + 2] = 0x5a; testmem[4 * step + 3] = 0x47;
    }
    for (channel = 0; channel < 2; channel++) {
        z180_writecontrol(state, Z180_CNTLB0 + channel, channel);
        z180_writecontrol(state, Z180_CNTLA0 + channel, 0x24);
        z180_writecontrol(state, Z180_TDR0 + channel, 0xa0 + channel);
    }
    elapsed = pc = 0;
    for (step = 0; elapsed < 3200; step++) {
        cpu_execute_z180(cpu, 1);
        elapsed += clocks[step % 3]; pc += lengths[step % 3];
        expect(attempts == step + 1 && cpu_get_state_z180(cpu, Z180_PC) == pc,
               "ASCI clock oracle executes full NOP/LD instruction");
        for (channel = 0; channel < 2; channel++) {
            ch = channel ? device->z180asci->m_chan1 : device->z180asci->m_chan0;
            tick = elapsed / (channel ? 320 : 160);
            expect(ch->tx_bits_rem == (tick < 10 ? 10 - tick : 0)
                   && serial_count[channel] == (tick >= 10),
                   "ASCI consumes documented instruction clocks, not execute slice budget");
        }
    }
    expect(serial_last[0] == 0xa0 && serial_last[1] == 0xa1, "ASCI concurrent CPU-clock callback payloads");
    cpu_reset_z180(cpu);
    puts("ASCI CPU-clock/divisor/frame/queue/reset model contracts passed");
}

static void asci_status_irq_contracts(void)
{
    struct z180_device *device = (struct z180_device *)cpu;
    struct z180_state *state = device->m_token;
    struct z180asci_channel *ch;
    unsigned channel, written, cts, status, tie, rie, cause, il, action, enabled, sleep;
    for (channel = 0; channel < 2; channel++) {
        ch = channel ? device->z180asci->m_chan1 : device->z180asci->m_chan0;
        for (written = 0; written < 256; written++) for (cts = 0; cts < 2; cts++) {
            cpu_reset_z180(cpu);
            if (channel) z180_writecontrol(state, Z180_STAT1, 4);
            z180_writecontrol(state, Z180_CNTLB0 + channel, written);
            mm_asci_set_pins(ch, cts, 0);
            expect(z180_readcontrol(state, Z180_CNTLB0 + channel) == ((written & ~32u) | (cts ? 32 : 0))
                   && ch->m_cntlb == written,
                   "ASCI CTS bit 5 read preserves written prescaler and all source bits");
        }
        for (status = 0; status < 256; status++) for (cts = 0; cts < 2; cts++) {
            cpu_reset_z180(cpu);
            ch->m_stat = status;
            ch->m_cts = cts;
            for (tie = 0; tie < 2; tie++) for (rie = 0; rie < 2; rie++) {
                unsigned visible = status;
                unsigned expected;
                if (cts && (!channel || (status & 4))) visible &= ~2u;
                if (!channel && (status & 4)) visible &= ~0xf0u;
                ch->m_stat = (status & ~9u) | tie | (rie << 3);
                expected = (tie && (visible & 2)) || (rie && (visible & (channel ? 0xf0 : 0xf4)));
                expect(!!mm_asci_request(ch) == !!expected,
                       "ASCI exhaustive independent status/mask/CTS request oracle");
            }
        }
    }
    mm_asci_set_pins(device->z180asci->m_chan0, 1, 1);
    mm_asci_set_pins(device->z180asci->m_chan1, 1, 0);
    cpu_reset_z180(cpu);
    expect(z180_readcontrol(state, Z180_CNTLB0) == 0x27
           && z180_readcontrol(state, Z180_STAT0) == 4
           && device->z180asci->m_chan1->m_cts == 1,
           "ASCI reset clears control/requests and preserves external pin levels");
    mm_asci_set_pins(device->z180asci->m_chan0, 0, 0);
    mm_asci_set_pins(device->z180asci->m_chan1, 0, 0);
    cpu_reset_z180(cpu);
    ch = device->z180asci->m_chan0;
    ch->m_stat |= 0xf8; ch->rx_bits_rem = 4;
    mm_asci_set_pins(ch, 0, 1);
    expect(mm_asci_status(ch) == 14 && !ch->rx_bits_rem && mm_asci_request(ch),
           "ASCI DCD high clears RX flags and requests through RIE");
    expect(z180_readcontrol(state, Z180_STAT0) == 14 && (ch->m_stat & 4),
           "ASCI high DCD cannot be acknowledged by status polling");
    mm_asci_set_pins(ch, 0, 0);
    expect(z180_readcontrol(state, Z180_STAT0) == 14 && !mm_asci_request(ch),
           "ASCI first low-DCD read returns old latch then acknowledges");
    expect(z180_readcontrol(state, Z180_STAT0) == 10,
           "ASCI second low-DCD read reports cleared latch without phantom RIE request");
    mm_asci_set_pins(ch, 1, 0);
    expect(z180_readcontrol(state, Z180_STAT0) == 8 && (ch->m_stat & 2),
           "ASCI CTS high suppresses TDRE without destroying TDR occupancy");
    mm_asci_set_pins(ch, 0, 0);
    expect(z180_readcontrol(state, Z180_STAT0) == 10, "ASCI CTS low restores empty TDRE");
    z180_writecontrol(state, Z180_CNTLB0, 0);
    z180_writecontrol(state, Z180_CNTLA0, 0x24);
    z180_writecontrol(state, Z180_TDR0, 0x81);
    mm_asci_set_pins(ch, 1, 0);
    memset(serial_count, 0, sizeof(serial_count));
    handle_io_timers(state, 1600);
    expect(serial_count[0] == 1 && serial_last[0] == 0x81 && !ch->tx_bits_rem
           && !(z180_readcontrol(state, Z180_STAT0) & 2),
           "ASCI CTS high does not stop ongoing transmit or invent a duplicate queued byte");
    for (channel = 0; channel < 2; channel++) for (il = 0; il < 256; il += 32) {
        for (cause = 0; cause < 5; cause++) for (action = 0; action < 3; action++) {
            unsigned vector = 0x2000 + il + (channel ? 16 : 14);
            timer_irq_setup(il, 0x20);
            testmem[vector] = 0; testmem[vector + 1] = 3;
            ch = channel ? device->z180asci->m_chan1 : device->z180asci->m_chan0;
            ch->m_stat = cause ? (8 | (8u << cause)) : 3;
            expect(check_interrupts(state) == 0, "ASCI condition retained while IFF clear");
            cpu_execute_z180(cpu, 1);
            expect(check_interrupts(state) == 0, "ASCI EI delay");
            cpu_execute_z180(cpu, 1);
            if (action == 1) z180_writecontrol(state, Z180_STAT0 + channel, 0);
            if (action == 2) {
                if (cause) ch->m_stat &= ~0xf0u; /* isolated RX cause acknowledgment */
                else ch->m_stat &= ~2u; /* TDR fills */
            }
            expect(check_interrupts(state) == (action ? 0 : 16),
                   "ASCI live mask/cause cancellation prevents stale interrupt entry");
            if (!action) {
                expect(cpu_get_state_z180(cpu, Z180_PC) == 0x300
                       && cpu_get_state_z180(cpu, Z180_SP) == 0xeffe
                       && testmem[0xeffe] == 9 && !testmem[0xefff],
                       "ASCI fixed channel vector and following-PC stack frame");
                testmem[0x300] = 0xfb; testmem[0x301] = 0;
                cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
                expect(check_interrupts(state) == 16, "ASCI unacknowledged level requests again");
            }
        }
    }
    for (channel = 0; channel < 2; channel++) for (enabled = 0; enabled < 2; enabled++) {
        for (sleep = 0; sleep < 2; sleep++) {
            timer_irq_setup(0, 0x20);
            testmem[0x2000 + (channel ? 16 : 14)] = 0;
            testmem[0x2001 + (channel ? 16 : 14)] = 3;
            if (enabled) { cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1); }
            testmem[enabled ? 9 : 7] = sleep ? 0xed : 0x76;
            if (sleep) testmem[enabled ? 10 : 8] = 0x76;
            cpu_execute_z180(cpu, 1);
            z180_writecontrol(state, Z180_STAT0 + channel, 1);
            expect((check_interrupts(state) > 0) == enabled,
                   "ASCI empty-TDR TIE write immediately creates request without data event");
            expect(cpu_get_state_z180(cpu, Z180_HALT) == (!enabled && !sleep ? 1 : 0),
                   "ASCI enabled peripheral request wakes SLP with IFF clear");
            if (enabled) expect(cpu_get_state_z180(cpu, Z180_PC) == 0x300
                                && testmem[0xeffe] == (sleep ? 11 : 10),
                                "ASCI low-power interrupt saves following PC");
        }
    }
    timer_irq_setup(0, 0x20);
    testmem[0x200e] = 0; testmem[0x200f] = 1;
    testmem[0x2010] = 0; testmem[0x2011] = 2;
    z180_writecontrol(state, Z180_STAT0, 1); z180_writecontrol(state, Z180_STAT1, 1);
    cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
    expect(check_interrupts(state) == 16 && cpu_get_state_z180(cpu, Z180_PC) == 0x100,
           "ASCI0 priority precedes ASCI1");
    z180_writecontrol(state, Z180_STAT0, 0);
    testmem[0x100] = 0xfb; testmem[0x101] = 0;
    cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
    expect(check_interrupts(state) == 16 && cpu_get_state_z180(cpu, Z180_PC) == 0x200,
           "ASCI1 request survives higher-priority delivery");
    cpu_reset_z180(cpu);
    puts("ASCI status/pin/latch/vector/mask/wakeup contracts passed");
}

static void board_command(struct mm_board_startup *b, unsigned command)
{
    int bit;
    expect(mm_board_write(b, 0x60, 0) == MM_BOARD_OK, "Microwire CS release");
    expect(mm_board_write(b, 0x41, 0) == MM_BOARD_OK, "Microwire SK low");
    expect(mm_board_write(b, 0x60, 0x40) == MM_BOARD_OK, "Microwire CS select");
    for (bit = 8; bit >= 0; bit--) {
        expect(mm_board_write(b, 0x40, (command >> bit) & 1) == MM_BOARD_OK, "Microwire DI command");
        expect(mm_board_write(b, 0x41, 1) == MM_BOARD_OK, "Microwire command rising edge");
        expect(mm_board_write(b, 0x41, 0) == MM_BOARD_OK, "Microwire command falling edge");
    }
}

static void board_data(struct mm_board_startup *b, unsigned word, unsigned bits)
{
    int bit;
    for (bit = 15; bit >= (int)(16 - bits); bit--) {
        mm_board_write(b, 0x40, (word >> bit) & 1);
        mm_board_write(b, 0x41, 1);
        mm_board_write(b, 0x41, 0);
    }
}

static void panel_write_contracts(void)
{
    struct mm_board_startup b;
    struct mm_panel *p = &b.panel;
    unsigned i, code;
    UINT8 read_value;
    static const unsigned char bitmap[5] = {0x03,0x51,0xc2,0x24,0x09};
    mm_board_reset(&b);
    expect(mm_board_write(&b, 0xa0, 0x14) == MM_BOARD_UNKNOWN,
           "Panel remains disconnected by default");
    p->enabled = 1;
    mm_panel_reset(p, MM_PANEL_POWER_CLOCKS);
    expect(mm_board_write(&b, 0xa0, 0x14) == MM_BOARD_INPUT,
           "Panel selector input direction cannot use reset latches");
    mm_board_write(&b, 0x43, 0x81);
    expect(mm_board_write(&b, 0xa0, 0x14) == MM_BOARD_PANEL_TIMING && !p->writes,
           "Panel power interval rejects a write without side effects");
    mm_board_tick(&b, 921599);
    expect(p->cooldown == 1 && mm_board_write(&b, 0xa0, 0x14) == MM_BOARD_PANEL_TIMING,
           "Panel initial interval lasts through the penultimate phi");
    mm_board_tick(&b, 1);
    expect(mm_board_write(&b, 0x55a0, 0x14) == MM_BOARD_OK && !p->cursor_visible,
           "Panel aliased data selector consumes cursor-off");
    expect(mm_board_write(&b, 0xa0, 'X') == MM_BOARD_PANEL_TIMING && p->cursor == 0,
           "Panel cooldown rejects a second byte before touching cells");
    mm_board_tick(&b, 737);
    expect(mm_board_write(&b, 0xa0, 'X') == MM_BOARD_PANEL_TIMING, "Panel write deadline minus one");
    mm_board_tick(&b, 1);
    for (i = 0; i < 40; i++) {
        expect(mm_board_write(&b, 0xa0, 0x21 + i) == MM_BOARD_OK, "Panel forty independent character writes");
        mm_board_tick(&b, 738);
    }
    expect(p->cursor == 0, "Panel wraps at exactly forty cells");
    for (i = 0; i < 40; i++) expect(p->cells[i] == 0x21 + i, "Panel independent cell index oracle");
    mm_board_write(&b, 0x41, 0x20);
    expect(mm_board_write(&b, 0x91a0, 23) == MM_BOARD_OK && p->cursor == 23,
           "Panel PB5 selects command cursor position, not a printable byte");
    mm_board_tick(&b, 245);
    expect(mm_board_write(&b, 0xa0, 1) == MM_BOARD_PANEL_TIMING && p->cursor == 23,
           "Panel cursor deadline minus one");
    mm_board_tick(&b, 1);
    mm_board_write(&b, 0x41, 0);
    mm_board_write(&b, 0xa0, 0x0c);
    expect(p->cursor == 23 && p->cooldown == 3441, "Panel clear preserves cursor and uses clear interval");
    for (i = 0; i < 40; i++) expect(p->cells[i] == 0x20, "Panel clear covers both rows");
    mm_board_tick(&b, 3440);
    expect(mm_board_write(&b, 0xa0, 0x0a) == MM_BOARD_PANEL_TIMING, "Panel clear deadline minus one");
    mm_board_tick(&b, 1); mm_board_write(&b, 0xa0, 0x0a);
    expect(p->cursor == 3, "Panel LF preserves column and changes row");
    mm_board_tick(&b, 738);
    for (code = 0; code < 2; code++) {
        mm_board_write(&b, 0xa0, 0x1b); mm_board_tick(&b, 738);
        mm_board_write(&b, 0xa0, 0xb4 + code); mm_board_tick(&b, 738);
        for (i = 0; i < 5; i++) {
            expect(p->custom_count == code, "Panel incomplete ESC cannot install a glyph");
            mm_board_write(&b, 0xa0, bitmap[i] ^ code); mm_board_tick(&b, 738);
        }
        expect(p->custom_code[code] == 0xb4 + code, "Panel ESC target retained");
        for (i = 0; i < 5; i++)
            expect(p->custom_bitmap[code][i] == (bitmap[i] ^ code), "Panel five-byte glyph packet oracle");
    }
    mm_board_write(&b, 0xa0, 0xb4); mm_board_tick(&b, 738);
    expect(p->cells[3] == 0xb4 && p->cursor == 4, "Panel custom code writes one cell");
    for (i = 0x7f; i <= 0xa0; i++) { mm_board_write(&b, 0xa0, i); mm_board_tick(&b, 738); }
    expect(p->cursor == 4, "Panel documented null range does not advance cursor");
    mm_board_write(&b, 0xa0, 0x0f); mm_board_tick(&b, 738);
    expect(p->cursor == 4 && p->cells[3] == 0xb4 && !p->pending_bytes,
           "Panel unlisted control-range null has no cursor/cell/packet effect");
    expect(mm_board_write(&b, 0xa0, 0x12) == MM_BOARD_PANEL_PROTOCOL && !p->pending_bytes,
           "Unimplemented scroll/control behavior stops before side effects");
    mm_board_write(&b, 0xa0, 0x1b); mm_board_tick(&b, 738);
    i = p->writes;
    expect(mm_board_write(&b, 0xa0, 0xb6) == MM_BOARD_PANEL_PROTOCOL
           && p->writes == i && p->custom_count == 2 && p->pending_bytes == 6,
           "Disputed third/redefined glyph does not consume a byte");
    mm_board_write(&b, 0x41, 0x20);
    expect(mm_board_write(&b, 0xa0, 5) == MM_BOARD_PANEL_PROTOCOL && p->cursor == 4,
           "Interleaved cursor command cannot silently discard ESC state");
    expect(mm_board_write(&b, 0xa0, 0x40) == MM_BOARD_OK && !p->pending_bytes && !p->custom_count,
           "Panel software reset aborts the packet and cancels custom definitions");
    expect(p->cooldown == 614400 && p->cursor == 0 && p->cursor_visible,
           "Panel software reset interval and explicit reset state");
    mm_board_tick(&b, 614400);
    expect(mm_board_write(&b, 0xa0, 0x41) == MM_BOARD_PANEL_PROTOCOL,
           "Unverified cursor-read command is not fabricated");
    read_value = 0xff;
    expect(mm_board_read(&b, 0xa0, &read_value) == MM_BOARD_OK && read_value == 0 && p->status_reads == 1,
           "Panel status read (AD high) answers 00: bit 1 clear, no display fault");
    mm_board_write(&b, 0x41, 0x00);
    expect(mm_board_read(&b, 0xa0, &read_value) == MM_BOARD_PANEL_READ && p->status_reads == 1,
           "Panel never provides an invented data or position byte");
    mm_board_write(&b, 0x41, 0x60);
    expect(mm_board_read(&b, 0xa0, &read_value) == MM_BOARD_PANEL_READ,
           "A deselected panel answers no read");
    mm_board_write(&b, 0x41, 0x20);
    mm_board_write(&b, 0x41, 0x60);
    i = p->writes;
    expect(mm_board_write(&b, 0xa0, 0xff) == MM_BOARD_OK && p->writes == i
           && p->ignored_writes == 1, "Panel deselected write has no device side effect");
    expect(p->writes == p->data_writes + p->command_writes, "Panel counters classify accepted writes only");
    b.mode = 0x90; b.latch[1] = 0; b.control_latch = 0x40;
    expect(mm_board_write(&b, 0xa0, 0x14) == MM_BOARD_OK && !b.eeprom_selected,
           "Panel byte does not clock or validate unrelated Microwire inputs");
    mm_board_reset(&b);
    expect(!p->enabled && !p->writes && !p->custom_count, "Fresh worker disconnects and clears panel state");
    puts("Panel selector/packet/cursor/clear/delay/guard model contracts passed");
}

static void board_startup_contracts(void)
{
    static const unsigned modes[] = {0x80,0x81,0x92,0x9b};
    static const unsigned masks[][3] = {{0,0,0},{0,0,15},{255,255,0},{255,255,255}};
    struct mm_board_startup b;
    unsigned i, port, bit, address, word;
    UINT8 value;
    for (i = 0; i < 4; i++) {
        mm_board_reset(&b);
        expect(b.mode == 0x9b && !b.eeprom_write_enabled, "Board reset inputs/write protection");
        mm_board_write(&b, 0x43, modes[i]);
        for (port = 0; port < 3; port++) {
            expect(mm_board_input_mask(&b, port) == masks[i][port], "Independent PPI direction oracle");
            mm_board_write(&b, 0xa040 + port, 0xa5 + port);
            expect(mm_board_read(&b, 0xf040 + port, &value) ==
                (masks[i][port] ? MM_BOARD_INPUT : MM_BOARD_OK), "PPI output versus unresolved input");
            if (!masks[i][port]) expect(value == 0xa5 + port, "PPI aliased output readback");
        }
    }
    mm_board_write(&b, 0x43, 0x80);
    for (bit = 0; bit < 8; bit++) {
        mm_board_write(&b, 0x43, bit * 2 + 1);
        expect(b.latch[2] == (1u << (bit + 1)) - 1, "PPI individual BSR set");
    }
    for (bit = 0; bit < 8; bit++) mm_board_write(&b, 0x43, bit * 2);
    expect(b.mode == 0x80 && !b.latch[2], "BSR retains mode and clears C bits");
    expect(mm_board_write(&b, 0x43, 0xa4) == MM_BOARD_MODE && b.mode == 0x80,
        "Unsupported PPI mode stops before changing mode");
    for (address = 0; address < 64; address++) b.eeprom[address] = 0xa55a ^ (address << 8) ^ address;
    for (address = 0; address < 64; address++) {
        board_command(&b, 0x180 | address);
        expect(mm_board_read(&b, 0x80, &value) == MM_BOARD_OK && value == 0, "Microwire dummy zero");
        word = 0;
        for (bit = 0; bit < 16; bit++) {
            mm_board_write(&b, 0x41, 1);
            mm_board_read(&b, 0x80, &value);
            word = (word << 1) | ((value >> 3) & 1);
            mm_board_write(&b, 0x41, 1); /* No second rising edge. */
            mm_board_write(&b, 0x41, 0);
        }
        expect(word == (0xa55a ^ (address << 8) ^ address), "64 Microwire address/MSB/edge cases");
    }
    board_command(&b, 0x145); board_data(&b, 0x1357, 16);
    expect(!b.eeprom_busy && b.eeprom[5] != 0x1357, "EWDS prevents programming");
    board_command(&b, 0x130);
    board_command(&b, 0x145); board_data(&b, 0x1357, 15);
    mm_board_write(&b, 0x60, 0);
    expect(!b.eeprom_busy, "Incomplete write aborted by CS");
    board_command(&b, 0x145); board_data(&b, 0x1357, 16);
    expect(b.eeprom_busy == 61440 && b.eeprom[5] != 0x1357, "Programming starts without immediate commit");
    mm_board_write(&b, 0x60, 0); mm_board_write(&b, 0x60, 0x40);
    expect(mm_board_read(&b, 0x80, &value) == MM_BOARD_OK && value == 0, "Actual busy status");
    mm_board_tick(&b, 61439);
    expect(b.eeprom_busy == 1 && b.eeprom[5] != 0x1357, "No commit one phi before deadline");
    mm_board_tick(&b, 1);
    expect(b.eeprom[5] == 0x1357 && b.eeprom_writes == 1, "Programming deadline commits word");
    expect(mm_board_read(&b, 0x80, &value) == MM_BOARD_OK && value == 8, "Completed busy poll becomes ready");
    board_command(&b, 0x1c5); mm_board_tick(&b, 61440);
    expect(b.eeprom[5] == 0xffff, "ERASE one word");
    board_command(&b, 0x110); board_data(&b, 0x2468, 16); mm_board_tick(&b, 61440);
    for (i = 0; i < 64; i++) expect(b.eeprom[i] == 0x2468, "WRAL array coverage");
    board_command(&b, 0x120); mm_board_tick(&b, 61440);
    for (i = 0; i < 64; i++) expect(b.eeprom[i] == 0xffff, "ERAL array coverage");
    board_command(&b, 0x100); board_command(&b, 0x145); board_data(&b, 0x1357, 16);
    expect(!b.eeprom_busy && b.eeprom[5] == 0xffff, "EWDS restored after programming");
    mm_board_reset(&b);
    expect(b.eeprom[5] == 0xffff && !b.eeprom_writes && b.status80_contacts == -1, "Fresh erased lab state");
    puts("Board PPI/Microwire startup model contracts passed");
}

static void csio_symbol_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned pattern, bit, order, mode, poll;
    /* Every byte and both firmware/documented initialization orderings.
     * Expected bits come directly from the independent byte value. */
    for (pattern = 0; pattern < 256; pattern++) {
        for (order = 0; order < 2; order++) {
            cpu_reset_z180(cpu);
            if (order) z180_writecontrol(state, Z180_CNTR, 0x57);
            z180_writecontrol(state, Z180_TRDR, pattern);
            if (!order) z180_writecontrol(state, Z180_CNTR, 0x57);
            handle_io_timers(state, 10000);
            for (poll = 0; poll < 3; poll++)
                expect(z180_readcontrol(state, Z180_CNTR) == 0x57,
                       "CSI/O external TE survives CPU clocks and repeated polls");
            for (bit = 0; bit < 8; bit++) {
                expect(mm_csio_external_symbol(cpu, 0) == ((pattern >> bit) & 1),
                       "CSI/O transmit LSB-first independent pattern");
                expect(mm_csio_remaining(cpu) == 7 - bit,
                       "CSI/O consumes exactly one bit per explicit symbol");
                expect(z180_readcontrol(state, Z180_CNTR) == (bit == 7 ? 0xc7 : 0x57),
                       "CSI/O TE/EF change only after eighth bit");
            }
            expect(mm_csio_external_symbol(cpu, 1) == -1
                   && z180_readcontrol(state, Z180_CNTR) == 0xc7,
                   "CSI/O idle clock cannot start another transfer");
            z180_writecontrol(state, Z180_CNTR, 0x47);
            expect(z180_readcontrol(state, Z180_CNTR) == 0xc7,
                   "CSI/O control write cannot clear read-only EF");
            z180_writecontrol(state, Z180_TRDR, 0x5a);
            expect(z180_readcontrol(state, Z180_CNTR) == 0x47,
                   "CSI/O data write acknowledges end flag");
        }
        cpu_reset_z180(cpu);
        z180_writecontrol(state, Z180_CNTR, 0x67);
        for (bit = 0; bit < 8; bit++) {
            mm_csio_external_symbol(cpu, (pattern >> bit) & 1);
            expect(z180_readcontrol(state, Z180_CNTR) == (bit == 7 ? 0xc7 : 0x67),
                   "CSI/O receive completes only after eighth sampled bit");
        }
        expect(z180_readcontrol(state, Z180_TRDR) == pattern,
               "CSI/O receive LSB-first independent pattern");
        expect(z180_readcontrol(state, Z180_CNTR) == 0x47,
               "CSI/O data read acknowledges end flag");
    }
    for (mode = 0x10; mode <= 0x20; mode += 0x10) {
        cpu_reset_z180(cpu);
        z180_writecontrol(state, Z180_CNTR, mode | 7);
        for (bit = 0; bit < 3; bit++) mm_csio_external_symbol(cpu, 1);
        z180_writecontrol(state, Z180_CNTR, mode | 0x47);
        expect(mm_csio_remaining(cpu) == 5, "CSI/O EIE change does not restart frame");
        z180_writecontrol(state, Z180_CNTR, 7);
        expect(mm_csio_remaining(cpu) == 0
               && mm_csio_external_symbol(cpu, 1) == -1
               && z180_readcontrol(state, Z180_CNTR) == 7,
               "CSI/O disable aborts without generating EF");
        z180_writecontrol(state, Z180_CNTR, mode | 7);
        mm_csio_external_symbol(cpu, 1);
        cpu_reset_z180(cpu);
        expect(mm_csio_remaining(cpu) == 0
               && mm_csio_external_symbol(cpu, 1) == -1,
               "CSI/O reset cancels in-flight symbols");
    }
    /* External symbols cannot clock an internally selected transfer. */
    cpu_reset_z180(cpu);
    z180_writecontrol(state, Z180_CNTR, 0x10);
    for (bit = 0; bit < 8; bit++) mm_csio_external_symbol(cpu, 1);
    expect(mm_csio_remaining(cpu) == 8
           && z180_readcontrol(state, Z180_CNTR) == 0x10,
           "CSI/O internal source cannot consume external symbols");
    cpu_reset_z180(cpu);
    puts("CSI/O external symbol, byte ordering, end flag, abort/reset contracts passed");
}

static void csio_irq_contracts(void)
{
    struct z180_state *state = ((struct z180_device *)cpu)->m_token;
    unsigned bit, action, enabled, sleep;
    for (action = 0; action < 3; action++) {
        timer_irq_setup(0xa0, 0x20);
        testmem[0x20ac] = 0; testmem[0x20ad] = 3;
        z180_writecontrol(state, Z180_CNTR, 0x27); /* EIE off while receiving */
        for (bit = 0; bit < 8; bit++) mm_csio_external_symbol(cpu, (0xa6 >> bit) & 1);
        expect(check_interrupts(state) == 0, "CSI/O DI retains completed request");
        z180_writecontrol(state, Z180_CNTR, 0x47);
        expect(check_interrupts(state) == 0, "CSI/O global interrupt mask");
        cpu_execute_z180(cpu, 1); /* EI */
        expect(check_interrupts(state) == 0, "CSI/O EI delay");
        cpu_execute_z180(cpu, 1);
        if (action == 1) z180_writecontrol(state, Z180_CNTR, 7);
        if (action == 2) z180_readcontrol(state, Z180_TRDR);
        expect(check_interrupts(state) == (action ? 0 : 16),
               "CSI/O EIE/data acknowledgment cancels unserved level request");
        if (!action) {
            expect(cpu_get_state_z180(cpu, Z180_PC) == 0x300
                   && cpu_get_state_z180(cpu, Z180_SP) == 0xeffe,
                   "CSI/O I/IL offset 0c vector and RAM stack");
            testmem[0x300] = 0xfb; testmem[0x301] = 0;
            cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1);
            expect(check_interrupts(state) > 0
                   && cpu_get_state_z180(cpu, Z180_PC) == 0x300,
                   "CSI/O pending EF requests again until data acknowledgment");
            expect(z180_readcontrol(state, Z180_TRDR) == 0xa6,
                   "CSI/O interrupt does not consume received data");
        }
    }
    for (enabled = 0; enabled < 2; enabled++) {
        for (sleep = 0; sleep < 2; sleep++) {
            timer_irq_setup(0, 0x20);
            testmem[0x200c] = 0; testmem[0x200d] = 3;
            if (enabled) { cpu_execute_z180(cpu, 1); cpu_execute_z180(cpu, 1); }
            testmem[enabled ? 9 : 7] = sleep ? 0xed : 0x76;
            if (sleep) testmem[enabled ? 10 : 8] = 0x76;
            cpu_execute_z180(cpu, 1);
            z180_writecontrol(state, Z180_CNTR, 0x57);
            for (bit = 0; bit < 8; bit++) mm_csio_external_symbol(cpu, 0);
            expect((check_interrupts(state) > 0) == enabled,
                   "CSI/O completion wake respects global IFF");
            expect(cpu_get_state_z180(cpu, Z180_HALT) == (!enabled && !sleep ? 1 : 0),
                   "CSI/O enabled request wakes SLP even with global mask");
            if (enabled) expect(cpu_get_state_z180(cpu, Z180_PC) == 0x300
                                && testmem[0xeffe] == (sleep ? 11 : 10),
                                "CSI/O low-power entry saves following PC");
        }
    }
    cpu_reset_z180(cpu);
    puts("CSI/O vector, IFF/EI, level/acknowledgment and low-power contracts passed");
}

static int self_test(void)
{
    /* Original synthetic instructions, no terminal ROM excerpts. */
    static const unsigned char arithmetic[] = {
        0x01,0x07,0x06, 0xed,0x4c, /* BC=0607; MLT BC => 42 */
        0x3e,0xf0, 0xed,0x64,0x0f, 0x76 /* TST 0f sets Z, keeps A */
    };
    static const unsigned char mmu[] = {
        0x3e,0x84,0xed,0x39,0x3a, /* OUT0 CBAR=84 */
        0x3e,0x20,0xed,0x39,0x39, /* OUT0 BBR=20 */
        0x3e,0x40,0xed,0x39,0x38, /* OUT0 CBR=40 */
        0xed,0x00,0x39,0x76       /* IN0 B,(BBR) */
    };
    static const unsigned char stack[] = {
        0x31,0x00,0xf0, 0xcd,0x08,0x00,0x76,0x00,0xc9
    };
    static const unsigned char wrap[] = {
        0x3e,0x85,0xed,0x39,0x3a,0x3e,0xff,0xed,0x39,0x38,0x76
    };
    static const unsigned char sleep[] = {0xed,0x76};
    static const unsigned char loop[] = {0x18,0xfe};
    static const unsigned char controls[] = {
        0x3e,0x3f,0xed,0x39,0x36, /* reserved RCR bits discarded */
        0x3e,0x5f,0xed,0x39,0x3e, /* M1TE remains one, reserved bits discarded */
        0x3e,0x1f,0xed,0x39,0x3f, /* reserved ICR bits do not relocate I/O */
        0x3e,0xc0,0xed,0x39,0x10, /* TIF bits are read-only */
        0x3e,0x88,0xed,0x39,0x0a, /* CSI/O EF and reserved bit are not writable */
        0x3e,0x80,0xed,0x39,0x34, /* writing TRAP=1 cannot create a trap */
        0x3e,0xff,0xed,0x39,0x33, /* IL uses only the high three bits */
        0x3e,0x00,0xed,0x39,0x30, /* disable DMA; DWE bits read back one */
        0x3e,0xa5,0xed,0x39,0x32,0x76
    };
    offs_t address;
    testing = 1;
    board_startup_contracts();
    panel_write_contracts();
    asci_inactive_contracts();
    asci_clock_contracts();
    asci_status_irq_contracts();
    dma_register_contracts();
    dma0_contracts();
    card_contracts();
    voice_contracts();
    timer_latch_contracts();
    timer_enable_ack_contracts();
    timer_flag_edge_contracts();
    timer_stop_restart_contracts();
    timer_transition_model_contracts();
    timer_instruction_clock_contracts();
    timer_irq_contracts();
    timer_low_power_contracts();
    timer_mmu_boundary_contracts();
    timer_irq_clock_model_contracts();
    timer_output_model_contracts();
    interrupt_interaction_contracts();
    csio_symbol_contracts();
    csio_irq_contracts();
    cpu_reset_z180(cpu);
    z180_writecontrol(((struct z180_device *)cpu)->m_token, Z180_TRDR, 0x5a);
    cpu_reset_z180(cpu);
    expect(z180_readcontrol(((struct z180_device *)cpu)->m_token, Z180_TRDR) == 0x5a,
           "CSI/O reset retains stopped data register");
    expect(cpu_get_state_z180(cpu, Z180_RCR) == 0xc0, "RCR reset");
    expect(cpu_get_state_z180(cpu, Z180_DCNTL) == 0xf0, "DCNTL reset");
    expect(cpu_get_state_z180(cpu, Z180_OMCR) == 0xe0, "OMCR reset");
    expect(cpu_get_state_z180(cpu, Z180_ITC) == 1, "ITC reset");
    expect(cpu_get_state_z180(cpu, Z180_DSTAT) == 0x30, "DSTAT reset");
    reset_test(controls, sizeof(controls));
    expect(cpu_get_state_z180(cpu, Z180_RCR) == 3, "RCR writable mask");
    expect(cpu_get_state_z180(cpu, Z180_OMCR) == 0x40, "OMCR fixed M1TE and writable mask");
    expect(cpu_get_state_z180(cpu, Z180_IOCR) == 0, "ICR reserved bits");
    expect(cpu_get_state_z180(cpu, Z180_DCNTL) == 0xa5, "DCNTL readback");
    expect(cpu_get_state_z180(cpu, Z180_TCR) == 0, "TCR read-only flags");
    expect(cpu_get_state_z180(cpu, Z180_CNTR) == 0, "CNTR read-only/reserved bits");
    expect(cpu_get_state_z180(cpu, Z180_ITC) == 0, "ITC TRAP is not software-settable");
    expect(cpu_get_state_z180(cpu, Z180_IL) == 0xe0, "IL writable mask");
    expect(cpu_get_state_z180(cpu, Z180_DSTAT) == 0x30, "DSTAT disabled readback");
    reset_test(arithmetic, sizeof(arithmetic));
    expect(cpu_get_state_z180(cpu, Z180_BC) == 42, "MLT");
    expect(cpu_get_state_z180(cpu, Z180_A) == 0xf0
           && (cpu_get_state_z180(cpu, Z180_AF) & 0x40), "TST immediate");
    reset_test(mmu, sizeof(mmu));
    expect(cpu_get_state_z180(cpu, Z180_B) == 0x20, "IN0/OUT0 register readback");
    address = 0x3fff; cpu_translate_z180(cpu, AS_PROGRAM, 0, &address);
    expect(address == 0x3fff, "common zero boundary");
    address = 0x4000; cpu_translate_z180(cpu, AS_PROGRAM, 0, &address);
    expect(address == 0x24000, "bank start");
    address = 0x7fff; cpu_translate_z180(cpu, AS_PROGRAM, 0, &address);
    expect(address == 0x27fff, "bank end");
    address = 0x8000; cpu_translate_z180(cpu, AS_PROGRAM, 0, &address);
    expect(address == 0x48000, "common one start");
    address = 0xffff; cpu_translate_z180(cpu, AS_PROGRAM, 0, &address);
    expect(address == 0x4ffff, "common one end");
    reset_test(wrap, sizeof(wrap));
    address = 0xffff; cpu_translate_z180(cpu, AS_PROGRAM, 0, &address);
    expect(address == 0x0efff, "20-bit physical wrap");
    reset_test(stack, sizeof(stack));
    expect(cpu_get_state_z180(cpu, Z180_SP) == 0xf000 && trace[3] == 6,
           "CALL/RET stack");
    reset_test(sleep, sizeof(sleep));
    expect(!strcmp(reason, "halt_or_sleep"), "SLP");
    reset_test(loop, sizeof(loop));
    expect(!strcmp(reason, "budget") && attempts == 100, "instruction budget");
    puts("Z180 reset/MMU/MLT/TST/IN0/OUT0/stack/SLP/budget/control-register contracts passed");
    return 0;
}

int main(int argc, char **argv)
{
    FILE *source;
    char *end;
    unsigned long requested;
    int contact_setting = -1;
    unsigned i;
    if (argc != 2 && (argc < 4 || argc > 22)) return 2;
    if (argc == 2 && strcmp(argv[1], "--self-test")) return 2;
    cpu = cpu_create_z180("mm", Z180_TYPE_Z180, 6144000,
        &memory, NULL, &ports, irq_acknowledge, NULL, receive, transmit, NULL, NULL, NULL, NULL);
    if (argc == 2) return self_test();
    requested = strtoul(argv[2], &end, 10);
    if (!argv[2][0] || *end || requested < 1) return 2;
    if (!strcmp(argv[3], "lab-ram-40000")) lab_ram = 1;
    else if (!strcmp(argv[3], "lab-board-startup")) lab_ram = lab_board = 1;
    else if (strcmp(argv[3], "rom-only")) return 2;
    /* The same ceiling as the wrapper's MAX_STARTUP_INSTRUCTIONS, and for its
     * reason: an install reaches the manager's first packet at 66 s. */
    if (requested > (lab_board ? 128000000 : 100000)) {
        /* A streamed run is paced to the wall clock, so its budget is real
         * minutes: an install with its table download is several of them.
         * 1G attempts is about 21 minutes modelled; the parent's wall and
         * CPU limits still bound it. */
        for (i = 9; i < (unsigned)argc; i++) if (!strncmp(argv[i], "stream=", 7)) break;
        if (!lab_board || i == (unsigned)argc || requested > 1000000000) return 2;
    }
    if (argc >= 5) {
        unsigned long contacts = strtoul(argv[4], &end, 10);
        if (!lab_board) return 2;
        if (argc >= 6 && !strcmp(argv[4], "-1")) contact_setting = -1;
        else {
            if (!argv[4][0] || *end || contacts > 15) return 2;
            contact_setting = contacts;
        }
    }
    if (argc >= 6) {
        unsigned long period = strtoul(argv[5], &end, 10);
        if (!argv[5][0] || *end || (period < 20 && !(argc >= 7 && period == 0))
            || period > 6144000) return 2;
        int2_period = int2_countdown = period;
    }
    limit = requested;
    source = fopen(argv[1], "rb");
    if (!source) return 2;
    image_size = fread(image, 1, sizeof(image), source);
    if (image_size != sizeof(image) || fgetc(source) != EOF || ferror(source)) {
        fclose(source); return 2;
    }
    fclose(source);
    mm_board_reset(&board);
    board.status80_contacts = contact_setting;
    if (argc >= 7) {
        unsigned long status = strtoul(argv[6], &end, 10);
        if (argc >= 8 && !strcmp(argv[6], "-1")) board.status_e0_fixture = -1;
        else {
            if (!argv[6][0] || *end || status > 255) return 2;
            board.status_e0_fixture = status;
        }
    }
    if (argc >= 8) {
        if (!lab_board) return 2;
        if (!strcmp(argv[7], "cu20026-t23a-lab")) {
            board.panel.enabled = 1;
            mm_panel_reset(&board.panel, MM_PANEL_POWER_CLOCKS);
        } else if (argc < 9 || strcmp(argv[7], "unconnected")) return 2;
    }
    if (argc >= 9 && strcmp(argv[8], "-1")) {
        unsigned long pins = strtoul(argv[8], &end, 10);
        if (!argv[8][0] || *end || pins > 3) return 2;
        asci0_pin_fixture = pins;
    }
    /* Opt-in stand-ins, by name after the positional fixtures. */
    for (i = 9; i < (unsigned)argc; i++) {
        if (!lab_board) return 2;
        if (!strcmp(argv[i], "telephony=standin")) telephony.enabled = 1;
        else if (!strcmp(argv[i], "coin=standin")) coin.enabled = 1;
        else if (!strcmp(argv[i], "modem=control")) modem.enabled = 1;
        else if (!strncmp(argv[i], "cards=", 6)) {
            /* how+track per card, comma separated: b4761...=2512 */
            char spec[MM_CARD_BODY + 2], *p = argv[i] + 6;
            card.enabled = 1;
            while (*p) {
                size_t n = strcspn(p, ",");
                if (!n || n > MM_CARD_BODY + 1) return 2;
                memcpy(spec, p, n);
                spec[n] = 0;
                if (!mm_card_add(&card, spec)) return 2;
                p += n;
                if (*p == ',' && *++p == 0) return 2;
            }
            if (!card.count) return 2;
        }
        else if (!strcmp(argv[i], "speech=silent")) speech_sink = 1;
        else if (!strcmp(argv[i], "speech=chip")) voice.enabled = 1;
        else if (!strncmp(argv[i], "voice=", 6)) {
            /* the voiceware: whole 128 KiB banks, each with its header */
            FILE *vw = fopen(argv[i] + 6, "rb");
            size_t n, b;
            if (!vw || voice.size) { if (vw) fclose(vw); return 2; }
            n = fread(voice_rom, 1, sizeof(voice_rom), vw);
            if (fgetc(vw) != EOF || ferror(vw)) n = 0;
            fclose(vw);
            if (!n || n % MM_VOICE_BANK) return 2;
            for (b = 0; b < n; b += MM_VOICE_BANK)
                if (memcmp(voice_rom + b + 1, "\x5a\xa5\x69\x55", 4)) return 2;
            voice.rom = voice_rom;
            voice.size = (unsigned)n;
        }
        else if (!strncmp(argv[i], "coinhello=", 10)) {
            /* the coin stand-in's answer to `g`: two hex digits, or - */
            if (!strcmp(argv[i] + 10, "-")) coin_hello = -1;
            else {
                unsigned long hello = strtoul(argv[i] + 10, &end, 16);
                if (!argv[i][10] || *end || hello > 0xff) return 2;
                coin_hello = (int)hello;
            }
        }
        else if (!strncmp(argv[i], "stream=", 7)) {
            unsigned long fd = strtoul(argv[i] + 7, &end, 10);
            if (!argv[i][7] || *end || fd < 3 || fd > 1023) return 2;
            stream_fd = (int)fd;
        }
        else if (!strncmp(argv[i], "peek=", 5)) {
            char *p = argv[i] + 5;
            while (*p) {
                unsigned long address = strtoul(p, &end, 16);
                if (end == p || address > 0xffff || peek_count == 8) return 2;
                peek[peek_count++] = address;
                p = end;
                if (*p == ',') p++;
                else if (*p) return 2;
            }
        }
        else if (!strncmp(argv[i], "events=", 7)) {
            /* ms:kind value, comma separated, in time order:
             * h1 / h0 handset lifted / hung up, xHH a raw telephony opcode,
             * cN a coin on channel N (1..16), kC a key (0-9 * #), sN the
             * port 80 contacts (bits 7..4) become N, mHH the 73K212's DR. */
            char *p = argv[i] + 7;
            unsigned last = 0;
            while (*p) {
                unsigned long ms = strtoul(p, &end, 10), value;
                char kind;
                if (end == p || *end != ':' || event_count == MM_EVENTS || ms < last) return 2;
                kind = end[1];
                p = end + 2;
                if (kind == 'k') {
                    if (!*p || !strchr("1234567890*#", *p)) return 2;
                    value = (unsigned char)*p;
                    end = p + 1;
                } else value = strtoul(p, &end, kind == 'x' || kind == 'm' || kind == 'b' ? 16 : 10);
                if (end == p || !kind || !strchr("hxckswmb", kind) || value > 0xff) return 2;
                events[event_count].ms = last = ms;
                events[event_count].kind = kind;
                events[event_count].value = value;
                event_count++;
                p = end;
                if (*p == ',') p++;
                else if (*p) return 2;
            }
        } else return 2;
    }
    if (event_count && !telephony.enabled) return 2;
    if (coin.enabled) coin.hello = coin_hello;
    /* The reader's bits are three of port 80's: the contacts fixture must
     * be explicit, and every stand-in it talks through is the telephony
     * stand-in's (events). */
    if (card.enabled && (board.status80_contacts < 0 || !telephony.enabled)) return 2;
    if ((speech_sink || voice.enabled) && !telephony.enabled) return 2;
    if (speech_sink && voice.enabled) return 2;
    if (!voice.enabled != !voice.size) return 2;
    /* The coin stand-in is ASCI0's far end: it needs the pin fixture with
     * DCD low, or the receiver is held off. */
    if (coin.enabled && (asci0_pin_fixture < 0 || (asci0_pin_fixture & 2))) return 2;
    for (i = 0; i < event_count; i++)
        if (!event_allowed(events[i].kind, events[i].value)) return 2;
    if (modem.enabled && board.status_e0_fixture < 0) return 2;
    /* The far end is the modem's: no modem, nothing to stream. */
    if (stream_fd >= 0 && !modem.enabled) return 2;
    stream_profile = stream_fd >= 0;
    if (stream_fd >= 0) {
        signal(SIGPIPE, SIG_IGN);
        clock_gettime(CLOCK_MONOTONIC, &stream_start);
    }
    if (telephony.enabled && board.status_e0_fixture < 0) return 2;
    cpu_reset_z180(cpu);
    if (asci0_pin_fixture >= 0)
        mm_asci_set_pins(((struct z180_device *)cpu)->z180asci->m_chan0,
                         asci0_pin_fixture & 1, (asci0_pin_fixture >> 1) & 1);
    run();
    printf("{\"schema_version\":13,\"stop_reason\":\"%s\",\"attempted_instructions\":%u,"
           "\"fault_address\":%u,\"last_instruction_pc\":%u,\"pc\":%u,\"sp\":%u,"
           "\"cbr\":%u,\"bbr\":%u,\"cbar\":%u,\"iocr\":%u,"
           "\"rcr\":%u,\"dcntl\":%u,\"omcr\":%u,\"tcr\":%u,\"cntr\":%u,"
           "\"itc\":%u,\"il\":%u,\"dstat\":%u,\"board_reads\":%u,\"board_writes\":%u,\"eeprom_reads\":%u,\"eeprom_writes\":%u,\"status80_contacts\":%d,"
           "\"modeled_clocks\":%llu,\"timer0_interrupts\":%u,\"timer1_interrupts\":%u,\"iff1\":%u,"
           "\"int2_period_clocks\":%u,\"clock_input_events\":%u,\"external2_interrupts\":%u,\"status_e0_fixture\":%d,\"trace_pc\":[",
           reason, attempts, fault_address, last_pc,
           cpu_get_state_z180(cpu, Z180_PC), cpu_get_state_z180(cpu, Z180_SP),
           cpu_get_state_z180(cpu, Z180_CBR), cpu_get_state_z180(cpu, Z180_BBR),
           cpu_get_state_z180(cpu, Z180_CBAR), cpu_get_state_z180(cpu, Z180_IOCR),
           cpu_get_state_z180(cpu, Z180_RCR), cpu_get_state_z180(cpu, Z180_DCNTL),
           cpu_get_state_z180(cpu, Z180_OMCR), cpu_get_state_z180(cpu, Z180_TCR),
           cpu_get_state_z180(cpu, Z180_CNTR), cpu_get_state_z180(cpu, Z180_ITC),
           cpu_get_state_z180(cpu, Z180_IL), cpu_get_state_z180(cpu, Z180_DSTAT),
           board.reads, board.writes, board.eeprom_reads, board.eeprom_writes, contact_setting,
           modeled_clocks, timer_interrupts[0], timer_interrupts[1], cpu_get_state_z180(cpu, Z180_IFF1),
           int2_period, clock_input_events, external2_interrupts, board.status_e0_fixture);
    for (i = 0; i < trace_count; i++) printf("%s%u", i ? "," : "", trace[i]);
    printf("],\"trace_tail_first_attempt\":%u,\"trace_tail_pc\":[",
           attempts > 64 ? attempts - 63 : 1);
    for (i = attempts > 64 ? attempts - 64 : 0; i < attempts; i++)
        printf("%s%u", i == (attempts > 64 ? attempts - 64 : 0) ? "" : ",",
               tail_trace[i % 64]);
    printf("],\"panel_profile\":\"%s\",\"panel_writes\":%u,\"panel_data_writes\":%u,"
           "\"panel_command_writes\":%u,\"panel_ignored_writes\":%u,\"panel_cursor\":%u,"
           "\"panel_cursor_visible\":%u,\"panel_pending_bytes\":%u,\"panel_cooldown_clocks\":%u,"
           "\"panel_cells\":[",
           board.panel.enabled ? "cu20026-t23a-lab" : "unconnected",
           board.panel.writes, board.panel.data_writes, board.panel.command_writes,
           board.panel.ignored_writes, board.panel.cursor, board.panel.cursor_visible,
           board.panel.pending_bytes, board.panel.cooldown);
    if (board.panel.enabled)
        for (i = 0; i < 40; i++) printf("%s%u", i ? "," : "", board.panel.cells[i]);
    printf("],\"panel_custom_codes\":[");
    for (i = 0; i < board.panel.custom_count; i++)
        printf("%s%u", i ? "," : "", board.panel.custom_code[i]);
    printf("],\"asci0_pin_fixture\":%d,\"asci0_transmitted_bytes\":%u,"
           "\"asci0_last_transmitted_byte\":%d,\"asci0_interrupts\":%u,",
           asci0_pin_fixture, serial_count[0], serial_count[0] ? (int)serial_last[0] : -1,
           asci_interrupts[0]);
    printf("\"telephony_profile\":\"%s\",\"telephony_resets\":%u,\"telephony_announces\":%u,"
           "\"telephony_commands\":%u,\"telephony_replies\":%u,\"telephony_frames\":%u,"
           "\"telephony_frame_errors\":%u,\"telephony_keepalives\":%u,\"telephony_status_queries\":%u,"
           "\"telephony_unarmed_strobes\":%u,\"telephony_single_commands\":%u,\"telephony_command_log\":[",
           telephony.enabled ? "standin" : "unconnected", telephony.resets, telephony.announces,
           telephony.commands, telephony.replies, telephony.frames, telephony.frame_errors,
           telephony.keepalives, telephony.link_up_queries, tel_unarmed_strobes, telephony.log_count);
    for (i = telephony.log_count > MM_TEL_LOG ? telephony.log_count - MM_TEL_LOG : 0; i < telephony.log_count; i++)
        printf("%s%u", i == (telephony.log_count > MM_TEL_LOG ? telephony.log_count - MM_TEL_LOG : 0) ? "" : ",",
               telephony.log[i % MM_TEL_LOG]);
    printf("],\"coin_profile\":\"%s\",\"coin_status_replies\":%u,\"coin_query_replies\":%u,"
           "\"coin_inserted\":%u,\"coin_bytes_sent\":%u,\"coin_commands\":%u,\"coin_command_log\":[",
           coin.enabled ? "standin" : "unconnected", coin.status_replies, coin.query_replies, coin.coins,
           coin.sent, coin.log_count);
    for (i = coin.log_count > MM_COIN_LOG ? coin.log_count - MM_COIN_LOG : 0; i < coin.log_count; i++)
        printf("%s%u", i == (coin.log_count > MM_COIN_LOG ? coin.log_count - MM_COIN_LOG : 0) ? "" : ",",
               coin.log[i % MM_COIN_LOG]);
    printf("],\"modem_profile\":\"%s\",\"modem_reads\":%u,\"modem_writes\":%u,\"modem_resets\":%u,"
           "\"modem_registers\":[%u,%u,%u,%u],\"modem_write_log\":[",
           modem.enabled ? "control" : "unconnected", modem.reads, modem.writes, modem.resets,
           modem.reg[0], modem.reg[1], modem.reg[2], modem.reg[3]);
    for (i = modem.log_count > MM_MODEM_LOG ? modem.log_count - MM_MODEM_LOG : 0; i < modem.log_count; i++)
        printf("%s%u", i == (modem.log_count > MM_MODEM_LOG ? modem.log_count - MM_MODEM_LOG : 0) ? "" : ",",
               modem.log[i % MM_MODEM_LOG]);
    printf("],\"modem_transmitted_bytes\":%u,\"modem_transmit_log\":[", modem.sent_count);
    for (i = modem.sent_count > MM_MODEM_LOG ? modem.sent_count - MM_MODEM_LOG : 0; i < modem.sent_count; i++)
        printf("%s%u", i == (modem.sent_count > MM_MODEM_LOG ? modem.sent_count - MM_MODEM_LOG : 0) ? "" : ",",
               modem.sent[i % MM_MODEM_LOG]);
    printf("],\"ram_peek\":[");
    for (i = 0; i < peek_count; i++) {
        offs_t physical = peek[i];
        int value = -1;
        cpu_translate_z180(cpu, AS_PROGRAM, 0, &physical);
        if (lab_ram && physical >= 0x40000 && physical <= 0x47fff) value = ram[physical - 0x40000];
        printf("%s[%u,%d]", i ? "," : "", peek[i], value);
    }
    printf("],\"stream\":%d,\"stream_rx_bytes\":%u,\"stream_overruns\":%u,\"stream_messages_in\":%u,\"stream_lag_ms\":%u",
           stream_profile, stream_rx_bytes, stream_overruns, stream_messages_in, stream_lag_ms);
    printf(",\"card_profile\":\"%s\",\"card_count\":%u,\"card_swipes\":%u,\"card_strobes\":%u,"
           "\"card_latched\":%u,\"card_lost\":%u,\"card_overruns\":%u,\"card_busy\":%u,"
           "\"card_chip_reads\":%u,\"dma0_interrupts\":%u,\"coin_hello_replies\":%u,\"coin_raw_bytes\":%u,\"panel_brightness\":%d,\"panel_status_reads\":%u,\"speech_profile\":\"%s\",\"speech_writes\":%u",
           card.enabled ? "standin" : "unconnected", card.count, card.swipes, card.strobes,
           card.latched, card.lost, card.overruns, card.busy, card.chip_reads, dma0_interrupts,
           coin.hello_replies, coin.raw, board.panel.enabled ? board.panel.brightness : -1,
           board.panel.status_reads, voice.enabled ? "chip" : speech_sink ? "silent" : "unconnected", speech_writes);
    printf(",\"voice_starts\":%u,\"voice_stops\":%u,\"voice_cuts\":%u,\"voice_ignored\":%u,"
           "\"voice_beyond\":%u,\"voice_errors\":%u,\"voice_busy\":%d,\"int0_interrupts\":%u,\"voice_log\":[",
           voice.starts, voice.stops, voice.cuts, voice.ignored, voice.beyond, voice.errors,
           voice.busy, int0_interrupts);
    for (i = voice.log_count > MM_VOICE_LOG ? voice.log_count - MM_VOICE_LOG : 0; i < voice.log_count; i++)
        printf("%s[%u,%u,%u,%u]", i == (voice.log_count > MM_VOICE_LOG ? voice.log_count - MM_VOICE_LOG : 0) ? "" : ",",
               voice.log[i % MM_VOICE_LOG].ms, voice.log[i % MM_VOICE_LOG].op,
               voice.log[i % MM_VOICE_LOG].bank, voice.log[i % MM_VOICE_LOG].sample);
    printf("]");
    printf(",\"events_applied\":%u,\"panel_history_dropped\":%u,\"panel_history\":[", events_applied, history_dropped);
    for (i = history_count > MM_HISTORY ? history_count - MM_HISTORY : 0; i < history_count; i++) {
        unsigned j;
        printf("%s[%u,\"", i == (history_count > MM_HISTORY ? history_count - MM_HISTORY : 0) ? "" : ",",
               history_ms[i % MM_HISTORY]);
        for (j = 0; j < 40; j++) printf("%02x", history[i % MM_HISTORY][j]);
        printf("\"]");
    }
    printf("]}\n");
#ifdef MM_DEBUG_RAM /* developer RAM peek, never in a report */
    if (getenv("MMDBG")) { unsigned a; char *e=getenv("MMDBG"); while (*e) { a=strtoul(e,&e,16); fprintf(stderr,"%04x=%02x ",a,ram[(a+0x38000-0x40000)&0x7fff]); if(*e==',')e++; } fprintf(stderr,"\n"); }
    { unsigned k; for (k = 0; k < dbg_n && dbg_n != 99; k++) fprintf(stderr, "hit %05x %u\n", dbg_pc[k], dbg_hits[k]); }
#endif
    return 0;
}
