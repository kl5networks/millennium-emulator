#!/usr/bin/env python3
"""Tests for the Hayes shim. Stdlib only, no pty and no socket.

`AtModem` is a pure state machine for the same reason `box/agent.py` is:
the cases that matter here -- a relay that connects while a command is
half-written, a hangup mid-packet, a manager that inits twice -- are
tedious to arrange against real I/O and trivial against a function.

    python3 -m unittest test_atshim -v
"""
import unittest

import atshim
from atshim import AtModem


class ResponseVocabulary(unittest.TestCase):
    """mm_manager matches replies with strstr against a fixed array, in
    order, first match wins. Every response this module can emit has to be
    unambiguous under that rule or the manager reads the wrong outcome."""

    def test_no_token_contains_an_earlier_one(self):
        toks = atshim.RESPONSE_TOKENS
        for i, later in enumerate(toks):
            for earlier in toks[:i]:
                self.assertNotIn(
                    earlier, later,
                    "%r contains %r, which sorts earlier in mm_manager's "
                    "array and would win the strstr" % (later, earlier))

    def test_every_emitted_response_matches_exactly_one_token(self):
        m = AtModem()
        emitted = [m.command(b"ATZ"), m.command(b"ATBOGUS"), m.command(b"not at")]
        m.wire_up = True
        emitted.append(m.carrier_up())
        emitted.append(m.command(b"ATA"))
        emitted.append(m.carrier_down())
        for raw in emitted:
            text = raw.decode()
            hits = [t for t in atshim.RESPONSE_TOKENS if t in text]
            self.assertEqual(len(hits), 1,
                             "%r matched %r; must match exactly one" % (text, hits))

    def test_responses_carry_no_echo(self):
        """An echoed command could itself contain a token -- and a banner
        containing 'OK' would beat a real 'CONNECT' to the strstr."""
        m = AtModem()
        self.assertNotIn(b"ATZ", m.command(b"ATZ"))
        self.assertNotIn(b"AT", m.command(b"ATE0").replace(b"\r\n", b""))


class Commands(unittest.TestCase):
    def test_unknown_at_commands_answer_ok(self):
        m = AtModem()
        for cmd in (b"ATZ", b"AT&F", b"ATE0", b"ATX4", b"AT+FCLASS=0", b"ATQ0V1"):
            self.assertIn(b"OK", m.command(cmd), cmd)

    def test_non_at_is_an_error(self):
        m = AtModem()
        self.assertIn(b"ERROR", m.command(b"hello"))

    def test_ata_without_a_wire_is_no_carrier(self):
        m = AtModem()
        self.assertIn(b"NO CARRIER", m.command(b"ATA"))
        self.assertFalse(m.online)

    def test_ata_with_a_wire_connects(self):
        m = AtModem()
        m.carrier_up()
        self.assertIn(b"CONNECT", m.command(b"ATA"))
        self.assertTrue(m.online)

    def test_ath_hangs_up_only_when_online(self):
        m = AtModem()
        self.assertIn(b"OK", m.command(b"ATH"))
        m.carrier_up()
        m.command(b"ATA")
        self.assertIn(b"NO CARRIER", m.command(b"ATH"))
        self.assertFalse(m.online)

    def test_s0_is_parsed_from_the_register(self):
        m = AtModem()
        m.command(b"ATS0=2")
        self.assertEqual(m.s0, 2)
        m.command(b"ATS0=0")
        self.assertEqual(m.s0, 0)

    def test_s0_survives_trailing_junk(self):
        m = AtModem()
        m.command(b"ATS0=3&C1")
        self.assertEqual(m.s0, 3)


class Carrier(unittest.TestCase):
    def test_the_relay_connecting_is_the_ring(self):
        m = AtModem()
        self.assertIn(b"RING", m.carrier_up())
        self.assertTrue(m.ringing)

    def test_auto_answer_fires_on_the_sth_ring(self):
        m = AtModem()
        m.command(b"ATS0=2")
        self.assertNotIn(b"CONNECT", m.carrier_up())
        self.assertIn(b"CONNECT", m.ring_again())
        self.assertTrue(m.online)

    def test_s0_of_one_answers_the_very_first_ring(self):
        """The case that ships, and the case the S0=2 test above missed.

        mm_manager 0.8.3's default init string sets S0=1, and S0=1 means
        answer on the first ring. Waiting for ring_again() instead put a
        measured 3 s (RING_SECONDS) between the relay connecting and
        CONNECT, on a manager whose own S7 carrier wait is 3 s and with
        the Millennium already trained at the far end.
        """
        m = AtModem()
        m.command(b"ATE=1 S0=1 S7=3 &D2 +MS=B212")     # verbatim, v0.8.3
        self.assertEqual(m.s0, 1)
        out = m.carrier_up()
        self.assertIn(b"RING", out)
        self.assertIn(b"CONNECT", out)
        self.assertTrue(m.online)
        self.assertEqual(m.ring_again(), b"")          # nothing more to say

    def test_the_real_init_string_answers_ok_and_only_ok(self):
        """Every line mm_manager 0.8.3 actually sends, against the strstr
        vocabulary. It matters that none of these answer ERROR: the reply
        is searched for {OK, ERROR, RING, CONNECT, NO CARRIER, NULL} in
        that order, first match wins.
        """
        for line in (b"ATZ", b"ATE=1 S0=1 S7=3 &D2 +MS=B212"):
            m = AtModem()
            self.assertEqual(m.command(line), b"\r\nOK\r\n", line)

    def test_no_auto_answer_when_s0_is_zero(self):
        m = AtModem()
        m.carrier_up()
        for _ in range(5):
            self.assertNotIn(b"CONNECT", m.ring_again())
        self.assertFalse(m.online)

    def test_carrier_down_before_answer_is_silent(self):
        """Never sent CONNECT, so NO CARRIER would be a lie -- and would
        be read as a failed session rather than a caller who gave up."""
        m = AtModem()
        m.carrier_up()
        self.assertEqual(m.carrier_down(), b"")

    def test_carrier_down_after_answer_reports_it(self):
        m = AtModem()
        m.carrier_up()
        m.command(b"ATA")
        self.assertIn(b"NO CARRIER", m.carrier_down())
        self.assertFalse(m.online)

    def test_ring_is_not_repeated_once_online(self):
        m = AtModem()
        m.carrier_up()
        m.command(b"ATA")
        self.assertEqual(m.ring_again(), b"")


class Feed(unittest.TestCase):
    def test_commands_split_on_cr(self):
        m = AtModem()
        resp, payload = m.feed(b"ATZ\rATE0\r")
        self.assertEqual(resp.count(b"OK"), 2)
        self.assertEqual(payload, b"")

    def test_a_partial_command_is_held(self):
        m = AtModem()
        resp, payload = m.feed(b"AT")
        self.assertEqual(resp, b"")
        resp, payload = m.feed(b"Z\r")
        self.assertIn(b"OK", resp)

    def test_bytes_after_ata_are_payload_not_commands(self):
        """The manager can pipeline its first packet behind ATA. Treating
        it as another command would eat the packet and answer OK to it."""
        m = AtModem()
        m.carrier_up()
        resp, payload = m.feed(b"ATA\r\x01\x02\x03")
        self.assertIn(b"CONNECT", resp)
        self.assertEqual(payload, b"\x01\x02\x03")

    def test_everything_is_payload_once_online(self):
        m = AtModem()
        m.carrier_up()
        m.feed(b"ATA\r")
        resp, payload = m.feed(b"ATZ\r")
        self.assertEqual(resp, b"")
        self.assertEqual(payload, b"ATZ\r",
                         "online, so this is data that merely looks like AT")

    def test_payload_resumes_as_commands_after_hangup(self):
        m = AtModem()
        m.carrier_up()
        m.feed(b"ATA\r")
        m.carrier_down()
        resp, payload = m.feed(b"ATZ\r")
        self.assertIn(b"OK", resp)
        self.assertEqual(payload, b"")


class Endpoints(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(atshim.parse_endpoint("listen:127.0.0.1:2324"),
                         ("listen", "127.0.0.1", 2324))
        self.assertEqual(atshim.parse_endpoint("connect:192.0.2.1:2323"),
                         ("connect", "192.0.2.1", 2323))

    def test_rejects_nonsense(self):
        for bad in ("", "listen:127.0.0.1", "wibble:127.0.0.1:1",
                    "listen:127.0.0.1:nope", "listen:127.0.0.1:0",
                    "listen:127.0.0.1:99999"):
            with self.assertRaises(ValueError, msg=bad):
                atshim.parse_endpoint(bad)


if __name__ == "__main__":
    unittest.main()
