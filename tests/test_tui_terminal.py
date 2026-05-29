"""Unit tests for src/pony/tui/terminal.py."""

import io
import sys
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def _patched_stdout(value: object) -> Iterator[None]:
    with patch.object(sys, "__stdout__", value):
        yield


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeNonTTY(io.StringIO):
    def isatty(self) -> bool:
        return False


class TestSetTerminalTitle(unittest.TestCase):
    def test_emits_osc2_when_tty(self) -> None:
        from pony.tui.terminal import set_terminal_title

        buf = _FakeTTY()
        with _patched_stdout(buf):
            set_terminal_title("hi")
        self.assertEqual(buf.getvalue(), "\x1b]2;hi\x07")

    def test_no_write_when_not_tty(self) -> None:
        from pony.tui.terminal import set_terminal_title

        buf = _FakeNonTTY()
        with _patched_stdout(buf):
            set_terminal_title("hi")
        self.assertEqual(buf.getvalue(), "")

    def test_no_write_when_stdout_is_none(self) -> None:
        from pony.tui.terminal import set_terminal_title

        with _patched_stdout(None):
            set_terminal_title("hi")  # must not raise


class TestPushPopTerminalTitle(unittest.TestCase):
    def test_push_emits_save_sequence(self) -> None:
        from pony.tui.terminal import push_terminal_title

        buf = _FakeTTY()
        with _patched_stdout(buf):
            push_terminal_title()
        self.assertEqual(buf.getvalue(), "\x1b[22;2t")

    def test_pop_emits_restore_sequence(self) -> None:
        from pony.tui.terminal import pop_terminal_title

        buf = _FakeTTY()
        with _patched_stdout(buf):
            pop_terminal_title()
        self.assertEqual(buf.getvalue(), "\x1b[23;2t")

    def test_push_no_write_when_not_tty(self) -> None:
        from pony.tui.terminal import push_terminal_title

        buf = _FakeNonTTY()
        with _patched_stdout(buf):
            push_terminal_title()
        self.assertEqual(buf.getvalue(), "")

    def test_pop_no_write_when_not_tty(self) -> None:
        from pony.tui.terminal import pop_terminal_title

        buf = _FakeNonTTY()
        with _patched_stdout(buf):
            pop_terminal_title()
        self.assertEqual(buf.getvalue(), "")
