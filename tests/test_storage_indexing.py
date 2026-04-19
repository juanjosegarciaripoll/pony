"""Tests for mirror-to-index ingestion."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from email.message import EmailMessage
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import (
    FolderRef,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
    SearchQuery,
)
from pony.index_store import SqliteIndexRepository
from pony.storage import MaildirMirrorRepository
from pony.storage_indexing import (
    RescanResult,
    ingest_account_from_mirror,
    rescan_local_account,
)


class StorageIndexingTestCase(unittest.TestCase):
    """Validate mapping from mirror storage location into index rows."""

    def test_ingest_account_from_mirror_indexes_message(self) -> None:
        root = TMP_ROOT / "storage-indexing" / uuid4().hex
        mirror_root = root / "mirror"
        data_root = root / "data"
        mirror_root.mkdir(parents=True, exist_ok=True)
        data_root.mkdir(parents=True, exist_ok=True)

        mirror = MaildirMirrorRepository(account_name="personal", root_dir=mirror_root)
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        _ = mirror.store_message(folder=folder, raw_message=sample_message_bytes())

        index = SqliteIndexRepository(database_path=data_root / "index.sqlite3")
        index.initialize()

        upserted = ingest_account_from_mirror(
            mirror_repository=mirror,
            index_repository=index,
            account_name="personal",
        )
        self.assertEqual(upserted, 1)

        hits = index.search(query=SearchQuery(text="indexed"), account_name="personal")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].storage_key)


class RescanLocalAccountTestCase(unittest.TestCase):
    """Validate the delta-rescan used for local accounts at TUI startup."""

    def _setup(self) -> tuple[MaildirMirrorRepository, SqliteIndexRepository]:
        root = TMP_ROOT / "storage-indexing-rescan" / uuid4().hex
        mirror_root = root / "mirror"
        data_root = root / "data"
        mirror_root.mkdir(parents=True, exist_ok=True)
        data_root.mkdir(parents=True, exist_ok=True)
        mirror = MaildirMirrorRepository(account_name="local", root_dir=mirror_root)
        index = SqliteIndexRepository(database_path=data_root / "index.sqlite3")
        index.initialize()
        return mirror, index

    def test_ingests_new_files_and_skips_already_indexed(self) -> None:
        mirror, index = self._setup()
        folder = FolderRef(account_name="local", folder_name="INBOX")
        key_a = mirror.store_message(folder=folder, raw_message=sample_message_bytes())

        announce_calls: list[RescanResult] = []
        result = rescan_local_account(
            mirror_repository=mirror,
            index_repository=index,
            account_name="local",
            on_plan=announce_calls.append,
        )
        self.assertEqual(result, RescanResult(added=1, removed=0))
        self.assertEqual(announce_calls, [RescanResult(added=1, removed=0)])

        # Second pass sees the same file — no work, no announcement.
        announce_calls.clear()
        result = rescan_local_account(
            mirror_repository=mirror,
            index_repository=index,
            account_name="local",
            on_plan=announce_calls.append,
        )
        self.assertEqual(result, RescanResult(added=0, removed=0))
        self.assertEqual(announce_calls, [])

        # Add a second file; announcement fires once before progress callbacks.
        key_b = mirror.store_message(folder=folder, raw_message=sample_message_bytes())
        self.assertNotEqual(key_a, key_b)
        progress_calls: list[tuple[str, int, int]] = []
        announce_calls.clear()
        result = rescan_local_account(
            mirror_repository=mirror,
            index_repository=index,
            account_name="local",
            on_plan=announce_calls.append,
            progress=lambda f, c, t: progress_calls.append((f, c, t)),
        )
        self.assertEqual(result, RescanResult(added=1, removed=0))
        self.assertEqual(announce_calls, [RescanResult(added=1, removed=0)])
        self.assertEqual(progress_calls, [("INBOX", 1, 1)])

    def test_prunes_rows_whose_files_vanished(self) -> None:
        mirror, index = self._setup()
        folder = FolderRef(account_name="local", folder_name="INBOX")
        key = mirror.store_message(folder=folder, raw_message=sample_message_bytes())
        rescan_local_account(
            mirror_repository=mirror,
            index_repository=index,
            account_name="local",
        )

        mirror.delete_message(folder=folder, storage_key=key)
        result = rescan_local_account(
            mirror_repository=mirror,
            index_repository=index,
            account_name="local",
        )
        self.assertEqual(result, RescanResult(added=0, removed=1))
        self.assertEqual(
            list(index.list_folder_messages(folder=folder)),
            [],
        )

    def test_preserves_pending_rows_with_empty_storage_key(self) -> None:
        """``uid=NULL`` + empty ``storage_key`` rows are pending appends —
        they must survive a rescan that sees no matching file on disk."""
        mirror, index = self._setup()
        folder = FolderRef(account_name="local", folder_name="INBOX")
        pending = IndexedMessage(
            message_ref=MessageRef(
                account_name="local",
                folder_name="INBOX",
                rfc5322_id="<pending@example.com>",
            ),
            sender="me@example.com",
            recipients="you@example.com",
            cc="",
            subject="pending append",
            body_preview="",
            storage_key="",
            local_flags=frozenset({MessageFlag.DRAFT}),
            base_flags=frozenset(),
            local_status=MessageStatus.ACTIVE,
            received_at=datetime.now(UTC),
        )
        index.upsert_message(message=pending)

        result = rescan_local_account(
            mirror_repository=mirror,
            index_repository=index,
            account_name="local",
        )
        self.assertEqual(result, RescanResult(added=0, removed=0))
        survivors = list(index.list_folder_messages(folder=folder))
        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0].storage_key, "")


def sample_message_bytes() -> bytes:
    """Create deterministic RFC 5322 fixture bytes."""
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "user@example.com"
    message["Subject"] = "indexed sample"
    message["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
    message.set_content("indexed body sample")
    return message.as_bytes()
