"""`pony calendar ...` — the calendar's commands under Pony's config."""

from __future__ import annotations

import argparse
import io
import unittest
from pathlib import Path
from unittest import mock

from conftest import TMP_ROOT, resolve_fixture

from pony.cli import build_parser, main, run_calendar
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


def _parse(argv: list[str]) -> argparse.Namespace:
    """What the program's parser makes of a calendar invocation."""
    return build_parser().parse_args(["calendar", *argv])


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
            code = run_calendar(
                paths=self.paths, args=_parse(["list"]), config_path=path
            )
        self.assertEqual(0, code)

    def test_an_unconfigured_calendar_says_how_to_configure_one(self) -> None:
        """A mail-only config is not an error; it is a calendar to add.

        Non-interactively it says so and names the file it would add
        the table to -- `[calendar]` in Pony's own configuration, which
        is the only place it can go now.
        """
        path = self._config(_MAIL_ONLY)
        err = io.StringIO()
        with (
            mock.patch("sys.stderr", err),
            mock.patch("sys.stdin.isatty", return_value=False),
        ):
            code = run_calendar(
                paths=self.paths, args=_parse(["list"]), config_path=path
            )
        self.assertEqual(2, code)
        self.assertIn("no calendar configured", err.getvalue())
        self.assertIn(str(path), err.getvalue())

    def test_the_programs_config_flag_is_honoured(self) -> None:
        """One parser, so `--config` is the program's, before the command.

        It used to be the calendar's own flag inside the tail Pony
        handed over, which is how the two halves could disagree about
        what `--config` meant.
        """
        path = self._config()
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            code = run_calendar(
                paths=self.paths, args=_parse(["list"]), config_path=path
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
                args=_parse(
                    [
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
                    ]
                ),
                config_path=path,
            )

        self.assertEqual(0, code)
        self.assertIn("[calendar] table", out.getvalue())
        text = path.read_text(encoding="utf-8")
        self.assertIn("[[calendar.accounts]]", text)
        # The mail half of the file, and the comment on it, are intact.
        self.assertIn("config_version = 3", text)
        self.assertIn("profile = ", text)
        # And the account is visible to the loader the program uses.
        from pony.calendar_runtime import load_calendar_config

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
                    paths=self.paths, args=_parse(["account", "list"]), config_path=path
                ),
            )
        listed = out.getvalue()

        err = io.StringIO()
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err):
            code = run_calendar(
                paths=self.paths,
                args=_parse(["account", "rm", "work"]),
                config_path=path,
            )

        self.assertIn("work", listed)
        self.assertEqual(0, code, err.getvalue())
        from pony.calendar_runtime import load_calendar_config

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
                paths=self.paths,
                args=_parse(["account", "rm", "work"]),
                config_path=path,
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
