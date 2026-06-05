"""Tests for pony.services — build_service_status and mirror integrity checks."""

from __future__ import annotations

import sys
import unittest

from conftest import TMP_ROOT

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
        state_dir=root / "state",
        cache_dir=root / "cache",
        log_dir=root / "state" / "logs",
        index_db_file=root / "data" / "index.sqlite3",
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
