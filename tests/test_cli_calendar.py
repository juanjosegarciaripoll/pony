"""`pony calendar ...` — the calendar's commands under Pony's config."""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest import mock

from conftest import TMP_ROOT

from pony.cli import _split_calendar_argv, main, run_calendar

_CONFIG = """
config_version = 2
accounts = []

[calendar]

[[calendar.accounts]]
name = "work"
url = "https://cal.example.com/dav/"
username = "user@example.com"
credential = { backend = "env", variable = "CAL_PASSWORD" }
"""

_MAIL_ONLY = """
config_version = 2
accounts = []
"""


class SplitCalendarArgvTest(unittest.TestCase):
    """The pass-through split that keeps argparse out of the way."""

    def test_no_calendar_command_leaves_the_list_alone(self) -> None:
        self.assertEqual(
            (["sync", "--yes"], None), _split_calendar_argv(["sync", "--yes"])
        )

    def test_everything_after_calendar_is_forwarded(self) -> None:
        head, tail = _split_calendar_argv(["calendar", "list", "--days", "7"])
        self.assertEqual(["calendar"], head)
        self.assertEqual(["list", "--days", "7"], tail)

    def test_a_flag_after_calendar_is_forwarded_too(self) -> None:
        # argparse.REMAINDER would reject this; the split is what lets
        # `pony calendar --help` reach the calendar's own parser.
        _head, tail = _split_calendar_argv(["calendar", "--help"])
        self.assertEqual(["--help"], tail)

    def test_pony_options_before_the_command_stay_with_pony(self) -> None:
        head, tail = _split_calendar_argv(["--config", "c.toml", "calendar", "sync"])
        self.assertEqual(["--config", "c.toml", "calendar"], head)
        self.assertEqual(["sync"], tail)

    def test_a_theme_named_calendar_is_not_the_command(self) -> None:
        head, tail = _split_calendar_argv(["--theme", "calendar", "tui"])
        self.assertEqual(["--theme", "calendar", "tui"], head)
        self.assertIsNone(tail)

    def test_an_option_value_spelled_with_equals_is_stepped_over(self) -> None:
        head, tail = _split_calendar_argv(["--config=c.toml", "calendar", "sync"])
        self.assertEqual(["--config=c.toml", "calendar"], head)
        self.assertEqual(["sync"], tail)

    def test_the_word_calendar_inside_a_subcommand_is_left_alone(self) -> None:
        # Only the subcommand position counts. Cutting at any later
        # "calendar" would swallow the rest of that command's arguments
        # — this one used to lose the recipient entirely.
        argv = ["compose", "--subject", "calendar", "--to", "her@example.com"]
        head, tail = _split_calendar_argv(argv)
        self.assertEqual(argv, head)
        self.assertIsNone(tail)

    def test_a_search_for_the_word_calendar_is_left_alone(self) -> None:
        head, tail = _split_calendar_argv(["search", "calendar"])
        self.assertEqual(["search", "calendar"], head)
        self.assertIsNone(tail)

    def test_an_empty_argument_list_is_not_a_calendar_invocation(self) -> None:
        self.assertEqual(([], None), _split_calendar_argv([]))

    def test_an_empty_tail_is_still_a_calendar_invocation(self) -> None:
        head, tail = _split_calendar_argv(["calendar"])
        self.assertEqual(["calendar"], head)
        self.assertEqual([], tail)


class RunCalendarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMP_ROOT / "cli-calendar" / self.id().rsplit(".", 1)[-1]
        self.root.mkdir(parents=True, exist_ok=True)
        # The calendar resolves its mirror, index and tokens from the
        # platform data dir; keep them inside the test's own tree.
        patcher = mock.patch.dict(
            "os.environ", {"XDG_DATA_HOME": str(self.root / "data")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config(self, text: str = _CONFIG) -> Path:
        path = self.root / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_calendar_command_runs_against_the_unified_config(self) -> None:
        path = self._config()
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            code = run_calendar(config_path=path, argv=["list"])
        self.assertEqual(0, code)

    def test_an_unconfigured_calendar_is_reported(self) -> None:
        path = self._config(_MAIL_ONLY)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = run_calendar(config_path=path, argv=["list"])
        self.assertEqual(1, code)
        self.assertIn("no calendar configured", err.getvalue())

    def test_the_calendars_own_config_flag_is_honoured(self) -> None:
        path = self._config()
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            code = run_calendar(config_path=None, argv=["--config", str(path), "list"])
        self.assertEqual(0, code)

    def test_main_routes_the_calendar_command(self) -> None:
        path = self._config()
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            code = main(["--config", str(path), "calendar", "list"])
        self.assertEqual(0, code)

    def test_help_reaches_the_calendars_parser(self) -> None:
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured), self.assertRaises(SystemExit) as exit_:
            main(["calendar", "--help"])
        self.assertEqual(0, exit_.exception.code)
        printed = captured.getvalue()
        self.assertIn("pony calendar", printed)
        self.assertIn("sync", printed)


if __name__ == "__main__":
    unittest.main()
