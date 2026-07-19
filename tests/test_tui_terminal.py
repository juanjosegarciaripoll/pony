"""Unit tests for src/pony/tui/terminal.py."""

import io
import sys
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from textual.app import App, SuspendNotSupported


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
    def test_format_terminal_title_without_mail(self) -> None:
        from pony.tui.terminal import format_terminal_title

        self.assertEqual(format_terminal_title("Pony Express"), "Pony Express")

    def test_format_terminal_title_with_mail(self) -> None:
        from pony.tui.terminal import format_terminal_title

        self.assertEqual(
            format_terminal_title("Pony Express", has_inbox_mail=True),
            "✉ Pony Express",
        )

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


class TestSuspendForExternalProgram(unittest.TestCase):
    def test_suspends_and_resumes_supported_app(self) -> None:
        from pony.tui.terminal import suspend_for_external_program

        events: list[str] = []

        @contextmanager
        def suspend() -> Iterator[None]:
            events.append("suspend")
            yield
            events.append("resume")

        app = MagicMock(spec=App)
        app.suspend.return_value = suspend()
        with suspend_for_external_program(app):
            events.append("launch")

        self.assertEqual(events, ["suspend", "launch", "resume"])

    def test_continues_when_suspension_is_not_supported(self) -> None:
        from pony.tui.terminal import suspend_for_external_program

        app = MagicMock(spec=App)
        app.suspend.side_effect = SuspendNotSupported("unsupported")

        with suspend_for_external_program(app):
            launched = True

        self.assertTrue(launched)
