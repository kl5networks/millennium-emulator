"""Build a pinned external GPL Z180 diagnostic worker; never download ROMs."""

import argparse
import os
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent


def build(source, output):
    if sys.byteorder != 'little':
        raise ValueError('pinned backend requires a little-endian build host')
    source, output = Path(source).resolve(), Path(output).resolve()
    manifest = json.loads((HERE / 'millennium_z180_backend.json').read_text())
    with tempfile.TemporaryDirectory(prefix='millennium-z180-build-') as work:
        work = Path(work)
        for relative, digest in manifest['files'].items():
            data = (source / relative).read_bytes().replace(b'\r\n', b'\n')
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('backend source differs from the pinned manifest')
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        core = work / 'z180/z180.c'
        text = core.read_text()
        for signature, hook in [
            ('UINT8 z180_readcontrol(struct z180_state *cpustate, offs_t port)\n{',
             'mm_internal_access(port - (cpustate->IO_IOCR & 0xc0), 0, 0);'),
            ('void z180_writecontrol(struct z180_state *cpustate, offs_t port, UINT8 data)\n{',
             'mm_internal_access(port - (cpustate->IO_IOCR & 0xc0), 1, data);'),
        ]:
            if text.count(signature) != 1:
                raise ValueError('backend guard insertion point changed')
            text = text.replace(signature, signature + '\n\t' + hook)
        # Export the complete upstream HALT/SLP state: its public accessor masks
        # out the SLP bit. This affects diagnostics, not instruction semantics.
        old = 'case Z180_HALT: return cpustate->HALT &0x1;'
        if text.count(old) != 1:
            raise ValueError('backend sleep accessor changed')
        text = text.replace(old, 'case Z180_HALT: return cpustate->HALT;')
        # UM005004 p.153: RESET aborts CSI/O but retains TRDR contents.
        old = '\tcpustate->IO_TRDR    = Z180_TRDR_RESET;'
        if text.count(old) != 1:
            raise ValueError('backend CSI/O data reset contract changed')
        text = text.replace(old, '')
        old = '\tmemset(cpustate->io, 0, sizeof(cpustate->io));'
        if text.count(old) != 1:
            raise ValueError('backend CSI/O retained storage changed')
        text = text.replace(old, '''\tmemset(cpustate->io, 0, Z180_TRDR);
\tmemset(cpustate->io + Z180_TRDR + 1, 0, sizeof(cpustate->io) - Z180_TRDR - 1);''')
        old = 'logerror("Z180 \'%s\' TRDR   rd $%02x ($%02x)\\n", cpustate->device->m_tag, data, cpustate->io[port]);'
        if text.count(old) != 1:
            raise ValueError('backend CSI/O diagnostic changed')
        text = text.replace(old, old.replace('logerror(', 'LOG('))
        # External-clock CSI/O uses explicit sampled symbols, never a CNTR
        # poll or CPU-clock fallback. The board CKS/RXS producer is unresolved.
        csio_patches = {
            '\tUINT8   io[64];                         /* 64 internal 8 bit registers */':
                '\tstruct mm_csio mm_csio;\n\tUINT8   io[64];                         /* 64 internal 8 bit registers */',
            '\t\t\tdata &= ~0x10; // Super Famicom Box sets the TE bit then wants it to be toggled after 8 bits transmitted': '',
            '\t\t\tdata = cpustate->IO_TRDR & Z180_TRDR_RMASK;':
                '\t\t\tdata = cpustate->IO_TRDR & Z180_TRDR_RMASK;\n\t\t\tcpustate->IO_CNTR &= ~Z180_CNTR_EF;',
            '\t\t\tcpustate->IO_CNTR = (cpustate->IO_CNTR & ~Z180_CNTR_WMASK) | (data & Z180_CNTR_WMASK);':
                '''\t\t\tmm_csio_control(&cpustate->mm_csio, cpustate->IO_CNTR, data, cpustate->IO_TRDR);
\t\t\tcpustate->IO_CNTR = (cpustate->IO_CNTR & ~Z180_CNTR_WMASK) | (data & Z180_CNTR_WMASK);''',
            '\t\t\tcpustate->IO_TRDR = (cpustate->IO_TRDR & ~Z180_TRDR_WMASK) | (data & Z180_TRDR_WMASK);':
                '''\t\t\tcpustate->IO_TRDR = (cpustate->IO_TRDR & ~Z180_TRDR_WMASK) | (data & Z180_TRDR_WMASK);
\t\t\tmm_csio_data_write(&cpustate->mm_csio, data);
\t\t\tcpustate->IO_CNTR &= ~Z180_CNTR_EF;''',
            '\tcpustate->IO_CNTR    = Z180_CNTR_RESET;':
                '\tmm_csio_reset(&cpustate->mm_csio);\n\tcpustate->IO_CNTR    = Z180_CNTR_RESET;',
            'void clock_timers(struct z180_state *cpustate)\n{':
                '''/* Explicit external bit boundary; no fabricated RXS/CKS input. */
int mm_csio_external_symbol(device_t *device, unsigned sampled_bit)
{
\tstruct z180_state *cpustate = get_safe_token(device);
\treturn mm_csio_symbol(&cpustate->mm_csio, &cpustate->IO_CNTR,
\t\t&cpustate->IO_TRDR, sampled_bit);
}

unsigned mm_csio_remaining(device_t *device)
{
\treturn get_safe_token(device)->mm_csio.remaining;
}

/* One falling edge on /DREQ0. Returns 1 when latched for the next DMA
 * slice, 0 when channel 0 is not enabled (the edge is lost, as nothing
 * samples it), -1 when the previous edge has not been served yet. */
int mm_dma0_request(device_t *device)
{
\tstruct z180_state *cpustate = get_safe_token(device);
\tif (!(cpustate->IO_DSTAT & Z180_DSTAT_DE0)) return 0;
\tif (cpustate->iol & Z180_DREQ0) return -1;
\tcpustate->iol |= Z180_DREQ0;
\treturn 1;
}

void clock_timers(struct z180_state *cpustate)
{''',
        }
        for old, new in csio_patches.items():
            if text.count(old) != 1:
                raise ValueError('backend CSI/O symbol contract changed')
            text = text.replace(old, new)
        old = 'LOG("Z180 bogus write control reg %04X:$%02X\\n", cpustate->device->m_tag, port, data);'
        if text.count(old) != 1:
            raise ValueError('backend diagnostic format changed')
        text = text.replace(old, 'LOG("Z180 %s bogus write control reg %04X:$%02X\\n", cpustate->device->m_tag, port, data);')
        # Zilog UM005004: pp. 15-16 (OMCR), 29-30 and appendix G (DCNTL),
        # 42 (ICR). These checked corrections affect register contracts only;
        # refresh, M1 pulses and CPU wait-state timing are still not modeled.
        corrections = {
            '#define Z180_DCNTL_RESET        0x00': '#define Z180_DCNTL_RESET        0xf0',
            '#define Z180_OMCR_RESET         0x00': '#define Z180_OMCR_RESET         0xe0',
            '#define Z180_OMCR_RMASK         0xff': '#define Z180_OMCR_RMASK         0xe0',
            '#define Z180_OMCR_WMASK         0xff': '#define Z180_OMCR_WMASK         0xa0',
            '#define Z180_IOCR_RMASK         0xff': '#define Z180_IOCR_RMASK         0xe0',
            '#define Z180_IOCR_WMASK         0xff': '#define Z180_IOCR_WMASK         0xe0',
            '#define Z180_RCR_REFW           0x80': '#define Z180_RCR_REFW           0x40',
            '#define Z180_CNTR_RMASK         0xff': '#define Z180_CNTR_RMASK         0xf7',
            '#define Z180_CNTR_WMASK         0x7f': '#define Z180_CNTR_WMASK         0x77',
            '#define Z180_RLDR1L_RESET       0x00': '#define Z180_RLDR1L_RESET       0xff',
            '#define Z180_RLDR1H_RESET       0x00': '#define Z180_RLDR1H_RESET       0xff',
        }
        for old, new in corrections.items():
            if text.count(old) != 1:
                raise ValueError('backend configuration contract changed')
            text = text.replace(old, new)
        # UM005004 pp. 157-159: both counters/reloads reset to ffff, a stopped
        # counter is freely accessed, and a running low-byte read latches high.
        timer_corrections = {
            'cpustate->tmdr_value[1] = (cpustate->tmdr_value[1] & 0x00ff) | cpustate->IO_TMDR1H;':
                'cpustate->tmdr_value[1] = (cpustate->tmdr_value[1] & 0x00ff) | (cpustate->IO_TMDR1H << 8);',
            'if ((cpustate->IO_TCR & Z180_TCR_TDE0) == 0)':
                'if ((cpustate->IO_TCR & Z180_TCR_TDE0) != 0)',
            'if ((cpustate->IO_TCR & Z180_TCR_TDE1) == 0)':
                'if ((cpustate->IO_TCR & Z180_TCR_TDE1) != 0)',
        }
        for old, new in timer_corrections.items():
            if text.count(old) != 1:
                raise ValueError('backend timer register contract changed')
            text = text.replace(old, new)
        # Figure 64 starts from the programmed TMDR value, not a forced zero.
        for channel in (0, 1):
            old = f'''\t\t\t\tif (!(old & Z180_TCR_TDE{channel}) && (cpustate->IO_TCR & Z180_TCR_TDE{channel}))
\t\t\t\t\tcpustate->tmdr_value[{channel}] = 0; //cpustate->IO_RLDR{channel}L | (cpustate->IO_RLDR{channel}H << 8);'''
            if text.count(old) != 1:
                raise ValueError('backend timer enable contract changed')
            text = text.replace(old, '')
        # Stopped TMDR accesses are free byte accesses (UM005004 p. 158).
        # Discard a running read snapshot when its channel is stopped so a
        # subsequent stopped read/reprogramming cannot return obsolete high.
        old = '\t\t\t\tcpustate->IO_TCR = (cpustate->IO_TCR & ~Z180_TCR_WMASK) | (data & Z180_TCR_WMASK);'
        if text.count(old) != 1:
            raise ValueError('backend timer stop latch contract changed')
        text = text.replace(old, old + '\n\t\t\t\tcpustate->tmdr_latch &= cpustate->IO_TCR & 3;')
        # The documented TCR -> TMDR acknowledgment requires distinct reads.
        # Repeated TCR reads only arm; unarmed TMDR reads cannot arm/clear.
        read_start = text.index('UINT8 z180_readcontrol(struct z180_state *cpustate, offs_t port)\n{')
        read_end = text.index('void z180_writecontrol(', read_start)
        reads = text[read_start:read_end]
        for channel in (0, 1):
            old = f'''\t\t\tif(cpustate->read_tcr_tmdr[{channel}])
\t\t\t{{
\t\t\t\tcpustate->tif[{channel}] = 0; // reset TIF{channel}
\t\t\t\tcpustate->read_tcr_tmdr[{channel}] = 0;
\t\t\t}}
\t\t\telse
\t\t\t{{
\t\t\t\tcpustate->read_tcr_tmdr[{channel}] = 1;
\t\t\t}}'''
            if reads.count(old) != 3:
                raise ValueError('backend timer acknowledgment contract changed')
            start = reads.index('case Z180_TCR:')
            end = reads.index('case Z180_IO11:', start)
            tcr = reads[start:end]
            if tcr.count(old) != 1:
                raise ValueError('backend TCR acknowledgment changed')
            reads = reads[:start] + tcr.replace(old, f'\t\t\tcpustate->read_tcr_tmdr[{channel}] = 1;') + reads[end:]
            # Remaining two blocks belong to this channel's low/high reads.
            reads = reads.replace(old, old.split('\n\t\t\telse')[0])
        text = text[:read_start] + reads + text[read_end:]
        # UM005004 p. 162: TIF is set when TMDR decrements to zero, not
        # one divider edge later when the reload value is loaded.
        for channel in (0, 1):
            old = f'''\t\t\t\tcpustate->tmdr_value[{channel}] = cpustate->IO_RLDR{channel}L | (cpustate->IO_RLDR{channel}H << 8);
\t\t\t\tcpustate->tif[{channel}] = 1;
\t\t\t}}
\t\t\telse
\t\t\t\tcpustate->tmdr_value[{channel}]--;'''
            new = f'''\t\t\t\tcpustate->tmdr_value[{channel}] = cpustate->IO_RLDR{channel}L | (cpustate->IO_RLDR{channel}H << 8);
\t\t\t}}
\t\t\telse
\t\t\t{{
\t\t\t\tcpustate->tmdr_value[{channel}]--;
\t\t\t\tif (cpustate->tmdr_value[{channel}] == 0)
\t\t\t\t\tcpustate->tif[{channel}] = 1;
\t\t\t}}'''
            if text.count(old) != 1:
                raise ValueError('backend timer flag edge contract changed')
            text = text.replace(old, new)
        # Timer requests are levels derived from TIF/TIE at each arbitration
        # boundary. Global IFF/EI gates delivery, not request generation.
        for channel in (0, 1):
            old = f'''\t\tif((cpustate->IO_TCR & Z180_TCR_TIE{channel}) && cpustate->tif[{channel}])
\t\t{{
\t\t\t// check if we can take the interrupt
\t\t\tif(cpustate->IFF1 && !cpustate->after_EI)
\t\t\t{{
\t\t\t\tcpustate->int_pending[Z180_INT_PRT{channel}] = 1;
\t\t\t}}
\t\t}}'''
            if text.count(old) != 1:
                raise ValueError('backend timer request contract changed')
            text = text.replace(old, '')
        old = 'int check_interrupts(struct z180_state *cpustate)\n{\n\tint i;\n\tint cycles = 0;'
        new = old + '''
\tint prt0 = (cpustate->IO_TCR & Z180_TCR_TIE0) && cpustate->tif[0];
\tint prt1 = (cpustate->IO_TCR & Z180_TCR_TIE1) && cpustate->tif[1];
\tint ext0 = cpustate->irq_state[0] != CLEAR_LINE && (cpustate->IO_ITC & Z180_ITC_ITE0);
\tint ext1 = cpustate->irq_state[1] != CLEAR_LINE && (cpustate->IO_ITC & Z180_ITC_ITE1);
\tint ext2 = cpustate->irq_state[2] != CLEAR_LINE && (cpustate->IO_ITC & Z180_ITC_ITE2);
\tint csio = (cpustate->IO_CNTR & 0xc0) == 0xc0;
\tint asci0 = mm_asci_request(cpustate->device->z180asci->m_chan0);
\tint asci1 = mm_asci_request(cpustate->device->z180asci->m_chan1);
\tint dma0 = (cpustate->IO_DSTAT & (Z180_DSTAT_DIE0 | Z180_DSTAT_DE0)) == Z180_DSTAT_DIE0;
\tcpustate->int_pending[Z180_INT_DMA0] = dma0;
\tcpustate->int_pending[Z180_INT_IRQ0] = !!ext0;
\tcpustate->int_pending[Z180_INT_IRQ1] = !!ext1;
\tcpustate->int_pending[Z180_INT_IRQ2] = !!ext2;
\tcpustate->int_pending[Z180_INT_PRT0] = prt0;
\tcpustate->int_pending[Z180_INT_PRT1] = prt1;
\tcpustate->int_pending[Z180_INT_CSIO] = csio;
\tcpustate->int_pending[Z180_INT_ASCI0] = asci0;
\tcpustate->int_pending[Z180_INT_ASCI1] = asci1;
\t/* Enabled peripheral requests wake SLP even with global IFF1 clear. */
\tif (cpustate->HALT == 2 && (prt0 || prt1 || dma0 || ext0 || ext1 || ext2 || csio || asci0 || asci1))
\t\tLEAVE_HALT(cpustate);'''
        if text.count(old) != 1:
            raise ValueError('backend timer arbitration contract changed')
        text = text.replace(old, new)
        old = '\t\t\t\tcycles += take_interrupt(cpustate, i);'
        if text.count(old) != 1:
            raise ValueError('backend timer interrupt observer changed')
        text = text.replace(old, '''\t\t\t\tif (i == Z180_INT_PRT0 || i == Z180_INT_PRT1)
\t\t\t\t\tmm_timer_interrupt(i == Z180_INT_PRT1);
\t\t\t\tif (i == Z180_INT_IRQ2) mm_external2_interrupt();
\t\t\t\tif (i == Z180_INT_DMA0) mm_dma0_interrupt();
\t\t\t\tif (i == Z180_INT_ASCI0 || i == Z180_INT_ASCI1)
\t\t\t\t\tmm_asci_interrupt(i == Z180_INT_ASCI1);
''' + old)
        # External INT pins are level requests; release/mask cancels an
        # unserved request even when a higher-priority source won last time.
        for channel in range(3):
            old = f'''\t\tif (cpustate->irq_state[{channel}] != CLEAR_LINE && (cpustate->IO_ITC & Z180_ITC_ITE{channel}) == Z180_ITC_ITE{channel})
\t\t\tcpustate->int_pending[Z180_INT_IRQ{channel}] = 1;'''
            if text.count(old) != 1:
                raise ValueError('backend external interrupt request contract changed')
            text = text.replace(old, '')
        ops = work / 'z180/z180ops.h'
        ops_text = ops.read_text()
        old = '\t\t(cs)->HALT = 0;                                         \\\n\t\t(cs)->_PC++;                                            \\'
        new = '\t\tif ((cs)->HALT == 1) (cs)->_PC++;                       \\\n\t\t(cs)->HALT = 0;                                         \\'
        if ops_text.count(old) != 1:
            raise ValueError('backend sleep resume contract changed')
        ops.write_text(ops_text.replace(old, new))
        # UM005004 pp. 95-97: DE changes require matching active-low DWE=0.
        # DIE bits are independent;
        # DWE reads one, DME is software read-only and latched by an enable.
        old = '\t\t\tcpustate->IO_DSTAT = (cpustate->IO_DSTAT & ~Z180_DSTAT_WMASK) | (data & Z180_DSTAT_WMASK);'
        new = '''\t\t\tcpustate->IO_DSTAT = (cpustate->IO_DSTAT & 0xc1) | 0x30 | (data & 0x0c);
\t\t\tif (!(data & Z180_DSTAT_DWE1))
\t\t\t\tcpustate->IO_DSTAT = (cpustate->IO_DSTAT & ~Z180_DSTAT_DE1) | (data & Z180_DSTAT_DE1);
\t\t\tif (!(data & Z180_DSTAT_DWE0))
\t\t\t\tcpustate->IO_DSTAT = (cpustate->IO_DSTAT & ~Z180_DSTAT_DE0) | (data & Z180_DSTAT_DE0);'''
        if text.count(old) != 1:
            raise ValueError('backend DMA write protection changed')
        text = text.replace(old, new)
        # UM005004 pp. 99-101 (DCNTL): DMS0, bit 2, selects edge sensing for
        # /DREQ0; DIM0 is channel 1's I/O-memory mode and says nothing about
        # channel 0. Upstream tested DIM0, so an edge-sensed request was
        # treated as a held level and repeated on every slice. The card
        # reader's DMA (DCNTL $F4) is edge-sensed: one strobe, one byte.
        old = '\t\t\t\tif (cpustate->IO_DCNTL & Z180_DCNTL_DIM0)'
        if text.count(old) != 4:
            raise ValueError('backend DMA request sensing changed')
        text = text.replace(old, '\t\t\t\tif (cpustate->IO_DCNTL & Z180_DCNTL_DMS0)')
        # UM005004 p. 97 (DIE0): the end-of-transfer request is a level, DIE0
        # with DE0 clear, arbitrated like every other source. Upstream set a
        # one-shot pending flag at terminal count, and only if IFF1 happened
        # to be set at that instant, so a transfer that ended under DI lost
        # its interrupt for good.
        old = '''\t\tif (cpustate->IO_DSTAT & Z180_DSTAT_DIE0 && cpustate->IFF1)
\t\t\tcpustate->int_pending[Z180_INT_DMA0] = 1;'''
        if text.count(old) != 1:
            raise ValueError('backend DMA end interrupt changed')
        text = text.replace(old, '')
        # Isolated TOUT logic model. Unspecified initial toggle level stays
        # unknown until software forces a level; no board pin is driven.
        output_patches = {
            'UINT8   timer_cnt;': 'int     mm_prt_output_level;\n\tUINT8   timer_cnt;',
            '\n\tcpustate->timer_cnt = 0;': '\n\tcpustate->timer_cnt = 0;\n\tcpustate->mm_prt_output_level = -1;',
            '\t\t\t\tcpustate->tmdr_latch &= cpustate->IO_TCR & 3;':
                '''\t\t\t\tcpustate->tmdr_latch &= cpustate->IO_TCR & 3;
\t\t\t\tif ((cpustate->IO_TCR & 12) == 8) cpustate->mm_prt_output_level = 0;
\t\t\t\tif ((cpustate->IO_TCR & 12) == 12) cpustate->mm_prt_output_level = 1;''',
            '''\t\t\t\tif (cpustate->tmdr_value[1] == 0)
\t\t\t\t\tcpustate->tif[1] = 1;''':
                '''\t\t\t\tif (cpustate->tmdr_value[1] == 0)
\t\t\t\t{
\t\t\t\t\tcpustate->tif[1] = 1;
\t\t\t\t\tif ((cpustate->IO_TCR & 12) == 4 && cpustate->mm_prt_output_level >= 0)
\t\t\t\t\t\tcpustate->mm_prt_output_level ^= 1;
\t\t\t\t}''',
            'void clock_timers(struct z180_state *cpustate)\n{':
                '''int mm_prt_output(device_t *device)
{
\tstruct z180_state *cpustate = get_safe_token(device);
\tif (!(cpustate->IO_TCR & 12)) return -2; /* address function */
\treturn cpustate->mm_prt_output_level; /* -1: unknown toggle baseline */
}

void clock_timers(struct z180_state *cpustate)
{''',
        }
        for old, new in output_patches.items():
            if text.count(old) != 1:
                raise ValueError('backend timer output contract changed')
            text = text.replace(old, new)
        # Advance ASCI and the optional laboratory board each modeled phi.
        # Serial data/pin guards remain in the adapter; original profiles also
        # retain the active-control guards.
        old = '\t\tclock_timers(cpustate);\n\t}\n}'
        new = '''\t\tclock_timers(cpustate);
\t\tz180asci_channel_device_timer(cpustate->device->z180asci->m_chan0);
\t\tz180asci_channel_device_timer(cpustate->device->z180asci->m_chan1);
\t\tmm_board_clock();
\t}
}'''
        if text.count(old) != 1:
            raise ValueError('backend ASCI CPU clock insertion point changed')
        text = text.replace(old, new)
        old = '\tif (cpustate->daisy != NULL)\n\t\tz80_daisy_chain_post_reset(cpustate->daisy);'
        if text.count(old) != 1:
            raise ValueError('backend ASCI CPU reset insertion point changed')
        text = text.replace(old, old + '\n\tz180asci_device_reset(cpustate->device->z180asci);')
        # z180.h must precede the status helpers' channel definitions.
        old = '#include "z180.h"'
        if text.count(old) != 1:
            raise ValueError('backend CPU ASCI include contract changed')
        text = text.replace(old, old + '\n#include "millennium_asci.h"')
        core.write_text('#include "millennium_csio.h"\nextern void mm_internal_access(unsigned, int, unsigned);\nextern void mm_board_clock(void);\nextern void mm_timer_interrupt(unsigned);\nextern void mm_external2_interrupt(void);\nextern void mm_dma0_interrupt(void);\nextern void mm_asci_interrupt(unsigned);\n' + text)
        asci = work / 'z180/z180asci.c'
        text = asci.read_text()
        old = '#include "z180.h"'
        if text.count(old) != 1:
            raise ValueError('backend ASCI include contract changed')
        text = text.replace(old, old + '\n#include "millennium_asci.h"')
        start = text.index('void z180asci_channel_check_interrupts(struct z180asci_channel *ch)\n{')
        end = text.index('\n}\n', start) + 3
        text = text[:start] + '''void z180asci_channel_check_interrupts(struct z180asci_channel *ch)
{
    /* Peripheral condition persists independently of global IFF delivery. */
    z180_set_asci_irq(ch->m_uart->m_owner, ch->m_index, mm_asci_request(ch));
}
''' + text[end:]
        old = '''\t\t\tdata |= !(ch->m_cts&1);'''
        if text.count(old) != 1:
            raise ValueError('backend ASCI CTS read changed')
        text = text.replace(old, '\t\t\tdata |= ch->m_cts ? CNTLB_CTS : 0;')
        old = '''\t\t\tdata = ch->m_stat;
\t\t\tif (ch->m_index==0) {
\t\t\t\tdata &= ~STAT_DCD0;
\t\t\t\tdata |= !(ch->m_dcd&1);
\t\t\t}'''
        if text.count(old) != 1:
            raise ValueError('backend ASCI status read changed')
        text = text.replace(old, '\t\t\tdata = mm_asci_read_status(ch);\n\t\t\tz180asci_channel_check_interrupts(ch);')
        # CPU reset cannot drive external CTS/DCD inputs to arbitrary levels.
        for old in ('\tch->m_cts = 0;', '\t\tch->m_dcd = 0;'):
            if text.count(old) != 1:
                raise ValueError('backend ASCI reset pin contract changed')
            text = text.replace(old, '')
        old = '\t// reset interrupts\n}'
        if text.count(old) != 1:
            raise ValueError('backend ASCI post-reset pin contract changed')
        text = text.replace(old, '\tmm_asci_set_pins(ch, ch->m_cts, ch->m_dcd);\n' + old)
        # Register enables/error acknowledgments must update the level request
        # immediately; arbitration also recomputes it before delivery/wakeup.
        old = '\n\n//-------------------------------------------------\n//  data_read - read data register from fifo'
        end = text.index(old)
        start = text.index('void z180asci_channel_register_write(struct z180asci_channel *ch, uint8_t reg, uint8_t data)\n{')
        body = text[start:end]
        if not body.endswith('\n}\n'):
            raise ValueError('backend ASCI write contract changed')
        text = text[:start] + body[:-3] + '\n\tz180asci_channel_check_interrupts(ch);\n}\n' + text[end:]
        # UM005004 pp. 125-133: RTS0 reads its software control bit, while
        # MPBR/EFR reads received MPB and writing zero clears the error flags.
        # CTS/DCD reads remain guarded until their board wiring is verified.
        old = '''\t\t\tif (ch->m_index==0) {
\t\t\t\tdata &= ~CNTLA_RTS0;
\t\t\t\tdata |= !(ch->m_rts&1);
\t\t\t}'''
        if text.count(old) != 1:
            raise ValueError('backend ASCI control read changed')
        text = text.replace(old, '')
        old = '\t\t\tch->m_cntla = data;'
        if text.count(old) != 1:
            raise ValueError('backend ASCI error reset changed')
        text = text.replace(old, '''\t\t\tif (!(data & CNTLA_MPBR))
\t\t\t\tch->m_stat &= ~(STAT_OVRN | STAT_PE | STAT_FE);
\t\t\tch->m_cntla = (data & ~CNTLA_MPBR) | (ch->m_cntla & CNTLA_MPBR);''')
        # UM005004 pp. 141-144: base internal bit period is PS * 2**SS
        # * DR; only the enhanced time-constant mode has an extra factor 2.
        # This callback model is not a pin-level sampling implementation.
        old = 'return (rate / (2 * z180asci_channel_get_clock_mode(ch)));'
        new = 'return (rate / ((ch->m_asext & ASEXT_BRGM ? 2 : 1) * z180asci_channel_get_clock_mode(ch)));'
        if text.count(old) != 1:
            raise ValueError('backend ASCI baud rate contract changed')
        text = text.replace(old, new)
        old = '''\tif (!--ch->m_brg_timer) {
\t\tz180asci_channel_rcv_callback(ch);
\t\tz180asci_channel_tra_callback(ch);
\t\tch->m_brg_timer = ch->m_brg_const;
\t}'''
        new = '''\t/* External CKA needs explicit edges; CPU clocks cannot supply them. */
\tif (!ch->m_brg_period) return;
\tif (!--ch->m_brg_timer) {
\t\tz180asci_channel_rcv_callback(ch);
\t\tz180asci_channel_tra_callback(ch);
\t\tch->m_brg_timer = ch->m_brg_period;
\t}'''
        if text.count(old) != 1:
            raise ValueError('backend ASCI bit clock contract changed')
        text = text.replace(old, new)
        old = '\t// reset interrupts\n}'
        new = '''\t/* Reset aborts frames and clears the model divider phase. */
\tch->m_brg_period = ch->m_brg_timer = 0;
\tch->m_brg_const = ch->m_brg_rate = 0;
\tz180asci_channel_update_serial(ch);
\t// reset interrupts
}'''
        if text.count(old) != 1:
            raise ValueError('backend ASCI clock reset contract changed')
        text = text.replace(old, new)
        old = '\tint clocks = z180asci_channel_get_clock_mode(ch);'
        new = old + '''
\tunsigned int period = (ch->m_cntlb & CNTLB_SS) == 7 ? 0 :
\t\tz180asci_channel_get_brg_const(ch) * clocks * (ch->m_asext & ASEXT_BRGM ? 2 : 1);
\t/* Model policy: speed/source changes restart phase, equal periods retain it.
\t * Exact silicon reconfiguration phase is still unverified. */
\tif (period != ch->m_brg_period) ch->m_brg_timer = period;
\tch->m_brg_period = period;
\tif (!period) ch->m_brg_const = ch->m_brg_rate = 0;'''
        if text.count(old) != 1:
            raise ValueError('backend ASCI reconfiguration contract changed')
        text = text.replace(old, new)
        # TE/RE clearing interrupts an in-progress operation, retaining status.
        old = '\t\t\tch->m_cntla = (data & ~CNTLA_MPBR) | (ch->m_cntla & CNTLA_MPBR);'
        new = '''\t\t\tif (!(data & CNTLA_TE)) ch->tx_bits_rem = 0;
\t\t\tif (!(data & CNTLA_RE)) ch->rx_bits_rem = 0;
''' + old
        if text.count(old) != 1:
            raise ValueError('backend ASCI disable contract changed')
        text = text.replace(old, new)
        asci.write_text(text)
        header = work / 'z180/z180asci.h'
        text = header.read_text()
        old = '\tuint16_t m_brg_timer;'
        new = '\tunsigned int m_brg_timer;\n\tunsigned int m_brg_period;'
        if text.count(old) != 1:
            raise ValueError('backend ASCI divider width changed')
        header.write_text(text.replace(old, new))
        units = ['z180.c', 'z180asci.c', 'z80daisy.c', 'z80scc.c']
        output.parent.mkdir(parents=True, exist_ok=True)
        # MILLENNIUM_Z180_DEBUG=1 compiles in developer traces (MMDBG RAM,
        # MMPC program counters, MMW a RAM write, MMTX ASCI0 bytes) that print
        # to stderr when their variable is set -- which the wrapper treats
        # as a failed run, so they never reach a report.
        debug = ['-DMM_DEBUG_RAM'] if os.environ.get('MILLENNIUM_Z180_DEBUG') else []
        subprocess.run(['cc', '-std=gnu89', '-fcommon', '-O2', *debug, '-I', str(work / 'z180'), '-I', str(HERE),
                        str(HERE / 'millennium_z180_worker.c'),
                        *(str(work / 'z180' / unit) for unit in units),
                        '-o', str(output)], check=True, timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == '__main__':
    main()
