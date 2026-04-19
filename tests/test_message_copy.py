"""Tests for ``pony.message_copy.copy_message_bytes``."""

from __future__ import annotations

import unittest

from pony.message_copy import copy_message_bytes


def _sample(message_id: bytes = b"<orig@example.com>", *, crlf: bool = False) -> bytes:
    eol = b"\r\n" if crlf else b"\n"
    parts = [
        b"From: alice@example.com",
        b"To: bob@example.com",
        b"Subject: hello",
        b"Message-ID: " + message_id,
        b"Date: Sun, 19 Apr 2026 10:00:00 +0000",
        b"",
        b"body line",
        b"",
    ]
    return eol.join(parts)


class CopyMessageBytesTest(unittest.TestCase):
    """The same-account path must produce a fresh, valid Message-ID."""

    def test_rewrite_replaces_message_id_and_returns_it(self) -> None:
        raw = _sample()
        new_raw, new_mid = copy_message_bytes(raw, rewrite_message_id=True)
        self.assertIn(new_mid.encode("ascii"), new_raw)
        self.assertNotIn(b"<orig@example.com>", new_raw)
        self.assertTrue(new_mid.startswith("<pony-copy-"))
        self.assertTrue(new_mid.endswith("@pony.local>"))

    def test_rewrite_preserves_all_other_headers_and_body(self) -> None:
        raw = _sample()
        new_raw, _ = copy_message_bytes(raw, rewrite_message_id=True)
        # Everything except the Message-ID line must survive verbatim.
        for untouched in (
            b"From: alice@example.com",
            b"To: bob@example.com",
            b"Subject: hello",
            b"Date: Sun, 19 Apr 2026 10:00:00 +0000",
            b"body line",
        ):
            self.assertIn(untouched, new_raw)

    def test_rewrite_produces_unique_ids_across_calls(self) -> None:
        raw = _sample()
        _, mid_a = copy_message_bytes(raw, rewrite_message_id=True)
        _, mid_b = copy_message_bytes(raw, rewrite_message_id=True)
        self.assertNotEqual(mid_a, mid_b)

    def test_preserve_path_returns_bytes_unchanged_and_extracts_mid(self) -> None:
        raw = _sample(b"<xyz@example.com>")
        new_raw, mid = copy_message_bytes(raw, rewrite_message_id=False)
        self.assertIs(new_raw, raw)  # same object — no copy performed
        self.assertEqual(mid, "<xyz@example.com>")

    def test_preserve_path_falls_back_to_rewrite_when_source_has_no_mid(self) -> None:
        raw = b"From: alice@example.com\nSubject: none\n\nbody\n"
        new_raw, mid = copy_message_bytes(raw, rewrite_message_id=False)
        # No MID in source → synthesised.
        self.assertTrue(mid.startswith("<pony-copy-"))
        self.assertIn(mid.encode("ascii"), new_raw)
        self.assertIn(b"From: alice@example.com", new_raw)

    def test_crlf_line_endings_preserved(self) -> None:
        raw = _sample(crlf=True)
        new_raw, new_mid = copy_message_bytes(raw, rewrite_message_id=True)
        # Look at the rewritten Message-ID line specifically.
        line = b"Message-ID: " + new_mid.encode("ascii") + b"\r\n"
        self.assertIn(line, new_raw)
        # And the surrounding headers still use \r\n.
        self.assertIn(b"From: alice@example.com\r\n", new_raw)

    def test_lf_line_endings_preserved(self) -> None:
        raw = _sample(crlf=False)
        new_raw, new_mid = copy_message_bytes(raw, rewrite_message_id=True)
        line = b"Message-ID: " + new_mid.encode("ascii") + b"\n"
        self.assertIn(line, new_raw)
        # No CRLF introduced.
        self.assertNotIn(b"\r\n", new_raw)

    def test_preserve_path_handles_continuation_line_in_message_id(self) -> None:
        """Message-ID headers legally fold onto continuation lines starting
        with whitespace.  We must extract the full id regardless."""
        raw = (
            b"From: alice@example.com\n"
            b"Message-ID: <first-half\n"
            b"  -second-half@example.com>\n"
            b"\n"
            b"body\n"
        )
        _, mid = copy_message_bytes(raw, rewrite_message_id=False)
        self.assertIn("first-half", mid)
        self.assertIn("second-half", mid)

    def test_rewrite_strips_folded_message_id_completely(self) -> None:
        raw = (
            b"From: alice@example.com\n"
            b"Message-ID: <first-half\n"
            b"  -second-half@example.com>\n"
            b"\n"
            b"body\n"
        )
        new_raw, _ = copy_message_bytes(raw, rewrite_message_id=True)
        # Neither half of the original folded id may survive.
        self.assertNotIn(b"first-half", new_raw)
        self.assertNotIn(b"second-half", new_raw)
