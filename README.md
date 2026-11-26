# millennium-emulator

**By John Walker, for [KL5 Networks](https://kl5networks.com)** (White Clay Technologies LLC).

An emulated Nortel Millennium payphone: the phone's own firmware running on
a Z180 core, with stand-ins for the boards around it (the display panel,
keypad and telephony board, the coin mechanism, the card reader, the 73K212
modem and the uPD7759 voice chip). It boots to the panel, takes coins and
keys, and can call in to a real [mm_manager](https://github.com/kl5networks/mm_manager)
and take a full INSTALL download, with no phone on the bench.

No firmware is included. You supply your own images; the public dumps in
[muccc/millennium](https://github.com/muccc/millennium) and
[armeniki/Nortel-Millennium](https://github.com/armeniki/Nortel-Millennium)
work, and `millennium_rom_inventory.py` recognises them by SHA-256.

## Why KL5 exists

KL5 is a hobby project, made for fun. Payphones like the Protel and the
Nortel Millennium are great machines, and most of the ones that survive sit
in garages collecting dust because the network they were built for is gone.
KL5 is a way to get them working again: making calls, taking coins, calling
in to a manager and being programmed, on an ordinary VoIP line.

The hope is to bring more people in to set up and use their own phones, and
to make that easy. A phone that calls in needs something to answer it, so
the project grew a management console, and the console grew a fictional
telephone company to run it: KL5 Networks, at
[kl5networks.com](https://kl5networks.com). The company is make-believe.
The phones, the call-ins and this code are real.

## What is here

| File | What it is |
|---|---|
| `millennium_z180.py` | Builds a run, starts the worker, reads its report and its live stream |
| `millennium_ncc_line.py` | Plays the phone line: connects a run's modem to mm_manager through `atshim` |
| `millennium_craft.py` | Plays the person at the phone: waits for a screen, then presses keys, drops coins, swipes cards |
| `millennium_voice.py` | Decodes the voice ROM's uPD7759 samples to audio |
| `millennium_rom_inventory.py` | Names a firmware or voice image from its hash |
| `atshim.py` | A Hayes-style modem on a pty for mm_manager, fed by a TCP socket |
| `z180/` | The board model, compiled into the Z180 core. **GPL-2.0-or-later** |

The Python is MIT and standard library only. It talks to the built worker
only as a separate process, over its command line, a JSON report and a
socket of tagged bytes, so nothing MIT links against GPL code.

## Building the worker

The Z180 core is [mtdev79/z180emu](https://github.com/mtdev79/z180emu),
pinned at `32592c7`. It is never copied in: check it out yourself and the
build refuses any file whose hash differs from `z180/millennium_z180_backend.json`.

    git clone https://github.com/mtdev79/z180emu
    git -C z180emu checkout 32592c760b169bbd71f6a3b41f2e2fb08769b145
    python3 z180/build_millennium_z180.py --source z180emu --output /tmp/mm-z180

## Tests

    python3 -m unittest discover -p 'test_*.py'

That runs what needs no firmware. Point it at the core, the images and an
mm_manager build to run the rest, including full INSTALLs on MTR 1.7 and
MTR 1.20:

    MILLENNIUM_Z180_SOURCE=z180emu \
    MILLENNIUM_MTR17_ROM=.../M06CAA17/NT_FW_1.7_06CAA17_STM27C2001_32DIP.BIN \
    MILLENNIUM_NPA1S01_ROM='.../NPA1S01/dump1/NPA1S01(FW).BIN' \
    MILLENNIUM_VOICEWARE='.../NPA1S01/dump1/NPA1S01(VW).BIN' \
    MILLENNIUM_MM_MANAGER=/path/to/mm_manager \
        python3 -m unittest discover -p 'test_*.py'

## How far it is proven

The board stand-ins were worked out from the firmware's own code, the
panel's data sheet and what real phones do, not from schematics. They are
enough for the firmware to boot, show its screens, take coins and keys and
complete an INSTALL against mm_manager. Anything the firmware does that
none of that exercised may be wrong. Treat it as a test bench, not a
faithful copy of the hardware.

## Notice

Not affiliated with Nortel or any payphone maker. No firmware is included.
For phones you own.

## Licence and credits

The Python is MIT, Copyright (c) 2026 White Clay Technologies LLC: see
`LICENSE`. Everything under `z180/` is GPL-2.0-or-later, because it is
compiled into the Z180 core: see `z180/COPYING` and `z180/README.md`.

With thanks to mtdev79 for z180emu; to Howard M. Harte and the mm_manager
contributors; to the US Payphones wiki
([payphones.telesfor.org](https://payphones.telesfor.org/), Piotr
Czerniak, with PhilTel and danc256), to muCCC
([wiki.muc.ccc.de](https://wiki.muc.ccc.de/millennium:start)) and to
armeniki for the firmware dumps and what is known about the Millennium;
to John Warga for the panel protocol notes; and to the participants in the
Millennium and Protel channels on the PhilTel Discord.
