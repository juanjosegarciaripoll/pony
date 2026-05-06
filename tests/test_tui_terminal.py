"""Unit tests for src/pony/tui/terminal.py."""

import io
import sys
import unittest


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
        orig = sys.__stdout__
        try:
            sys.__stdout__ = buf  # type: ignore[assignment]
            set_terminal_title("hi")
        finally:
            sys.__stdout__ = orig
        self.assertEqual(buf.getvalue(), "\x1b]2;hi\x07")

    def test_no_write_when_not_tty(self) -> None:
        from pony.tui.terminal import set_terminal_title

        buf = _FakeNonTTY()
        orig = sys.__stdout__
        try:
            sys.__stdout__ = buf  # type: ignore[assignment]
            set_terminal_title("hi")
        finally:
            sys.__stdout__ = orig
        self.assertEqual(buf.getvalue(), "")

    def test_no_write_when_stdout_is_none(self) -> None:
        from pony.tui.terminal import set_terminal_title

        orig = sys.__stdout__
        try:
            sys.__stdout__ = None  # type: ignore[assignment]
            set_terminal_title("hi")  # must not raise
        finally:
            sys.__stdout__ = orig


class TestPushPopTerminalTitle(unittest.TestCase):
    def test_push_emits_save_sequence(self) -> None:
        from pony.tui.terminal import push_terminal_title

        buf = _FakeTTY()
        orig = sys.__stdout__
        try:
            sys.__stdout__ = buf  # type: ignore[assignment]
            push_terminal_title()
        finally:
            sys.__stdout__ = orig
        self.assertEqual(buf.getvalue(), "\x1b[22;2t")

    def test_pop_emits_restore_sequence(self) -> None:
        from pony.tui.terminal import pop_terminal_title

        buf = _FakeTTY()
        orig = sys.__stdout__
        try:
            sys.__stdout__ = buf  # type: ignore[assignment]
            pop_terminal_title()
        finally:
            sys.__stdout__ = orig
        self.assertEqual(buf.getvalue(), "\x1b[23;2t")

    def test_push_no_write_when_not_tty(self) -> None:
        from pony.tui.terminal import push_terminal_title

        buf = _FakeNonTTY()
        orig = sys.__stdout__
        try:
            sys.__stdout__ = buf  # type: ignore[assignment]
            push_terminal_title()
        finally:
            sys.__stdout__ = orig
        self.assertEqual(buf.getvalue(), "")

    def test_pop_no_write_when_not_tty(self) -> None:
        from pony.tui.terminal import pop_terminal_title

        buf = _FakeNonTTY()
        orig = sys.__stdout__
        try:
            sys.__stdout__ = buf  # type: ignore[assignment]
            pop_terminal_title()
        finally:
            sys.__stdout__ = orig
        self.assertEqual(buf.getvalue(), "")
