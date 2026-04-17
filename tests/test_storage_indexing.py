"""Tests for mirror-to-index ingestion."""

from __future__ import annotations

import unittest
from email.message import EmailMessage
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import FolderRef, SearchQuery
from pony.index_store import SqliteIndexRepository
from pony.storage import MaildirMirrorRepository
from pony.storage_indexing import ingest_account_from_mirror


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


def sample_message_bytes() -> bytes:
    """Create deterministic RFC 5322 fixture bytes."""
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "user@example.com"
    message["Subject"] = "indexed sample"
    message["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
    message.set_content("indexed body sample")
    return message.as_bytes()
