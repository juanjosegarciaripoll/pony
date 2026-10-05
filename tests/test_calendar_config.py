"""The calendar's slice of Pony's single configuration file."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pony.calendar import (
    load_calendar_config,
    open_calendar_runtime,
    parse_calendar_config,
)
from pony.config import ConfigError

_MAIL_ONLY: dict[str, object] = {
    "config_version": 2,
    "accounts": [],
}


def _with_calendar(**section: object) -> dict[str, object]:
    return {**_MAIL_ONLY, "calendar": section}


_ACCOUNT: dict[str, object] = {
    "name": "personal",
    "url": "https://caldav.example.com/dav/",
    "username": "user@example.com",
    "credential": {"backend": "env", "variable": "CAL_PASSWORD"},
}


class ParseCalendarConfigTest(unittest.TestCase):
    def test_absent_section_means_no_calendar(self) -> None:
        self.assertIsNone(parse_calendar_config(_MAIL_ONLY))

    def test_empty_section_enables_a_calendar_without_accounts(self) -> None:
        config = parse_calendar_config(_with_calendar())
        assert config is not None
        self.assertEqual((), config.accounts)

    def test_accounts_are_parsed(self) -> None:
        config = parse_calendar_config(_with_calendar(accounts=[_ACCOUNT]))
        assert config is not None
        self.assertEqual(1, len(config.accounts))
        account = config.accounts[0]
        self.assertEqual("personal", account.name)
        self.assertEqual("https://caldav.example.com/dav/", account.url)
        self.assertEqual("user@example.com", account.username)

    def test_calendar_defaults_are_the_calendars_own(self) -> None:
        config = parse_calendar_config(_with_calendar())
        assert config is not None
        self.assertTrue(config.background_sync_enabled)
        self.assertEqual(3600, config.background_sync_interval_seconds)

    def test_shared_keys_are_inherited_from_the_top_level(self) -> None:
        raw: dict[str, object] = {
            **_MAIL_ONLY,
            "use_utf8": True,
            "editor": "nvim",
            "theme": "flexoki",
            "calendar": {},
        }
        config = parse_calendar_config(raw)
        assert config is not None
        self.assertTrue(config.use_utf8)
        self.assertEqual("nvim", config.editor)
        self.assertEqual("flexoki", config.theme)

    def test_the_section_overrides_an_inherited_key(self) -> None:
        raw: dict[str, object] = {
            **_MAIL_ONLY,
            "use_utf8": True,
            "theme": "flexoki",
            "calendar": {"use_utf8": False, "theme": "nord"},
        }
        config = parse_calendar_config(raw)
        assert config is not None
        self.assertFalse(config.use_utf8)
        self.assertEqual("nord", config.theme)

    def test_mail_keys_do_not_leak_into_the_calendar(self) -> None:
        # The mail accounts are a different shape entirely; inheriting
        # them would make the calendar reject the whole file.
        raw: dict[str, object] = {
            "config_version": 2,
            "accounts": [{"name": "work", "account_type": "local"}],
            "markdown_compose": True,
            "calendar": {"accounts": [_ACCOUNT]},
        }
        config = parse_calendar_config(raw)
        assert config is not None
        self.assertEqual(("personal",), tuple(a.name for a in config.accounts))

    def test_a_second_config_version_is_rejected(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_calendar_config(_with_calendar(config_version=1))
        self.assertIn("one 'config_version'", str(caught.exception))

    def test_section_must_be_a_table(self) -> None:
        with self.assertRaises(ConfigError):
            parse_calendar_config({**_MAIL_ONLY, "calendar": "yes"})

    def test_top_level_must_be_a_mapping(self) -> None:
        with self.assertRaises(ConfigError):
            parse_calendar_config(["not", "a", "table"])

    def test_a_malformed_account_names_the_calendar_section(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_calendar_config(_with_calendar(accounts=[{"name": "nope"}]))
        message = str(caught.exception)
        self.assertIn("[calendar]", message)
        self.assertIn("accounts[0][nope]", message)

    def test_a_bad_interval_is_reported(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_calendar_config(_with_calendar(background_sync_interval_seconds=0))
        self.assertIn("[calendar]", str(caught.exception))


class LoadCalendarConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, text: str) -> Path:
        path = self.root / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_reads_the_calendar_table_from_the_file(self) -> None:
        path = self._write(
            """
            config_version = 2
            use_utf8 = true
            accounts = []

            [calendar]
            background_sync_interval_seconds = 900

            [[calendar.accounts]]
            name = "work"
            url = "https://cal.example.com/dav/"
            username = "user@example.com"
            credential = { backend = "env", variable = "CAL_PASSWORD" }
            """
        )
        config = load_calendar_config(path)
        assert config is not None
        self.assertEqual(900, config.background_sync_interval_seconds)
        self.assertTrue(config.use_utf8)
        self.assertEqual(("work",), tuple(a.name for a in config.accounts))

    def test_mail_only_file_has_no_calendar(self) -> None:
        path = self._write("config_version = 2\naccounts = []\n")
        self.assertIsNone(load_calendar_config(path))

    def test_missing_file_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_calendar_config(self.root / "absent.toml")


class OpenCalendarRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # chronos.paths reads the environment at call time, so pointing
        # XDG_DATA_HOME at the temp dir keeps the opened index and
        # mirror out of the developer's real calendar data.
        import os

        previous = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.root / "data")

        def _restore() -> None:
            if previous is None:
                del os.environ["XDG_DATA_HOME"]
            else:
                os.environ["XDG_DATA_HOME"] = previous

        self.addCleanup(_restore)

    def _write(self, text: str) -> Path:
        path = self.root / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_unconfigured_calendar_opens_nothing(self) -> None:
        path = self._write("config_version = 2\naccounts = []\n")
        self.assertIsNone(open_calendar_runtime(path))

    def test_opens_the_mirror_and_index(self) -> None:
        path = self._write(
            """
            config_version = 2
            accounts = []

            [calendar]

            [[calendar.accounts]]
            name = "work"
            url = "https://cal.example.com/dav/"
            username = "user@example.com"
            credential = { backend = "env", variable = "CAL_PASSWORD" }
            """
        )
        runtime = open_calendar_runtime(path)
        assert runtime is not None
        self.addCleanup(runtime.close)
        self.assertEqual(("work",), tuple(a.name for a in runtime.config.accounts))
        # The index creates its schema eagerly, so the file exists and
        # answers queries as soon as the runtime is open.
        self.assertEqual([], list(runtime.index.list_calendars()))
        self.assertTrue(runtime.mirror.root.name == "mirror")


if __name__ == "__main__":
    unittest.main()
