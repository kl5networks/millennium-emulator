"""A craftsperson at an emulated Millennium: reads its panel, presses its
keys, swipes a card, drops coins -- in step with what the firmware shows.

A streamed worker run (`millennium_z180.run_diagnostic(..., far_end=...)`)
reports every panel screen as it settles. A fixed schedule of events
cannot follow a run whose middle is a real manager download of unknown
length, so this plays the person instead: each step waits for a screen
containing its text, then does its actions, each a delay (seconds, from
the step's start) and an event as `StreamLink.event` takes it:

    Step('Deposit a calibration coin', [(1.0, 'c', 1)])

Steps run in order, each matched only after the previous step's actions
are done, and never on the screen that started the previous step, so the
same text can start two steps in a row: the calibration step shows
"Deposit a calibration coin" twice, and an operator that matched the
second step on the first screen would drop its coin into the wrong one. The
line (`millennium_ncc_line.ManagerLine`) still plays the network: this
forwards it everything and adds the person on top.

Every screen is kept with the step that was waiting when it appeared, so
a test can say what the keypad step echoed for each key. Standard library
only; MIT, like the rest of this directory outside z180/.
"""

import time

import millennium_z180 as diagnostic


class Step(object):

    def __init__(self, screen, actions, name=None, timeout=None):
        self.screen = screen
        self.actions = list(actions)
        self.name = name or screen
        self.timeout = timeout          # seconds to wait for the screen, or None


class Operator(object):

    def __init__(self, steps, line=None, *, hold_after=5.0, log=None):
        self.steps = list(steps)
        self.line = line
        self.hold_after = hold_after
        self.log = log or (lambda text: None)
        self.index = 0                  # the step waiting for its screen
        self.pending = []               # (wall time, kind, value) still to do
        self.started = time.monotonic()
        self.waiting_since = self.started
        self.ms = 0
        self.screens = []               # (modelled ms, text, step name)
        self.done = []                  # (modelled ms, step name)
        self.failed = None              # the step that timed out, if one did
        self._finish_at = None
        self.finished = False
        self._used = -1                 # index of the screen the last step matched

    # -- what the worker reports ------------------------------------------
    def on_message(self, link, tag, payload):
        if self.line is not None:
            self.line.on_message(link, tag, payload)
        if tag == 'Z':
            self.ms = int.from_bytes(payload, 'little')
        elif tag == 'S':
            self.ms = max(self.ms, int.from_bytes(payload[:4], 'little'))
            text = diagnostic.panel_text(payload[4:])
            self._match(text)
            self.screens.append((self.ms, text, self.done[-1][1] if self.done else None))
            self.log('%7d ms  panel  %s' % (self.ms, text))

    def _match(self, text, at=None):
        if self.pending or self.index >= len(self.steps):
            return
        at = len(self.screens) if at is None else at
        step = self.steps[self.index]
        if step.screen in text and at != self._used:
            self._used = at
            now = time.monotonic()
            self.log('%7d ms  step   %s' % (self.ms, step.name))
            self.pending = [(now + delay, kind, value) for delay, kind, value in step.actions]
            self.pending.sort(key=lambda action: action[0])
            self.done.append((self.ms, step.name))
            self.index += 1
            self.waiting_since = now
            if not self.pending:
                self._next()

    def _next(self):
        if self.index >= len(self.steps) and self._finish_at is None:
            self._finish_at = time.monotonic() + self.hold_after
        elif self.screens and self.index < len(self.steps):
            # The screen the next step wants may already be showing, if it
            # is not the one the step just done was started by.
            self._match(self.screens[-1][1], len(self.screens) - 1)

    def selectables(self):
        return self.line.selectables() if self.line is not None else []

    def poll(self, link):
        if self.line is not None:
            self.line.poll(link)
        now = time.monotonic()
        while self.pending and self.pending[0][0] <= now:
            _, kind, value = self.pending.pop(0)
            self.log('%7d ms  do     %s %r' % (self.ms, kind, value))
            link.event(kind, value)
            if not self.pending:
                self.waiting_since = now
                self._next()
        if (self._finish_at is None and not self.pending and self.index < len(self.steps)
                and self.steps[self.index].timeout is not None
                and now - self.waiting_since > self.steps[self.index].timeout):
            self.failed = self.steps[self.index].name
            self.log('%7d ms  gave up waiting for %r' % (self.ms, self.steps[self.index].screen))
            self._finish_at = now
        if self._finish_at is not None and now >= self._finish_at and not self.finished:
            self.finished = True
            link.finish()

    # -- what happened ----------------------------------------------------
    def screens_during(self, name):
        """Screens from the moment the named step matched (its own screen
        included) to the moment the next one did."""
        return [text for _, text, step in self.screens if step == name]


def keys(text, start=0.0, step=0.5):
    """Actions that key `text` in, `step` seconds apart."""
    return [(start + step * i, 'k', key) for i, key in enumerate(text)]
