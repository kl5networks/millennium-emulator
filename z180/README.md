# Millennium Z180 worker: GPL-2.0-or-later, kept apart on purpose

Everything in this directory is **GPL-2.0-or-later**, unlike the rest of
this repository, which is MIT. It is fenced off in its own directory so the
boundary is a directory, not a header comment nobody reads.

- `millennium_z180_worker.c` and the `millennium_*.h` headers are the
  board model, including the inferred telephony-board, coin-mechanism,
  73K212 modem, mag-stripe card-reader and uPD7759 voice-chip stand-ins. They
  are compiled *into* the Z180 core, so they take its licence.
- `build_millennium_z180.py` patches that core at build time. Its find and
  replace strings quote the upstream source, so it is GPL too.
- `millennium_z180_backend.json` pins the upstream core,
  [mtdev79/z180emu](https://github.com/mtdev79/z180emu) at
  `32592c760b169bbd71f6a3b41f2e2fb08769b145`, file by file by SHA-256.
  The core itself is never vendored: CI checks it out, and the build
  refuses any file whose hash differs.

The MIT side talks to the built worker only as a **separate process**,
over its command line, a bounded JSON report on stdout and, in a
streamed run, a socket that carries tagged bytes
(`../millennium_z180.py`, `../millennium_ncc_line.py`,
`../millennium_craft.py`). The socket
carries data, never code: serial bytes, register values, panel cells. Nothing MIT links against it or imports it,
and only the tests call the build script here. Keep it that way.

## The stream, and what changed in it

`millennium_z180_worker.c` documents every tag. One was added with the
voice chip, and only a run that asks for the chip ever sees it, so a host
written before it is unaffected:

| tag | bytes after it | meaning |
|---|---|---|
| `V` | op, bank, sample, ms (u32 LE) | the voice chip (`speech=chip` only): op 1 = sample `sample` of bank `bank` starts, 0 = it played out (BUSY drops; INT0 asks for the next), 2 = the firmware reset the chip mid-sample |

`millennium_z180.STREAM_LENGTHS` has it (`b'V': 7`) and
`millennium_z180.voice_event(payload)` reads it. The audio is not in the
stream: a host plays `millennium_voice.decode(voiceware, bank, sample)`
(MIT, `../millennium_voice.py`) from a start and stops on a cut.

The batch report went to **schema 13** with it: `voice_starts`,
`voice_stops`, `voice_cuts`, `voice_ignored`, `voice_beyond`,
`voice_errors`, `voice_busy`, `int0_interrupts`, and `voice_log` (the
last 64 events as `[ms, op, bank, sample]`). All are zero or empty
unless the chip was asked for. `speech_profile` can now be `chip`.

The worker takes the voiceware as `voice=PATH` beside `speech=chip`; it
reads it only to time BUSY (`millennium_voice.h`), never to make sound.

Build:

    python3 z180/build_millennium_z180.py --source /path/to/z180emu --output /tmp/mm-z180

No firmware is committed. The images are public (muccc/millennium,
armeniki/Nortel-Millennium) and are passed in by path.
