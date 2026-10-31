"""The shared reporting vocabulary: what a failure is allowed to say."""

from __future__ import annotations

import unittest

from pony.outcome import describe_exception


class DescribeExceptionTests(unittest.TestCase):
    """A reason is never the empty string.

    Every front end builds its failure notice out of this, and the
    failures a sync hits in the field are precisely the ones whose
    ``str()`` is empty: a timeout, a bare ``OSError``, a TLS connection
    that went away.  Those read as "Sync failed: " and stopped.
    """

    def test_a_message_is_used_as_it_stands(self) -> None:
        self.assertEqual(
            describe_exception(RuntimeError("the server hung up")),
            "the server hung up",
        )

    def test_an_exception_with_no_message_names_its_class(self) -> None:
        for exc in (TimeoutError(), OSError(), RuntimeError(), ValueError("")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(describe_exception(exc), type(exc).__name__)

    def test_a_whitespace_only_message_names_its_class_too(self) -> None:
        self.assertEqual(describe_exception(RuntimeError("  \n ")), "RuntimeError")

    def test_an_errno_message_survives_whole(self) -> None:
        self.assertEqual(
            describe_exception(ConnectionResetError(104, "Connection reset by peer")),
            "[Errno 104] Connection reset by peer",
        )

    def test_a_keyerror_keeps_its_quoted_key(self) -> None:
        """``str(KeyError)`` is the repr of the key, which is the reason."""
        self.assertEqual(describe_exception(KeyError("INBOX")), "'INBOX'")
