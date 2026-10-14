"""`pony calendar ...` — the calendar's commands under Pony's config."""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest import mock

from conftest import TMP_ROOT, resolve_fixture

from pony.cli import _split_calendar_argv, main, run_calendar
from pony.paths import resolve

_CONFIG = """
config_version = 3
data_dir = "{DATA_DIR}"
profile = "test"
accounts = []

[calendar]

[[calendar.accounts]]
name = "work"
url = "https://cal.example.com/dav/"
username = "user@example.com"
credential = { backend = "env", variable = "CAL_PASSWORD" }
"""

_MAIL_ONLY = """
config_version = 3
data_dir = "{DATA_DIR}"
profile = "test"
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
        # Keep the calendar's files inside this test's tree. The host
        # resolves them from the paths it is running on, so pointing
        # `PONY_ROOT`-style `AppPaths` at this directory is enough:
        # `run_calendar` hands the calendar `AppPaths.calendar_dir`.
        self.paths = resolve(data_dir=self.root / "data")

    def _config(self, text: str = _CONFIG) -> Path:
        text = resolve_fixture(text)
        path = self.root / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_calendar_command_runs_against_the_unified_config(self) -> None:
        path = self._config()
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            code = run_calendar(paths=self.paths, config_path=path, argv=["list"])
        self.assertEqual(0, code)

    def test_an_unconfigured_calendar_is_reported(self) -> None:
        path = self._config(_MAIL_ONLY)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = run_calendar(paths=self.paths, config_path=path, argv=["list"])
        self.assertEqual(1, code)
        self.assertIn("no calendar configured", err.getvalue())

    def test_the_calendars_own_config_flag_is_honoured(self) -> None:
        path = self._config()
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            code = run_calendar(
                paths=self.paths, config_path=None, argv=["--config", str(path), "list"]
            )
        self.assertEqual(0, code)

    def test_account_add_writes_the_calendar_table_in_ponys_config(self) -> None:
        """It used to write a standalone file nothing here reads.

        The command reported success into `<data_dir>/calendar/
        config.toml` and `pony calendar list` then showed nothing.
        """
        path = self._config()
        before = path.read_text(encoding="utf-8")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = run_calendar(
                paths=self.paths,
                config_path=path,
                argv=[
                    "account",
                    "add",
                    "--name",
                    "home",
                    "--url",
                    "https://dav.example.com/home/",
                    "--username",
                    "me@example.com",
                    "--credential-backend",
                    "plaintext",
                    "--credential-value",
                    "secret",
                ],
            )

        self.assertEqual(0, code)
        self.assertIn("[calendar] table", out.getvalue())
        text = path.read_text(encoding="utf-8")
        self.assertIn("[[calendar.accounts]]", text)
        # The mail half of the file, and the comment on it, are intact.
        self.assertIn("config_version = 3", text)
        self.assertIn("profile = ", text)
        # And the account is visible to the loader the program uses.
        from pony.calendar import load_calendar_config

        config = load_calendar_config(path)
        assert config is not None
        self.assertIn("home", [account.name for account in config.accounts])
        self.assertNotEqual(before, text)

    def test_account_list_and_rm_see_the_same_table(self) -> None:
        path = self._config()
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(
                0,
                run_calendar(
                    paths=self.paths, config_path=path, argv=["account", "list"]
                ),
            )
        listed = out.getvalue()

        err = io.StringIO()
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err):
            code = run_calendar(
                paths=self.paths, config_path=path, argv=["account", "rm", "work"]
            )

        self.assertIn("work", listed)
        self.assertEqual(0, code, err.getvalue())
        from pony.calendar import load_calendar_config

        config = load_calendar_config(path)
        assert config is not None
        self.assertEqual([], [account.name for account in config.accounts])

    def test_a_comment_outside_the_table_survives_an_edit(self) -> None:
        path = self._config()
        path.write_text(
            "# keep me\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        with mock.patch("sys.stdout", io.StringIO()):
            run_calendar(
                paths=self.paths, config_path=path, argv=["account", "rm", "work"]
            )

        self.assertIn("# keep me", path.read_text(encoding="utf-8"))

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
