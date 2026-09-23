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


class TestResolveViewerCommand(unittest.TestCase):
    """Content-type lookup against the configured [viewers] table."""

    def test_returns_command_for_matching_content_type(self) -> None:
        from pony.domain import ViewerRule
        from pony.tui.terminal import resolve_viewer_command

        rules = (ViewerRule("text/calendar", ("chronos", "import")),)
        self.assertEqual(
            resolve_viewer_command(rules, "text/calendar"), ("chronos", "import")
        )

    def test_match_ignores_case_and_surrounding_space(self) -> None:
        from pony.domain import ViewerRule
        from pony.tui.terminal import resolve_viewer_command

        rules = (ViewerRule("text/calendar", ("chronos",)),)
        self.assertEqual(resolve_viewer_command(rules, " TEXT/Calendar "), ("chronos",))

    def test_returns_none_for_unlisted_type(self) -> None:
        from pony.domain import ViewerRule
        from pony.tui.terminal import resolve_viewer_command

        rules = (ViewerRule("text/calendar", ("chronos",)),)
        self.assertIsNone(resolve_viewer_command(rules, "text/plain"))

    def test_returns_none_without_a_content_type(self) -> None:
        from pony.domain import ViewerRule
        from pony.tui.terminal import resolve_viewer_command

        rules = (ViewerRule("text/calendar", ("chronos",)),)
        self.assertIsNone(resolve_viewer_command(rules, None))

    def test_returns_none_when_no_rules_are_configured(self) -> None:
        from pony.tui.terminal import resolve_viewer_command

        self.assertIsNone(resolve_viewer_command((), "text/calendar"))


class TestLaunchFile(unittest.TestCase):
    """launch_file dispatches to the configured viewer or the OS default."""

    def test_configured_command_receives_the_path_last(self) -> None:
        from pathlib import Path

        from pony.tui.terminal import launch_file

        with patch("pony.tui.terminal.subprocess.run") as run:
            launch_file(Path("/tmp/invite.ics"), ("chronos", "import"))

        run.assert_called_once_with(
            ["chronos", "import", "/tmp/invite.ics"], check=False
        )

    def test_empty_command_falls_back_to_the_os_default(self) -> None:
        from pathlib import Path

        from pony.tui.terminal import launch_file

        with (
            patch("pony.tui.terminal.sys.platform", "linux"),
            patch("pony.tui.terminal.subprocess.run") as run,
        ):
            launch_file(Path("/tmp/invite.ics"), ())

        run.assert_called_once_with(["xdg-open", "/tmp/invite.ics"], check=False)

    def test_no_command_uses_the_os_default(self) -> None:
        from pathlib import Path

        from pony.tui.terminal import launch_file

        with (
            patch("pony.tui.terminal.sys.platform", "linux"),
            patch("pony.tui.terminal.subprocess.run") as run,
        ):
            launch_file(Path("/tmp/report.pdf"))

        run.assert_called_once_with(["xdg-open", "/tmp/report.pdf"], check=False)
