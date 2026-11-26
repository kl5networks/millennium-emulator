#!/usr/bin/env python3
"""A Hayes modem that isn't: `mm_manager`'s serial port, fed by a relay.

`mm_manager` has no raw-pipe transport. `-f FILE` without `-m` is a
read-only playback of a captured session; `-m` makes it a *serial device*
and it talks AT to whatever is on the other end. So bridging
`app_softmodem` to it needs something that opens a pty, speaks Hayes on
one side, and passes bytes on the other.

This is that something. A softmodem (Asterisk's `app_softmodem`, or
the emulator's line) connects to the shim's wire port; the shim RINGs,
`mm_manager` answers ATA and gets CONNECT, and bytes pass through:

    phone -> ATA -> Asterisk, Softmodem() demodulates
                  -> TCP -> mm-atshim  --wire listen:127.0.0.1:2424
                            mm_manager -f <the pty this prints> -m -w

Bind the wire port to loopback or a private tunnel address, never
0.0.0.0: it is an unauthenticated byte pipe into the manager.

Standard library only; this has to run anywhere the manager does.

## Two traps, both already paid for

**`-w` is not optional.** `mm_manager`'s `monitor_carrier` defaults on, and
`receive_mm_packet()` asks the port for modem status with `TIOCMGET`, which
is **ENOTTY on a pty** -- verified on both the master and the slave side.
The ioctl's return value is ignored, so `status` stays 0, no carrier is
seen, and it bails "Carrier lost, bailing." on the very first packet. That
failure looks like a dead line and is not one. Run it with `-w`.

**Responses must be bare.** `mm_manager` matches modem replies with
`strstr` against {"OK", "ERROR", "RING", "CONNECT", "NO CARRIER", "NULL"}
**in that order, first match wins**. A chatty response like `AT&F0 OK` or a
banner containing any of those words is read as whichever token appears in
the array first, not the one that is true. So this never echoes commands
and never decorates a reply -- `AtModem.RESPONSES` is the whole vocabulary,
and `test_atshim.py` asserts none of them contains another.
"""
import argparse
import errno
import os
import selectors
import socket
import sys
import time

# The exact strings mm_manager scans for, in its own order. Kept as a tuple
# so the test can assert no earlier token appears inside a later one --
# which is the failure mode the module docstring describes.
RESPONSE_TOKENS = ("OK", "ERROR", "RING", "CONNECT", "NO CARRIER", "NULL")

CR = b"\r\n"

# A real modem rings until answered; mm_manager may wait for a second RING
# or rely on ATS0. Without a repeat the S0 path could never fire.
RING_SECONDS = 3.0


class AtModem(object):
    """The Hayes side, as a state machine: bytes in, responses out.

    No sockets and no pty, for the same reason `box/agent.py` has none --
    the cases worth getting right (a relay that connects mid-command, a
    hangup during a packet, a manager that re-inits twice) are exactly the
    ones that are tedious to arrange against real I/O.

    `feed()` takes whatever arrived from the manager and returns the bytes
    to send back. `carrier_up()` / `carrier_down()` are called when the
    wire side connects and disconnects.
    """

    def __init__(self, auto_answer=True):
        self.auto_answer = auto_answer
        self.buf = b""
        self.online = False       # True once CONNECT has been sent
        self.ringing = False      # wire is up, manager has not answered
        self.wire_up = False
        self.rings_sent = 0
        self.s0 = 0               # ATS0=n, rings before auto-answer

    # -- the wire side ---------------------------------------------------

    def carrier_up(self):
        """The wire connected. That is the RING.

        S0 is honoured on THIS ring, not only on the next one. mm_manager's
        default init string is `ATE=1 S0=1 S7=3 &D2 +MS=B212`, so S0=1 is
        the case that actually ships, and S0=1 means answer on the first
        ring. Leaving it to ring_again() cost a measured 3 s of dead air
        before CONNECT -- against the manager's own S7=3 carrier wait, and
        with the Millennium already trained and waiting at the far end.
        The earlier code was right for S0=2 and only for S0=2, which is
        what the test that covered it happened to use.
        """
        self.wire_up = True
        if self.online:
            return b""
        self.ringing = True
        self.rings_sent = 1
        out = CR + b"RING" + CR
        if self.s0 and self.rings_sent >= self.s0:
            out += self._answer()
        return out

    def ring_again(self):
        """A further RING, for a manager waiting on S0 auto-answer."""
        if not self.ringing or self.online:
            return b""
        self.rings_sent += 1
        out = CR + b"RING" + CR
        if self.s0 and self.rings_sent >= self.s0:
            out += self._answer()
        return out

    def carrier_down(self):
        """The relay went away. NO CARRIER only if we ever said CONNECT."""
        self.wire_up = False
        self.ringing = False
        self.rings_sent = 0
        if not self.online:
            return b""
        self.online = False
        return CR + b"NO CARRIER" + CR

    # -- the manager side ------------------------------------------------

    def feed(self, data):
        """Manager bytes in -> (response bytes, payload bytes for the wire).

        Once online everything is payload. Before that, complete
        CR-terminated lines are AT commands and anything else is held.
        """
        if self.online:
            return b"", data

        self.buf += data
        out = b""
        while True:
            for sep in (b"\r", b"\n"):
                idx = self.buf.find(sep)
                if idx >= 0:
                    break
            else:
                break
            line, self.buf = self.buf[:idx], self.buf[idx + 1:]
            line = line.strip()
            if line:
                out += self.command(line)
            if self.online:
                # ATA landed mid-buffer; the rest is payload.
                rest, self.buf = self.buf, b""
                return out, rest
        return out, b""

    def command(self, line):
        """One AT command -> its bare response.

        Unknown commands answer OK on purpose. A modem's init string is
        whatever its driver felt like sending, and answering ERROR to a
        register set we do not model would fail an init that had nothing
        wrong with it. We are not pretending to be a modem for anyone but
        this one program.
        """
        try:
            text = line.decode("ascii", "replace").upper()
        except Exception:
            return CR + b"ERROR" + CR
        if not text.startswith("AT"):
            return CR + b"ERROR" + CR
        body = text[2:]

        if body.startswith("A"):                 # ATA -- answer
            if not self.wire_up:
                return CR + b"NO CARRIER" + CR
            return self._answer()
        if body.startswith("H"):                 # ATH -- hang up
            self.ringing = False
            was = self.online
            self.online = False
            return CR + (b"NO CARRIER" if was else b"OK") + CR
        if "S0=" in body:                        # auto-answer after n rings
            digits = ""
            for ch in body.split("S0=", 1)[1]:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            self.s0 = int(digits) if digits else 0
        return CR + b"OK" + CR

    def _answer(self):
        self.online = True
        self.ringing = False
        return CR + b"CONNECT" + CR


# -- endpoints -----------------------------------------------------------
#
# A small endpoint parser of its own, so the shim stands alone.

def parse_endpoint(spec):
    """`listen:HOST:PORT` or `connect:HOST:PORT` -> (mode, host, port)."""
    parts = (spec or "").split(":")
    if len(parts) != 3:
        raise ValueError("endpoint must be listen:HOST:PORT or"
                         " connect:HOST:PORT, not %r" % spec)
    mode, host, port = parts[0].lower(), parts[1], parts[2]
    if mode not in ("listen", "connect"):
        raise ValueError("endpoint mode must be listen or connect, not %r"
                         % mode)
    try:
        port = int(port)
    except ValueError:
        raise ValueError("endpoint port must be a number, not %r" % port)
    if not 1 <= port <= 65535:
        raise ValueError("endpoint port out of range: %d" % port)
    return mode, host, port


def open_pty(link=None):
    """A raw pty pair. Returns (master_fd, slave_path).

    The slave fd is kept open for the life of the process on purpose: with
    no process holding it, reads on the master fail EIO the moment
    mm_manager closes its end between sessions, and the shim would exit
    between call-ins rather than waiting for the next one.

    Raw, because mm_manager terminates AT commands with CR and a canonical
    line discipline would hold them until a newline that never comes.
    """
    import tty          # here, not at the top: AtModem is imported on Windows (KL5-NET Desktop)
    master_fd, slave_fd = os.openpty()
    tty.setraw(master_fd)
    tty.setraw(slave_fd)
    path = os.ttyname(slave_fd)
    if link:
        try:
            os.unlink(link)
        except OSError:
            pass
        os.symlink(path, link)
    return master_fd, slave_fd, (link or path)


def serve(args, log=print):
    """Run until interrupted. One wire connection at a time, by design:
    one terminal is talking to the manager at a time, and a second caller
    on the same port would interleave into the same pty."""
    mode, host, port = parse_endpoint(args.wire)
    master_fd, slave_fd, path = open_pty(args.link)
    log("pty ready: %s" % path)
    log("point the manager at it:  mm_manager -f %s -m -w" % path)
    log("  -w is mandatory: TIOCMGET is ENOTTY on a pty and the manager")
    log("  would bail 'Carrier lost' on the first packet without it.")

    listener = None
    if mode == "listen":
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(1)

    sel = selectors.DefaultSelector()
    modem = AtModem()

    def write_pty(data):
        if data:
            os.write(master_fd, data)

    while True:
        wire = None
        if listener is not None:
            # Logged every time round, not once at startup: after a session
            # ends, silence in the journal reads the same whether the shim
            # is waiting for the next call-in or has died.
            log("waiting for the relay on %s:%d" % (host, port))
            sel.register(listener, selectors.EVENT_READ)
            sel.register(master_fd, selectors.EVENT_READ)
            while wire is None:
                for key, _ in sel.select():
                    if key.fileobj is listener:
                        wire, addr = listener.accept()
                        log("relay connected from %s:%d" % addr)
                    else:
                        try:
                            data = os.read(master_fd, 4096)
                        except OSError:
                            data = b""
                        if data:
                            resp, _ = modem.feed(data)
                            write_pty(resp)
            sel.unregister(listener)
            sel.unregister(master_fd)
        else:
            wire = socket.create_connection((host, port), timeout=args.timeout)
            log("connected to the relay at %s:%d" % (host, port))

        wire.setblocking(False)
        write_pty(modem.carrier_up())
        sel.register(wire, selectors.EVENT_READ)
        sel.register(master_fd, selectors.EVENT_READ)
        sent = recv = 0
        last_ring = time.monotonic()
        try:
            while True:
                closed = False
                if modem.ringing and time.monotonic() - last_ring >= RING_SECONDS:
                    write_pty(modem.ring_again())
                    last_ring = time.monotonic()
                for key, _ in sel.select(timeout=1.0):
                    if key.fileobj is wire:
                        try:
                            data = wire.recv(4096)
                        except (BlockingIOError, InterruptedError):
                            continue
                        except OSError:
                            data = b""
                        if not data:
                            closed = True
                            break
                        recv += len(data)
                        write_pty(data)
                    else:
                        try:
                            data = os.read(master_fd, 4096)
                        except OSError as exc:
                            if exc.errno in (errno.EIO, errno.EAGAIN):
                                continue
                            raise
                        if not data:
                            continue
                        resp, payload = modem.feed(data)
                        write_pty(resp)
                        if payload:
                            sent += len(payload)
                            wire.sendall(payload)
                if closed:
                    break
        finally:
            sel.unregister(wire)
            sel.unregister(master_fd)
            wire.close()
            write_pty(modem.carrier_down())
            # Let the manager actually read NO CARRIER before anything
            # closes the pty. Without this the --once teardown discards it,
            # and a session that ended cleanly looks like one that vanished.
            try:
                import termios
                termios.tcdrain(master_fd)
            except OSError:
                pass
            log("session ended: %d bytes to the wire, %d from it" % (sent, recv))
        if args.once:
            # Closing the pty MASTER discards whatever is still queued for
            # the slave -- tcdrain puts NO CARRIER in the queue and the
            # close then throws the queue away. The long-running path never
            # closes, so this only affects --once, which is a debugging
            # convenience; a bounded grace is the honest fix rather than
            # pretending the close is synchronous.
            time.sleep(args.grace)
            break
    os.close(slave_fd)
    os.close(master_fd)


def build_parser():
    p = argparse.ArgumentParser(
        description="Hayes modem shim: a pty for mm_manager, a socket for"
                    " the relay.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wire", required=True, metavar="SPEC",
                   help="relay side: listen:HOST:PORT or connect:HOST:PORT")
    p.add_argument("--link", default="",
                   help="symlink the pty here so the manager's -f path is"
                        " stable across restarts (e.g. /run/kl5/mm-modem)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="connect timeout when --wire is connect:")
    p.add_argument("--once", action="store_true",
                   help="exit after one session instead of waiting for the next")
    p.add_argument("--grace", type=float, default=0.25,
                   help="with --once, seconds to let the manager read the"
                        " final NO CARRIER before the pty is closed"
                        " (closing the master discards it)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        serve(args)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
