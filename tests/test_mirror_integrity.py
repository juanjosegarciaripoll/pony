"""Tests for mirror integrity scanning."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import (
    AccountConfig,
    FolderSyncState,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
    MirrorConfig,
    SmtpConfig,
)
from pony.index_store import SqliteIndexRepository
from pony.services import CheckStatus, check_mirror_integrity


def _make_env(
    fmt: str = "maildir",
) -> tuple[AccountConfig, SqliteIndexRepository]:
    """Create a temp mirror dir, index, and account config."""
    tmp = TMP_ROOT / "integrity" / uuid4().hex
    tmp.mkdir(parents=True, exist_ok=True)
    mirror_path = tmp / "mirror"
    mirror_path.mkdir()
    index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
    index.initialize()
    account = AccountConfig(
        name="test",
        email_address="u@example.com",
        imap_host="imap.example.com",
        smtp=SmtpConfig(host="smtp.example.com"),
        username="u",
        credentials_source="plaintext",
        mirror=MirrorConfig(path=mirror_path, format=fmt),  # type: ignore[arg-type]
    )
    return account, index


def _index_message(
    index: SqliteIndexRepository,
    folder: str,
    storage_key: str,
) -> None:
    """Insert a minimal message row into the index."""
    index.insert_message(
        message=IndexedMessage(
            message_ref=MessageRef(
                account_name="test",
                folder_name=folder,
                id=0,
            ),
            message_id=f"<{storage_key}@test>",
            sender="a@test.com",
            recipients="b@test.com",
            cc="",
            subject="Test",
            body_preview="",
            storage_key=storage_key,
            local_flags=frozenset(),
            base_flags=frozenset(),
            local_status=MessageStatus.ACTIVE,
            received_at=datetime.now(tz=UTC),
            uid=1,
            server_flags=frozenset({MessageFlag.SEEN}),
            synced_at=datetime.now(tz=UTC),
        ),
    )
    # Record a sync state so the scanner discovers this folder.
    index.record_folder_sync_state(
        state=FolderSyncState(
            account_name="test",
            folder_name=folder,
            uid_validity=1,
            highest_uid=1,
        ),
    )


class MaildirIntegrityTests(unittest.TestCase):
    def test_clean_mirror_reports_ok(self) -> None:
        account, index = _make_env()
        mirror = account.mirror.path

        # Create a Maildir message file and a matching index row.
        inbox_cur = mirror / "cur"
        inbox_cur.mkdir(parents=True)
        (mirror / "new").mkdir()
        (mirror / "tmp").mkdir()
        key = "1234567890.1.host"
        (inbox_cur / f"{key}!2,S").write_bytes(b"test")
        _index_message(index, "INBOX", key)

        result = check_mirror_integrity(account=account, index=index)
        self.assertEqual(result.status, CheckStatus.OK)
        self.assertIn("1 message", result.detail)

    def test_orphan_file_detected(self) -> None:
        account, index = _make_env()
        mirror = account.mirror.path

        inbox_cur = mirror / "cur"
        inbox_cur.mkdir(parents=True)
        (mirror / "new").mkdir()
        (mirror / "tmp").mkdir()

        # File on disk with no index row.
        (inbox_cur / "orphan.1.host!2,S").write_bytes(b"orphan")

        # Need at least a sync state for the folder to be scanned.
        index.record_folder_sync_state(
            state=FolderSyncState(
                account_name="test",
                folder_name="INBOX",
                uid_validity=1,
                highest_uid=0,
            ),
        )

        result = check_mirror_integrity(account=account, index=index)
        self.assertEqual(result.status, CheckStatus.WARN)
        self.assertIn("1 orphan", result.detail)

    def test_stale_index_row_detected(self) -> None:
        account, index = _make_env()
        mirror = account.mirror.path

        # Create Maildir structure but no file.
        (mirror / "cur").mkdir(parents=True)
        (mirror / "new").mkdir()
        (mirror / "tmp").mkdir()

        # Index row with no matching file.
        _index_message(index, "INBOX", "missing.1.host")

        result = check_mirror_integrity(account=account, index=index)
        self.assertEqual(result.status, CheckStatus.WARN)
        self.assertIn("1 stale", result.detail)

    def test_subfolder_scanned(self) -> None:
        account, index = _make_env()
        mirror = account.mirror.path

        # Create .Sent subfolder with an orphan.
        sent_cur = mirror / ".Sent" / "cur"
        sent_cur.mkdir(parents=True)
        (mirror / ".Sent" / "new").mkdir()
        (mirror / ".Sent" / "tmp").mkdir()
        (sent_cur / "orphan.2.host!2,").write_bytes(b"orphan")

        index.record_folder_sync_state(
            state=FolderSyncState(
                account_name="test",
                folder_name="Sent",
                uid_validity=1,
                highest_uid=0,
            ),
        )

        result = check_mirror_integrity(account=account, index=index)
        self.assertEqual(result.status, CheckStatus.WARN)
        self.assertIn("orphan", result.detail)

    def test_no_index_data_reports_ok(self) -> None:
        account, index = _make_env()
        result = check_mirror_integrity(account=account, index=index)
        self.assertEqual(result.status, CheckStatus.OK)
        self.assertIn("no indexed", result.detail)

    def test_mirror_not_created_reports_ok(self) -> None:
        """Account whose mirror dir doesn't exist yet."""
        tmp = TMP_ROOT / "integrity" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        index = SqliteIndexRepository(
            database_path=tmp / "index.sqlite3"
        )
        index.initialize()
        account = AccountConfig(
            name="test",
            email_address="u@example.com",
            imap_host="imap.example.com",
            smtp=SmtpConfig(host="smtp.example.com"),
            username="u",
            credentials_source="plaintext",
            mirror=MirrorConfig(
                path=tmp / "nonexistent",
                format="maildir",
            ),
        )
        result = check_mirror_integrity(account=account, index=index)
        self.assertEqual(result.status, CheckStatus.OK)


class MboxIntegrityTests(unittest.TestCase):
    def test_missing_mbox_file_reports_stale(self) -> None:
        account, index = _make_env(fmt="mbox")
        _index_message(index, "INBOX", "0")

        result = check_mirror_integrity(account=account, index=index)
        self.assertEqual(result.status, CheckStatus.WARN)
        self.assertIn("stale", result.detail)

    def test_existing_mbox_file_reports_ok(self) -> None:
        account, index = _make_env(fmt="mbox")
        mirror = account.mirror.path
        (mirror / "INBOX.mbox").write_bytes(b"")
        _index_message(index, "INBOX", "0")

        result = check_mirror_integrity(account=account, index=index)
        self.assertEqual(result.status, CheckStatus.OK)
