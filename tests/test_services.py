"""Tests for pony.services — build_service_status and mirror integrity checks."""

from __future__ import annotations

import sys
import unittest

from conftest import TMP_ROOT, isolated_data_dir, resolve_fixture

from pony.domain import AccountConfig, AppConfig, MirrorConfig, SmtpConfig
from pony.paths import AppPaths
from pony.services import (
    CheckStatus,
    _maildir_base_key,
    _scan_maildir,
    _scan_mbox_stale,
    build_service_status,
    check_mirror_integrity,
)


def _tmp_paths(label: str) -> AppPaths:
    root = TMP_ROOT / f"svc-{label}"
    root.mkdir(parents=True, exist_ok=True)
    return AppPaths(
        config_file=root / "config.toml",
        data_dir=root / "data",
    )


def _make_account(paths: AppPaths, name: str = "personal") -> AccountConfig:
    mirror_dir = paths.data_dir / "mirrors" / name
    mirror_dir.mkdir(parents=True, exist_ok=True)
    return AccountConfig(
        name=name,
        email_address=f"{name}@example.com",
        imap_host="imap.example.com",
        smtp=SmtpConfig(host="smtp.example.com"),
        username=name,
        credentials_source="plaintext",
        mirror=MirrorConfig(path=mirror_dir, format="maildir"),
        password="secret",
    )


class BuildServiceStatusTest(unittest.TestCase):
    def test_no_config_file_gives_error_check(self) -> None:
        paths = _tmp_paths("no-config")
        status = build_service_status(paths=paths, config_path=None, config=None)
        config_check = next(c for c in status.checks if c.name == "Config file")
        self.assertEqual(config_check.status, CheckStatus.ERROR)
        self.assertIn("Not found", config_check.detail)

    def test_config_none_parse_error_gives_error_check(self) -> None:
        paths = _tmp_paths("parse-error")
        # Create a config file so the "not found" branch is skipped, but
        # pass config=None to simulate a parse failure.
        paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text("[invalid toml", encoding="utf-8")
        status = build_service_status(
            paths=paths, config_path=paths.config_file, config=None
        )
        config_check = next(c for c in status.checks if c.name == "Config file")
        self.assertEqual(config_check.status, CheckStatus.ERROR)
        self.assertIn("parse", config_check.detail.lower())

    def test_valid_config_shows_ok_check(self) -> None:
        paths = _tmp_paths("valid-config")
        account = _make_account(paths)
        config = AppConfig(accounts=(account,))
        paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text("# dummy", encoding="utf-8")
        status = build_service_status(
            paths=paths, config_path=paths.config_file, config=config
        )
        config_check = next(c for c in status.checks if c.name == "Config file")
        self.assertEqual(config_check.status, CheckStatus.OK)
        self.assertIn("personal", config_check.detail)

    def test_index_db_not_created_gives_warn_check(self) -> None:
        paths = _tmp_paths("no-index")
        account = _make_account(paths)
        config = AppConfig(accounts=(account,))
        paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text("# dummy", encoding="utf-8")
        status = build_service_status(
            paths=paths, config_path=paths.config_file, config=config
        )
        index_check = next(c for c in status.checks if c.name == "Index database")
        self.assertEqual(index_check.status, CheckStatus.WARN)

    def test_existing_index_db_gives_ok_check(self) -> None:
        import sqlite3

        paths = _tmp_paths("with-index")
        account = _make_account(paths)
        config = AppConfig(accounts=(account,))
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(paths.index_db_file))
        conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text("# dummy", encoding="utf-8")
        status = build_service_status(
            paths=paths, config_path=paths.config_file, config=config
        )
        index_check = next(c for c in status.checks if c.name == "Index database")
        self.assertEqual(index_check.status, CheckStatus.OK)
        self.assertIn("table(s)", index_check.detail)

    def test_mirror_path_not_exists_gives_warn(self) -> None:
        paths = _tmp_paths("no-mirror")
        account = _make_account(paths)
        # Remove the mirror directory so it doesn't exist.
        import shutil

        shutil.rmtree(account.mirror.path)
        config = AppConfig(accounts=(account,))
        paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text("# dummy", encoding="utf-8")
        status = build_service_status(paths=paths, config_path=None, config=config)
        mirror_checks = [c for c in status.checks if c.name.startswith('Mirror "')]
        self.assertTrue(mirror_checks)
        self.assertEqual(mirror_checks[0].status, CheckStatus.WARN)
        self.assertIn("Does not exist", mirror_checks[0].detail)

    def test_mirror_path_is_file_not_dir_gives_error(self) -> None:
        paths = _tmp_paths("mirror-file")
        account = _make_account(paths)
        # Replace the mirror directory with a regular file.
        import shutil

        shutil.rmtree(account.mirror.path)
        account.mirror.path.write_text("not a dir")
        config = AppConfig(accounts=(account,))
        paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text("# dummy", encoding="utf-8")
        status = build_service_status(paths=paths, config_path=None, config=config)
        mirror_checks = [c for c in status.checks if c.name.startswith('Mirror "')]
        self.assertTrue(mirror_checks)
        self.assertEqual(mirror_checks[0].status, CheckStatus.ERROR)
        self.assertIn("Not a directory", mirror_checks[0].detail)

    def test_no_config_returns_status_with_checks(self) -> None:
        paths = _tmp_paths("no-config-2")
        status = build_service_status(paths=paths, config_path=None, config=None)
        self.assertIsNotNone(status)
        self.assertGreater(len(status.checks), 0)

    @unittest.skipIf(
        sys.version_info >= (3, 13),
        "Only runs on Python < 3.13 to test the WARN branch",
    )
    def test_old_python_gives_warn(self) -> None:  # pragma: no cover
        paths = _tmp_paths("old-python")
        status = build_service_status(paths=paths, config_path=None, config=None)
        py_check = next(c for c in status.checks if c.name == "Python version")
        self.assertEqual(py_check.status, CheckStatus.WARN)


class MaildirBaseKeyTest(unittest.TestCase):
    def test_cur_suffix_stripped(self) -> None:
        self.assertEqual(_maildir_base_key("abc123!2,S"), "abc123")

    def test_colon_cur_suffix_stripped(self) -> None:
        self.assertEqual(_maildir_base_key("abc123:2,RS"), "abc123")

    def test_no_suffix_unchanged(self) -> None:
        self.assertEqual(_maildir_base_key("abc123"), "abc123")


class ScanMaildirTest(unittest.TestCase):
    def setUp(self) -> None:
        from uuid import uuid4

        self.root = TMP_ROOT / f"scan-maildir-{uuid4().hex}"
        inbox = self.root
        (inbox / "cur").mkdir(parents=True, exist_ok=True)
        (inbox / "new").mkdir(parents=True, exist_ok=True)

    def test_orphan_on_disk_detected(self) -> None:
        (self.root / "cur" / "orphan-key").write_bytes(b"data")
        orphans, stale = _scan_maildir(self.root, "acc", {"INBOX": set()})
        self.assertTrue(any("orphan-key" in str(p) for p in orphans))
        self.assertEqual(stale, [])

    def test_stale_index_row_detected(self) -> None:
        orphans, stale = _scan_maildir(self.root, "acc", {"INBOX": {"missing-key"}})
        self.assertEqual(orphans, [])
        self.assertIn("acc/INBOX/missing-key", stale)

    def test_matching_key_clean(self) -> None:
        key = "match-key"
        (self.root / "cur" / key).write_bytes(b"data")
        orphans, stale = _scan_maildir(self.root, "acc", {"INBOX": {key}})
        self.assertEqual(orphans, [])
        self.assertEqual(stale, [])


class ScanMboxStaleTest(unittest.TestCase):
    def test_missing_mbox_marks_all_keys_stale(self) -> None:
        root = TMP_ROOT / "scan-mbox"
        root.mkdir(parents=True, exist_ok=True)
        stale = _scan_mbox_stale(root, "acc", {"INBOX": {"key1", "key2"}})
        self.assertEqual(len(stale), 2)
        self.assertIn("acc/INBOX/key1", stale)

    def test_existing_mbox_file_no_stale(self) -> None:
        root = TMP_ROOT / "scan-mbox-ok"
        root.mkdir(parents=True, exist_ok=True)
        (root / "INBOX.mbox").write_text("From ...", encoding="utf-8")
        stale = _scan_mbox_stale(root, "acc", {"INBOX": {"key1"}})
        self.assertEqual(stale, [])


class CheckMirrorIntegrityTest(unittest.TestCase):
    def test_mirror_not_created_returns_ok(self) -> None:
        paths = _tmp_paths("mi-not-created")
        account = _make_account(paths)
        import shutil

        shutil.rmtree(account.mirror.path)
        from pony.index_store import SqliteIndexRepository

        paths.data_dir.mkdir(parents=True, exist_ok=True)
        index = SqliteIndexRepository(database_path=paths.index_db_file)
        index.initialize()
        check = check_mirror_integrity(account=account, index=index)
        self.assertEqual(check.status, CheckStatus.OK)
        self.assertIn("not yet created", check.detail)

    def test_no_synced_folders_returns_ok(self) -> None:
        paths = _tmp_paths("mi-no-sync")
        account = _make_account(paths)
        from pony.index_store import SqliteIndexRepository

        paths.data_dir.mkdir(parents=True, exist_ok=True)
        index = SqliteIndexRepository(database_path=paths.index_db_file)
        index.initialize()
        check = check_mirror_integrity(account=account, index=index)
        self.assertEqual(check.status, CheckStatus.OK)
        self.assertIn("no indexed messages", check.detail)


class CalendarDoctorChecksTest(unittest.TestCase):
    """`pony doctor` answers for the calendar half too."""

    def setUp(self) -> None:
        from chronos.paths import activate as activate_calendar
        from chronos.paths import active as active_calendar

        self.paths = _tmp_paths(f"cal-{self.id().rsplit('.', 1)[-1]}")
        self.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        # The calendar's files live under the data directory the doctor
        # was handed, so there is nothing to redirect.
        self.calendar_home = self.paths.calendar_dir
        self.calendar_home.mkdir(parents=True, exist_ok=True)
        # `pony` activates the calendar's root before reading anything,
        # and an account's `mirror_path` default comes from it, so a
        # doctor reading this config has to see this root.
        previous = active_calendar().root
        activate_calendar(self.calendar_home)
        self.addCleanup(activate_calendar, previous)

    def _write_config(self, text: str) -> None:
        self.paths.config_file.write_text(resolve_fixture(text), encoding="utf-8")

    def _checks(self) -> dict[str, tuple[CheckStatus, str]]:
        status = build_service_status(
            paths=self.paths,
            config_path=self.paths.config_file,
            config=None,
        )
        return {c.name: (c.status, c.detail) for c in status.checks}

    def test_a_mail_only_config_reports_no_calendar(self) -> None:
        self._write_config(
            "config_version = 3\n"
            f'data_dir = "{isolated_data_dir()}"\n'
            'profile = "test"\n'
            "accounts = []\n"
        )
        checks = self._checks()
        self.assertEqual(CheckStatus.OK, checks["Calendar"][0])
        self.assertIn("Not configured", checks["Calendar"][1])

    def test_a_configured_calendar_names_its_accounts(self) -> None:
        self._write_config(
            """
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
        )
        checks = self._checks()
        self.assertEqual(CheckStatus.OK, checks["Calendar config"][0])
        self.assertIn("work", checks["Calendar config"][1])

    def test_an_unsynced_calendar_warns_about_its_index_and_mirror(self) -> None:
        self._write_config(
            """
            config_version = 3
            data_dir = "{DATA_DIR}"
            profile = "test"
            accounts = []

            [calendar]
            """
        )
        checks = self._checks()
        self.assertEqual(CheckStatus.WARN, checks["Calendar index"][0])
        self.assertIn("pony calendar sync", checks["Calendar index"][1])
        self.assertEqual(CheckStatus.WARN, checks["Calendar mirror"][0])

    _CALENDAR_ACCOUNT = """
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

    def test_a_synced_calendar_counts_its_calendars(self) -> None:
        self._write_config(self._CALENDAR_ACCOUNT)
        (self.calendar_home / "mirror" / "work" / "personal").mkdir(parents=True)
        (self.calendar_home / "mirror" / "work" / "shared").mkdir(parents=True)
        (self.calendar_home / "index.sqlite3").write_bytes(b"")
        checks = self._checks()
        self.assertEqual(CheckStatus.OK, checks["Calendar index"][0])
        self.assertEqual(CheckStatus.OK, checks["Calendar mirror"][0])
        self.assertIn("2 calendar(s)", checks["Calendar mirror"][1])

    def test_an_index_row_with_no_mirror_file_is_an_error(self) -> None:
        """The calendar's own comparison, not a directory count.

        "Is the calendar healthy?" was answered by counting mirror
        directories with `iterdir`, so a row pointing at an event whose
        `.ics` is gone -- the one corruption that makes the agenda offer
        something it cannot open -- was reachable only through
        `pony calendar doctor`.
        """
        from chronos.domain import ComponentRef, LocalStatus, VEvent
        from chronos.index_store import SqliteIndexRepository

        self._write_config(self._CALENDAR_ACCOUNT)
        (self.calendar_home / "mirror" / "work" / "personal").mkdir(parents=True)
        index = SqliteIndexRepository(self.calendar_home / "index.sqlite3")
        self.addCleanup(index.close)
        index.upsert_component(
            VEvent(
                ref=ComponentRef("work", "personal", "gone@example.com"),
                href="/dav/work/personal/gone.ics",
                etag="e1",
                raw_ics=b"",
                summary="Vanished",
                description=None,
                location=None,
                dtstart=None,
                dtend=None,
                status=None,
                local_flags=frozenset(),
                server_flags=frozenset(),
                local_status=LocalStatus.ACTIVE,
                trashed_at=None,
                synced_at=None,
            )
        )
        index.close()

        checks = self._checks()
        name = 'Calendar integrity "work/personal"'
        self.assertEqual(CheckStatus.ERROR, checks[name][0])
        self.assertIn("missing mirror files", checks[name][1])

    def test_a_broken_calendar_table_is_an_error_not_a_crash(self) -> None:
        self._write_config(
            """
            config_version = 3
            data_dir = "{DATA_DIR}"
            profile = "test"
            accounts = []

            [calendar]

            [[calendar.accounts]]
            name = "broken"
            """
        )
        checks = self._checks()
        self.assertEqual(CheckStatus.ERROR, checks["Calendar config"][0])
        self.assertIn("[calendar]", checks["Calendar config"][1])
