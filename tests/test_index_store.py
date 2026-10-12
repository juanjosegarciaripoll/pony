"""Tests for the SQLite index repository."""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import (
    Contact,
    FolderRef,
    FolderSyncState,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
    SearchQuery,
)
from pony.index_store import _BATCH_COMMIT_ROWS, SqliteIndexRepository
from pony.search_parser import parse_query


class UpsertAndListTestCase(unittest.TestCase):
    """Validate basic insert, upsert idempotence, and folder listing."""

    def test_upsert_and_list_folder_messages(self) -> None:
        repo = _fresh_repo()
        folder = FolderRef(account_name="personal", folder_name="INBOX")

        repo.upsert_message(message=_make_message("m-1", subject="Alpha"))
        repo.upsert_message(message=_make_message("m-2", subject="Beta"))

        rows = repo.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 2)
        ids = {r.message_id for r in rows}
        self.assertEqual(ids, {"m-1", "m-2"})

    def test_update_replaces_row_in_place(self) -> None:
        """Identity is the row id; updating with the same id is in place."""
        import dataclasses

        repo = _fresh_repo()
        folder = FolderRef(account_name="personal", folder_name="INBOX")

        saved = repo.insert_message(
            message=_make_message("m-1", subject="Original"),
        )
        repo.update_message(
            message=dataclasses.replace(saved, subject="Updated"),
        )

        rows = repo.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].subject, "Updated")

    def test_flags_roundtrip(self) -> None:
        repo = _fresh_repo()
        flags = frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED})
        repo.upsert_message(message=_make_message("m-1", local_flags=flags))

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = repo.list_folder_messages(folder=folder)
        self.assertEqual(rows[0].local_flags, flags)

    def test_list_folder_messages_empty_folder(self) -> None:
        repo = _fresh_repo()
        other = FolderRef(account_name="personal", folder_name="Sent")
        self.assertEqual(repo.list_folder_messages(folder=other), ())

    def test_list_folder_storage_keys_returns_dict(self) -> None:
        repo = _fresh_repo()
        a = repo.insert_message(message=_make_message("m-1", storage_key="k-1"))
        b = repo.insert_message(message=_make_message("m-2", storage_key="k-2"))
        # Pending-append row (empty storage_key) must be filtered out.
        repo.insert_message(message=_make_message("m-3", storage_key=""))

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        keys = repo.list_folder_storage_keys(folder=folder)

        self.assertEqual(set(keys), {"k-1", "k-2"})
        self.assertEqual(keys["k-1"], a.message_ref)
        self.assertEqual(keys["k-2"], b.message_ref)

    def test_list_folder_messages_isolates_folders(self) -> None:
        repo = _fresh_repo()
        repo.upsert_message(
            message=_make_message("m-1", folder_name="INBOX", subject="Inbox msg")
        )
        repo.upsert_message(
            message=_make_message("m-2", folder_name="Sent", subject="Sent msg")
        )

        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        sent = FolderRef(account_name="personal", folder_name="Sent")
        self.assertEqual(len(repo.list_folder_messages(folder=inbox)), 1)
        self.assertEqual(len(repo.list_folder_messages(folder=sent)), 1)


class DeleteMessageTestCase(unittest.TestCase):
    """Validate index deletion keeps the index consistent with mirror state."""

    def test_delete_removes_message(self) -> None:
        repo = _fresh_repo()
        saved = repo.insert_message(message=_make_message("m-1"))

        repo.delete_message(message_ref=saved.message_ref)

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        self.assertEqual(repo.list_folder_messages(folder=folder), ())

    def test_delete_nonexistent_is_silent(self) -> None:
        repo = _fresh_repo()
        ref = MessageRef(
            account_name="personal",
            folder_name="INBOX",
            id=99999,
        )
        repo.delete_message(message_ref=ref)  # must not raise


class SearchTestCase(unittest.TestCase):
    """Validate search across every indexed field and both case modes."""

    def setUp(self) -> None:
        self.repo = _fresh_repo()
        self.repo.upsert_message(
            message=_make_message(
                "m-1",
                sender="alice@example.com",
                recipients="bob@example.com",
                cc="carol@example.com",
                subject="Quarterly Report",
                body_preview="Please review the attached report.",
            )
        )
        self.repo.upsert_message(
            message=_make_message(
                "m-2",
                sender="bob@example.com",
                recipients="alice@example.com",
                cc="",
                subject="Re: Quarterly Report",
                body_preview="Thanks, looks good.",
            )
        )

    def test_text_matches_subject_and_body(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(text="report"), account_name="personal"
        )
        self.assertEqual(len(hits), 2)

    def test_text_no_match(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(text="zzznomatch"), account_name="personal"
        )
        self.assertEqual(hits, ())

    def test_from_address_filter(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(from_address="alice"), account_name="personal"
        )
        ids = {h.message_id for h in hits}
        self.assertEqual(ids, {"m-1"})

    def test_to_address_filter(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(to_address="alice"), account_name="personal"
        )
        ids = {h.message_id for h in hits}
        self.assertEqual(ids, {"m-2"})

    def test_cc_address_filter(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(cc_address="carol"), account_name="personal"
        )
        ids = {h.message_id for h in hits}
        self.assertEqual(ids, {"m-1"})

    def test_subject_filter(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(subject="Re: Q"), account_name="personal"
        )
        ids = {h.message_id for h in hits}
        self.assertEqual(ids, {"m-2"})

    def test_body_filter(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(body="looks good"), account_name="personal"
        )
        ids = {h.message_id for h in hits}
        self.assertEqual(ids, {"m-2"})

    def test_combined_filters_narrow_results(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(from_address="alice", subject="Quarterly"),
            account_name="personal",
        )
        ids = {h.message_id for h in hits}
        self.assertEqual(ids, {"m-1"})

    def test_case_insensitive_by_default(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(subject="quarterly report"), account_name="personal"
        )
        self.assertEqual(len(hits), 2)

    def test_matching_ignores_case_however_it_is_typed(self) -> None:
        """There is no case-sensitive mode, and there never was one.

        ``SearchQuery`` carried a ``case_sensitive`` flag and the parser
        set it from a ``case:`` token, but nothing read it — FTS5's
        ``unicode61`` tokenizer folds case and diacritics
        unconditionally.  Both are gone rather than left looking like an
        option that does nothing.
        """
        for spelling in ("quarterly report", "QUARTERLY REPORT", "Quarterly Report"):
            with self.subTest(spelling=spelling):
                hits = self.repo.search(
                    query=SearchQuery(subject=spelling),
                    account_name="personal",
                )
                self.assertEqual(len(hits), 2)

    def test_cross_account_search_none_returns_all(self) -> None:
        # add a message in a different account
        other = _make_message(
            "m-3",
            account_name="work",
            subject="Work thing",
            body_preview="quarterly numbers",
        )
        self.repo.upsert_message(message=other)

        hits = self.repo.search(query=SearchQuery(text="quarterly"), account_name=None)
        ids = {h.message_id for h in hits}
        self.assertIn("m-1", ids)
        self.assertIn("m-2", ids)
        self.assertIn("m-3", ids)

    def test_account_scoped_search_excludes_other_account(self) -> None:
        other = _make_message(
            "m-3",
            account_name="work",
            subject="Work thing",
            body_preview="quarterly numbers",
        )
        self.repo.upsert_message(message=other)

        hits = self.repo.search(
            query=SearchQuery(text="quarterly"), account_name="personal"
        )
        ids = {h.message_id for h in hits}
        self.assertNotIn("m-3", ids)


class DiacriticAndCaseFoldingSearchTestCase(unittest.TestCase):
    """FTS5 ``unicode61`` tokenizer folds case + diacritics everywhere."""

    def test_ascii_query_matches_accented_subject(self) -> None:
        repo = _fresh_repo()
        repo.upsert_message(
            message=_make_message(
                "m-1",
                subject="Carta de María",
                body_preview="contenido cualquiera",
            ),
        )
        hits = repo.search(
            query=SearchQuery(subject="maria"),
            account_name="personal",
        )
        self.assertEqual(
            {h.message_id for h in hits},
            {"m-1"},
        )

    def test_ascii_query_matches_accented_sender(self) -> None:
        repo = _fresh_repo()
        repo.upsert_message(
            message=_make_message(
                "m-1",
                sender="Juan.Garcia@example.com",
                subject="hello",
            ),
        )
        hits = repo.search(
            query=SearchQuery(from_address="garcia"),
            account_name="personal",
        )
        self.assertEqual(
            {h.message_id for h in hits},
            {"m-1"},
        )

    def test_field_scoping_is_enforced(self) -> None:
        repo = _fresh_repo()
        repo.upsert_message(
            message=_make_message(
                "m-1",
                sender="foo@example.com",
                subject="an unrelated subject",
                body_preview="some body",
            ),
        )
        # "foo" appears only in the sender column — a subject-scoped
        # query must not find it.
        hits = repo.search(
            query=SearchQuery(subject="foo"),
            account_name="personal",
        )
        self.assertEqual(hits, ())


class FolderSyncStateTestCase(unittest.TestCase):
    """Validate folder sync watermark persistence."""

    def test_roundtrip(self) -> None:
        repo = _fresh_repo()
        state = FolderSyncState(
            account_name="personal",
            folder_name="INBOX",
            uid_validity=999,
            highest_uid=42,
        )
        repo.record_folder_sync_state(state=state)

        loaded = repo.get_folder_sync_state(
            account_name="personal", folder_name="INBOX"
        )
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.highest_uid, 42)
        self.assertEqual(loaded.uid_validity, 999)

    def test_missing_returns_none(self) -> None:
        repo = _fresh_repo()
        result = repo.get_folder_sync_state(
            account_name="personal", folder_name="INBOX"
        )
        self.assertIsNone(result)

    def test_update(self) -> None:
        repo = _fresh_repo()
        repo.record_folder_sync_state(
            state=FolderSyncState(
                account_name="personal",
                folder_name="INBOX",
                uid_validity=1,
                highest_uid=10,
            )
        )
        repo.record_folder_sync_state(
            state=FolderSyncState(
                account_name="personal",
                folder_name="INBOX",
                uid_validity=1,
                highest_uid=20,
            )
        )
        loaded = repo.get_folder_sync_state(
            account_name="personal", folder_name="INBOX"
        )
        assert loaded is not None
        self.assertEqual(loaded.highest_uid, 20)


class SchemaVersionGateTestCase(unittest.TestCase):
    """initialize() refuses to open a DB older than the current schema."""

    def test_initialize_refuses_legacy_db(self) -> None:
        import sqlite3

        from pony.index_store import SchemaMismatchError

        path = TMP_ROOT / "legacy" / f"{uuid4().hex}.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create a fake "legacy" DB: the messages table exists but
        # user_version has not been bumped to the current schema.
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE messages (account_name TEXT, folder_name TEXT)",
            )
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
        finally:
            conn.close()

        repo = SqliteIndexRepository(database_path=path)
        with self.assertRaises(SchemaMismatchError) as ctx:
            repo.initialize()
        self.assertEqual(ctx.exception.database_path, path)
        self.assertEqual(ctx.exception.found, 0)
        self.assertGreaterEqual(ctx.exception.expected, 2)

    def test_initialize_creates_fresh_db_at_current_version(self) -> None:
        import sqlite3

        repo = _fresh_repo()
        conn = sqlite3.connect(repo._database_path)  # noqa: SLF001
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
        self.assertGreaterEqual(version, 2)


class V2ToV3MigrationTestCase(unittest.TestCase):
    """The v2 → v3 in-place migration drops synthetic-mid orphans and
    backfills uid_validity from folder_sync_state, while preserving
    every real row."""

    def _build_v2_db(self, path: Path) -> None:
        """Create a minimal v2 (synthetic-mid era) database at *path*."""
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE messages (
                    account_name     TEXT NOT NULL,
                    folder_name      TEXT NOT NULL,
                    message_id       TEXT NOT NULL,
                    sender           TEXT NOT NULL,
                    recipients       TEXT NOT NULL,
                    cc               TEXT NOT NULL,
                    subject          TEXT NOT NULL,
                    body_preview     TEXT NOT NULL,
                    storage_key      TEXT NOT NULL DEFAULT '',
                    has_attachments  INTEGER NOT NULL DEFAULT 0,
                    local_flags      TEXT NOT NULL,
                    base_flags       TEXT NOT NULL,
                    local_status     TEXT NOT NULL,
                    received_at      TEXT NOT NULL,
                    uid              INTEGER,
                    server_flags     TEXT NOT NULL DEFAULT '',
                    extra_imap_flags TEXT NOT NULL DEFAULT '',
                    trashed_at       TEXT,
                    synced_at        TEXT,
                    PRIMARY KEY (account_name, folder_name, message_id)
                );
                CREATE TABLE folder_sync_state (
                    account_name   TEXT NOT NULL,
                    folder_name    TEXT NOT NULL,
                    uid_validity   INTEGER NOT NULL,
                    highest_uid    INTEGER NOT NULL,
                    uidnext        INTEGER NOT NULL DEFAULT 0,
                    highest_modseq INTEGER NOT NULL DEFAULT 0,
                    synced_at      TEXT NOT NULL,
                    PRIMARY KEY (account_name, folder_name)
                );
                CREATE TABLE pending_operations (
                    operation_id   TEXT PRIMARY KEY,
                    account_name   TEXT NOT NULL,
                    folder_name    TEXT NOT NULL,
                    message_id     TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    created_at     TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO folder_sync_state VALUES "
                "('personal','INBOX',42,7,8,12345,'2026-04-20T00:00:00+00:00')",
            )
            # A real (non-synthetic) row, synced.
            conn.execute(
                "INSERT INTO messages VALUES "
                "('personal','INBOX','<real@example.com>',"
                "'a@example.com','b@example.com','','Real subject','body',"
                "'maildir-key',0,'seen','seen','active',"
                "'2026-04-20T10:00:00+00:00',7,'seen','',NULL,"
                "'2026-04-20T10:00:01+00:00')",
            )
            # A synthetic-mid orphan — exactly the row that drove the
            # APPEND loop in production.
            conn.execute(
                "INSERT INTO messages VALUES "
                "('personal','INBOX','<synthetic-deadbeef@pony.local>',"
                "'a@example.com','b@example.com','','Stuck synthetic','body',"
                "'',0,'','','active',"
                "'2026-04-20T10:00:00+00:00',NULL,'','',NULL,NULL)",
            )
            # A synthetic-mid row with a uid (e.g. an indexed copy that
            # *did* round-trip): this one survives — only ``uid IS NULL``
            # synthetics drove the duplicate-APPEND loop.
            conn.execute(
                "INSERT INTO messages VALUES "
                "('personal','INBOX','<synthetic-cafe@pony.local>',"
                "'a@example.com','b@example.com','','Synthetic w/ uid','body',"
                "'k2',0,'','','active',"
                "'2026-04-20T10:00:00+00:00',9,'','',NULL,"
                "'2026-04-20T10:00:01+00:00')",
            )
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        finally:
            conn.close()

    def test_v2_to_v3_leaves_the_search_index_usable(self) -> None:
        """The migrated rows must be searchable afterwards.

        The migration drops ``messages_fts`` and copies the rows into the
        new table before the triggers exist, so nothing populates the
        index.  Search then silently returned nothing for every upgraded
        database, and no ordinary sync repaired it — unchanged rows are
        never rewritten.
        """
        path = TMP_ROOT / "v2-migration-fts" / f"{uuid4().hex}.sqlite3"
        self._build_v2_db(path)

        repo = SqliteIndexRepository(database_path=path)
        repo.initialize()

        hits = repo.search(query=parse_query("Real"), account_name=None)
        self.assertEqual(
            [m.message_id for m in hits],
            ["<real@example.com>"],
            "migrated rows are absent from the FTS index",
        )

    def test_v2_to_v3_drops_synthetic_orphans_and_backfills_validity(
        self,
    ) -> None:
        path = TMP_ROOT / "v2-migration" / f"{uuid4().hex}.sqlite3"
        self._build_v2_db(path)

        repo = SqliteIndexRepository(database_path=path)
        repo.initialize()  # runs the v2 → v3 migration

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = repo.list_folder_messages(folder=folder)
        # The synthetic-mid orphan is gone; the real row and the
        # synthetic-with-uid row survive.
        ids = {r.message_id for r in rows}
        self.assertIn("<real@example.com>", ids)
        self.assertIn("<synthetic-cafe@pony.local>", ids)
        self.assertNotIn("<synthetic-deadbeef@pony.local>", ids)
        # uid_validity is backfilled from folder_sync_state on every
        # surviving row.
        self.assertTrue(all(r.uid_validity == 42 for r in rows))
        # Every row got a fresh autoincrement id.
        self.assertEqual(len({r.message_ref.id for r in rows}), len(rows))

    def test_v2_to_v3_is_idempotent(self) -> None:
        path = TMP_ROOT / "v2-migration-idem" / f"{uuid4().hex}.sqlite3"
        self._build_v2_db(path)

        repo = SqliteIndexRepository(database_path=path)
        repo.initialize()  # v2 → v3
        repo.initialize()  # second init must be a no-op

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = repo.list_folder_messages(folder=folder)
        self.assertEqual(
            {r.message_id for r in rows},
            {"<real@example.com>", "<synthetic-cafe@pony.local>"},
        )

    def test_v2_to_v3_drops_pending_operations_table(self) -> None:
        import sqlite3

        path = TMP_ROOT / "v2-migration-pending" / f"{uuid4().hex}.sqlite3"
        self._build_v2_db(path)

        repo = SqliteIndexRepository(database_path=path)
        repo.initialize()

        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='pending_operations'",
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row, "pending_operations table must be dropped")

    def test_load_contacts_for_backup_reads_legacy_db(self) -> None:
        """The emergency loader must work on a DB initialize() refuses."""
        import sqlite3

        from pony.index_store import load_contacts_for_backup

        path = TMP_ROOT / "legacy-contacts" / f"{uuid4().hex}.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE contacts (
                    id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    affix TEXT NOT NULL DEFAULT '[]',
                    organization TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE contact_emails (
                    contact_id INTEGER NOT NULL,
                    email_address TEXT NOT NULL,
                    PRIMARY KEY (email_address)
                );
                CREATE TABLE contact_aliases (
                    contact_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    PRIMARY KEY (contact_id, alias)
                );
                CREATE TABLE messages (account_name TEXT);
                INSERT INTO contacts VALUES
                    (1, 'María', 'López', '[]', '', '', 3,
                     '2026-04-10T12:00:00+00:00',
                     '2026-04-10T12:00:00+00:00',
                     '2026-04-10T12:00:00+00:00');
                INSERT INTO contact_emails VALUES (1, 'maria@example.com');
                INSERT INTO contact_aliases VALUES (1, 'Mari');
                PRAGMA user_version = 0;
                """,
            )
            conn.commit()
        finally:
            conn.close()

        contacts = load_contacts_for_backup(database_path=path)
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].first_name, "María")
        self.assertEqual(contacts[0].emails, ("maria@example.com",))
        self.assertEqual(contacts[0].aliases, ("Mari",))

    def test_load_contacts_for_backup_tolerates_missing_file(self) -> None:
        from pony.index_store import load_contacts_for_backup

        path = TMP_ROOT / "missing" / f"{uuid4().hex}.sqlite3"
        self.assertEqual(load_contacts_for_backup(database_path=path), [])


class BatchedTransactionTestCase(unittest.TestCase):
    """Verify connection() batched-transaction semantics."""

    def test_connection_batches_writes(self) -> None:
        """Multiple upserts inside connection() are visible after exit."""
        repo = _fresh_repo()
        with repo.connection():
            repo.upsert_message(message=_make_message("m-1", subject="A"))
            repo.upsert_message(message=_make_message("m-2", subject="B"))

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = repo.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 2)

    def test_connection_rolls_back_on_exception(self) -> None:
        """Writes inside a failed connection() block are not persisted."""
        repo = _fresh_repo()
        repo.upsert_message(message=_make_message("m-0", subject="Before"))

        with self.assertRaises(RuntimeError), repo.connection():
            repo.upsert_message(
                message=_make_message("m-1", subject="A"),
            )
            raise RuntimeError("simulated crash")

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = repo.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].message_id, "m-0")

    def test_nested_connection_reuses_outer(self) -> None:
        """Nested connection() blocks do not commit early."""
        repo = _fresh_repo()
        with repo.connection():
            repo.upsert_message(message=_make_message("m-1"))
            with repo.connection():
                repo.upsert_message(message=_make_message("m-2"))
            # Inner block exited — should NOT have committed yet.

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = repo.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 2)

    def test_methods_work_without_connection_block(self) -> None:
        """Methods still work standalone (one connection per call)."""
        repo = _fresh_repo()
        repo.upsert_message(message=_make_message("m-1"))

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = repo.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)


class NarrowSyncProjectionTestCase(unittest.TestCase):
    """Validate the narrow per-row projections used by the sync planner."""

    def test_slow_path_rows_include_all_folder_rows(self) -> None:
        repo = _fresh_repo()
        repo.upsert_message(
            message=_make_message(
                "m-1",
                uid=10,
                local_flags=frozenset({MessageFlag.SEEN}),
                base_flags=frozenset({MessageFlag.SEEN}),
                extra_imap_flags=frozenset({"$Important"}),
            )
        )
        repo.upsert_message(
            message=_make_message(
                "m-2",
                uid=None,
                local_status=MessageStatus.ACTIVE,
            )
        )
        repo.upsert_message(
            message=_make_message(
                "m-3",
                uid=12,
                local_status=MessageStatus.TRASHED,
            )
        )
        repo.upsert_message(
            message=_make_message(
                "m-other",
                folder_name="Sent",
                uid=99,
            )
        )

        rows = repo.list_folder_slow_path_rows(
            account_name="personal",
            folder_name="INBOX",
        )
        self.assertEqual(len(rows), 3)
        by_mid = {r.message_id: r for r in rows}

        self.assertEqual(by_mid["m-1"].uid, 10)
        self.assertEqual(
            by_mid["m-1"].local_flags,
            frozenset({MessageFlag.SEEN}),
        )
        self.assertEqual(
            by_mid["m-1"].base_flags,
            frozenset({MessageFlag.SEEN}),
        )
        self.assertEqual(
            by_mid["m-1"].extra_imap_flags,
            frozenset({"$Important"}),
        )

        self.assertIsNone(by_mid["m-2"].uid)
        self.assertEqual(by_mid["m-2"].local_status, MessageStatus.ACTIVE)

        self.assertEqual(by_mid["m-3"].local_status, MessageStatus.TRASHED)

    def test_slow_path_rows_empty_folder(self) -> None:
        repo = _fresh_repo()
        rows = repo.list_folder_slow_path_rows(
            account_name="personal",
            folder_name="INBOX",
        )
        self.assertEqual(rows, ())

    def test_base_flags_returns_uid_bearing_rows_only(self) -> None:
        repo = _fresh_repo()
        repo.upsert_message(
            message=_make_message(
                "m-1",
                uid=10,
                base_flags=frozenset({MessageFlag.SEEN}),
                extra_imap_flags=frozenset({"$Junk", "$Important"}),
            )
        )
        repo.upsert_message(
            message=_make_message(
                "m-2",
                uid=11,
                base_flags=frozenset(),
            )
        )
        repo.upsert_message(message=_make_message("m-3", uid=None))

        result = repo.list_folder_base_flags(
            account_name="personal",
            folder_name="INBOX",
        )
        self.assertEqual(set(result.keys()), {10, 11})
        self.assertEqual(result[10][0], frozenset({MessageFlag.SEEN}))
        self.assertEqual(result[10][1], frozenset({"$Junk", "$Important"}))
        self.assertEqual(result[11], (frozenset(), frozenset()))

    def test_base_flags_isolates_folder(self) -> None:
        repo = _fresh_repo()
        repo.upsert_message(message=_make_message("m-1", uid=10))
        repo.upsert_message(
            message=_make_message(
                "m-other",
                folder_name="Sent",
                uid=99,
            )
        )
        result = repo.list_folder_base_flags(
            account_name="personal",
            folder_name="INBOX",
        )
        self.assertEqual(set(result.keys()), {10})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_repo() -> SqliteIndexRepository:
    temp_root = TMP_ROOT / "index"
    temp_root.mkdir(parents=True, exist_ok=True)
    repo = SqliteIndexRepository(database_path=temp_root / f"{uuid4().hex}.sqlite3")
    repo.initialize()
    return repo


def _make_message(
    message_id: str,
    *,
    account_name: str = "personal",
    folder_name: str = "INBOX",
    sender: str = "sender@example.com",
    recipients: str = "recipient@example.com",
    cc: str = "",
    subject: str = "Test Subject",
    body_preview: str = "Test body preview.",
    local_flags: frozenset[MessageFlag] = frozenset(),
    base_flags: frozenset[MessageFlag] = frozenset(),
    local_status: MessageStatus = MessageStatus.ACTIVE,
    uid: int | None = None,
    extra_imap_flags: frozenset[str] = frozenset(),
    storage_key: str | None = None,
) -> IndexedMessage:
    """Build an unsaved IndexedMessage; the row id is assigned at insert time."""
    return IndexedMessage(
        message_ref=MessageRef(
            account_name=account_name,
            folder_name=folder_name,
            id=0,
        ),
        message_id=message_id,
        sender=sender,
        recipients=recipients,
        cc=cc,
        subject=subject,
        body_preview=body_preview,
        storage_key=message_id if storage_key is None else storage_key,
        local_flags=local_flags,
        base_flags=base_flags,
        local_status=local_status,
        received_at=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
        uid=uid,
        extra_imap_flags=extra_imap_flags,
    )


class MarkFolderReadTestCase(unittest.TestCase):
    """Tests for mark_folder_read."""

    def test_marks_all_active_messages_as_seen(self) -> None:
        from datetime import UTC, datetime

        from pony.domain import FolderRef, MessageRef, MessageStatus

        repo = _fresh_repo()
        folder = FolderRef(account_name="acct", folder_name="INBOX")

        # Insert two messages without SEEN flag
        from pony.domain import IndexedMessage

        for i in range(2):
            row = IndexedMessage(
                message_ref=MessageRef(account_name="acct", folder_name="INBOX", id=0),
                message_id=f"<mark-read-{i}@x.com>",
                sender="alice@x.com",
                recipients="bob@x.com",
                cc="",
                subject=f"Message {i}",
                body_preview="",
                storage_key=f"key{i}",
                local_flags=frozenset(),
                base_flags=frozenset(),
                local_status=MessageStatus.ACTIVE,
                received_at=datetime(2024, 1, i + 1, tzinfo=UTC),
            )
            repo.insert_message(message=row)

        count = repo.mark_folder_read(folder=folder)
        self.assertEqual(count, 2)

        messages = list(repo.list_folder_messages(folder=folder))
        for m in messages:
            self.assertIn(MessageFlag.SEEN, m.local_flags)

    def test_mark_folder_read_already_seen_returns_zero(self) -> None:
        from datetime import UTC, datetime

        from pony.domain import FolderRef, IndexedMessage, MessageRef, MessageStatus

        repo = _fresh_repo()
        folder = FolderRef(account_name="acct", folder_name="INBOX")

        row = IndexedMessage(
            message_ref=MessageRef(account_name="acct", folder_name="INBOX", id=0),
            message_id="<already-seen@x.com>",
            sender="alice@x.com",
            recipients="bob@x.com",
            cc="",
            subject="Already seen",
            body_preview="",
            storage_key="key",
            local_flags=frozenset({MessageFlag.SEEN}),
            base_flags=frozenset({MessageFlag.SEEN}),
            local_status=MessageStatus.ACTIVE,
            received_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        repo.insert_message(message=row)
        count = repo.mark_folder_read(folder=folder)
        self.assertEqual(count, 0)


class MissingDatabaseTestCase(unittest.TestCase):
    """Read paths must answer for a database that was never created.

    ``pony doctor``, the folder panel's unread badges and the sync
    planner all query the index before the first sync has written
    anything.  Each read must return an empty result rather than raise
    a "no such table" error.
    """

    def _unwritten_repo(self) -> SqliteIndexRepository:
        temp_root = TMP_ROOT / "index"
        temp_root.mkdir(parents=True, exist_ok=True)
        # Deliberately not initialize()d — the file does not exist.
        return SqliteIndexRepository(
            database_path=temp_root / f"absent-{uuid4().hex}.sqlite3"
        )

    def test_listing_accounts_returns_empty(self) -> None:
        self.assertEqual(self._unwritten_repo().list_indexed_accounts(), [])

    def test_unread_counts_return_empty(self) -> None:
        repo = self._unwritten_repo()
        self.assertEqual(repo.unread_counts_by_folder(account_name="personal"), {})

    def test_folder_sync_states_return_empty(self) -> None:
        repo = self._unwritten_repo()
        self.assertEqual(repo.list_folder_sync_states(account_name="personal"), [])


class QueryGuardTestCase(unittest.TestCase):
    """Short-circuits that keep degenerate queries away from SQLite."""

    def test_update_requires_a_row_id(self) -> None:
        """``insert_message`` assigns ids; updating an unsaved row is a bug."""
        repo = _fresh_repo()
        unsaved = _make_message("<unsaved@example.com>")

        with self.assertRaises(ValueError) as ctx:
            repo.update_message(message=unsaved)

        self.assertIn("requires a row id", str(ctx.exception))

    def test_an_empty_message_id_matches_nothing(self) -> None:
        """Rows imported without a Message-ID must not all match each other."""
        repo = _fresh_repo()
        repo.insert_message(message=_make_message(""))

        self.assertEqual(
            repo.find_messages_by_message_id(
                account_name="personal",
                folder_name="INBOX",
                message_id="",
            ),
            (),
        )

    def test_message_id_lookup_can_span_every_folder(self) -> None:
        """Omitting the folder searches the whole account."""
        repo = _fresh_repo()
        repo.insert_message(
            message=_make_message("<shared@example.com>", folder_name="INBOX")
        )
        repo.insert_message(
            message=_make_message(
                "<shared@example.com>",
                folder_name="Archive",
                storage_key="archive-copy",
            )
        )

        scoped = repo.find_messages_by_message_id(
            account_name="personal",
            folder_name="INBOX",
            message_id="<shared@example.com>",
        )
        everywhere = repo.find_messages_by_message_id(
            account_name="personal",
            folder_name=None,
            message_id="<shared@example.com>",
        )

        self.assertEqual(len(scoped), 1)
        self.assertEqual(len(everywhere), 2)

    def test_an_empty_uid_set_returns_no_slow_path_rows(self) -> None:
        repo = _fresh_repo()

        self.assertEqual(
            repo.list_folder_slow_path_rows_by_uid(
                account_name="personal",
                folder_name="INBOX",
                uids=(),
            ),
            (),
        )

    def test_a_blank_contact_prefix_returns_no_suggestions(self) -> None:
        repo = _fresh_repo()

        self.assertEqual(repo.search_contacts(prefix="   ", limit=10), [])

    def test_loading_an_unknown_contact_id_raises(self) -> None:
        """The batch loader returning nothing must not yield a bare IndexError."""
        repo = _fresh_repo()

        with self.assertRaises(KeyError):
            repo._load_contact(99999)


class SearchSemanticsTest(unittest.TestCase):
    """What a typed query is expected to mean."""

    def _store(self) -> SqliteIndexRepository:
        path = TMP_ROOT / "search-semantics" / f"{uuid4().hex}.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        repo = SqliteIndexRepository(database_path=path)
        repo.initialize()
        return repo

    def _seed(self, repo: SqliteIndexRepository) -> None:
        repo.insert_message(
            message=_make_message(
                "<a@x>", subject="Invoice #3 paid", body_preview="thanks"
            )
        )
        repo.insert_message(
            message=_make_message(
                "<b@x>", subject="Paid the invoice", body_preview="regards"
            )
        )
        repo.insert_message(
            message=_make_message(
                "<c@x>",
                subject="Meeting notes",
                body_preview="please pay the invoice today",
            )
        )

    def _find(self, repo: SqliteIndexRepository, raw: str) -> set[str]:
        return {
            m.subject for m in repo.search(query=parse_query(raw), account_name=None)
        }

    def test_two_words_mean_both_words_not_a_phrase(self) -> None:
        # A single FTS phrase required the words to be adjacent *and* in
        # the same column, so the commonest way to narrow a search
        # returned nothing at all.
        repo = self._store()
        self._seed(repo)
        self.assertEqual(
            self._find(repo, "invoice paid"),
            {"Invoice #3 paid", "Paid the invoice"},
        )

    def test_word_order_does_not_matter(self) -> None:
        repo = self._store()
        self._seed(repo)
        self.assertEqual(
            self._find(repo, "paid invoice"),
            {"Invoice #3 paid", "Paid the invoice"},
        )

    def test_terms_may_match_different_columns_of_one_message(self) -> None:
        # "notes" is in the subject, "invoice" only in the body.
        repo = self._store()
        self._seed(repo)
        self.assertEqual(self._find(repo, "notes invoice"), {"Meeting notes"})

    def test_a_single_word_still_matches_broadly(self) -> None:
        repo = self._store()
        self._seed(repo)
        self.assertEqual(len(self._find(repo, "invoice")), 3)

    def test_repeated_field_prefixes_and_together(self) -> None:
        repo = self._store()
        repo.insert_message(
            message=_make_message("<d@x>", subject="Quarterly report update")
        )
        self.assertEqual(
            self._find(repo, "subject:quarterly subject:report"),
            {"Quarterly report update"},
        )
        self.assertEqual(self._find(repo, "subject:quarterly subject:absent"), set())

    def test_trashed_messages_do_not_appear_in_results(self) -> None:
        # Trashed rows live on until retention reaps them, so the
        # duplicates `pony folder dedup` hid came back in every search.
        repo = self._store()
        repo.insert_message(message=_make_message("<keep@x>", subject="Active secret"))
        repo.insert_message(
            message=_make_message(
                "<gone@x>",
                subject="Trashed secret",
                local_status=MessageStatus.TRASHED,
            )
        )
        self.assertEqual(self._find(repo, "secret"), {"Active secret"})

    def test_a_pending_move_is_still_findable(self) -> None:
        # It is real mail awaiting a push, and the folder list shows it.
        repo = self._store()
        repo.insert_message(
            message=_make_message(
                "<pm@x>",
                subject="Archived thing",
                local_status=MessageStatus.PENDING_MOVE,
            )
        )
        self.assertEqual(self._find(repo, "archived"), {"Archived thing"})


class ChunkedParametersTest(unittest.TestCase):
    """A list longer than one statement's parameters keeps its order.

    The chunker's docstring used to claim the concatenation of its runs
    "is in the same order a single statement would have given", which
    is false for any ORDER BY -- each run is sorted on its own.  Its one
    call site re-orders afterwards, so the claim was wrong rather than
    broken, and the next caller to believe it would have shipped a
    mis-sorted list.
    """

    def test_a_listing_longer_than_one_statement_is_still_sorted(self) -> None:
        repo = _fresh_repo()
        total = 450  # more than _MAX_SQL_PARAMETERS
        with repo.connection():
            for n in range(total):
                repo.upsert_contact(
                    contact=Contact(
                        id=None,
                        first_name="Ada",
                        last_name=f"{n:04d}",
                        emails=(f"ada{n}@example.com",),
                    )
                )

        names = [c.last_name for c in repo.list_all_contacts()]

        self.assertEqual(len(names), total)
        self.assertEqual(names, sorted(names))

    def test_slow_path_rows_come_back_for_every_uid(self) -> None:
        repo = _fresh_repo()
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        uids = list(range(1, 451))
        with repo.connection():
            for uid in uids:
                repo.insert_message(
                    message=dataclasses.replace(
                        _make_message(f"<u{uid}@x>", uid=uid),
                        uid_validity=1,
                    )
                )

        rows = repo.list_folder_slow_path_rows_by_uid(
            account_name=folder.account_name,
            folder_name=folder.folder_name,
            uids=uids,
        )

        self.assertEqual({row.uid for row in rows}, set(uids))


class UtcNormalisationTest(unittest.TestCase):
    """Timestamps are stored in one spelling, so SQL can compare them.

    ``_row_params`` used to store the header's own offset while every
    read normalised to UTC, so a row written straight from a projection
    held ``+02:00`` and the same row read back and rewritten held
    ``+00:00``.  ``find_pending_matches`` compares ``received_at = ?``
    as text, ``purge_expired_trash`` compares ``trashed_at <= cutoff``
    as text, and the folder index sorts ``received_at DESC`` as text.
    """

    folder = FolderRef(account_name="personal", folder_name="INBOX")
    offset = timezone(timedelta(hours=2))

    def test_the_fallback_match_ignores_the_headers_offset(self) -> None:
        repo = _fresh_repo()
        received = datetime(2026, 4, 10, 12, 0, tzinfo=self.offset)
        repo.insert_message(
            message=dataclasses.replace(
                _make_message("<offset@x>", subject="Berlin"),
                received_at=received,
            )
        )
        # What a flag sync does to every UID-bearing row: read it back
        # and write it again.
        (row,) = repo.list_folder_messages(folder=self.folder)
        repo.update_message(message=row)

        for label, probe in (
            ("the header's own offset", received),
            ("the same instant in UTC", received.astimezone(UTC)),
        ):
            with self.subTest(label):
                found = repo.find_pending_matches(
                    folder=self.folder,
                    sender="sender@example.com",
                    subject="Berlin",
                    received_at=probe,
                )
                self.assertEqual(len(found), 1)

    def test_trash_expiry_compares_the_same_spelling(self) -> None:
        # An hour past the retention, written at +02:00: the instant is
        # expired, and the text of it sorts an hour *after* the cutoff's
        # text, which is what the SQL comparison would have gone by.
        repo = _fresh_repo()
        expired = (datetime.now(tz=UTC) - timedelta(days=30, hours=1)).astimezone(
            self.offset
        )
        repo.insert_message(
            message=dataclasses.replace(
                _make_message("<stale@x>", local_status=MessageStatus.TRASHED),
                trashed_at=expired,
            )
        )

        purged = repo.purge_expired_trash(account_name="personal", retention_days=30)

        self.assertEqual(len(purged), 1)

    def test_a_folder_listing_orders_across_offsets(self) -> None:
        repo = _fresh_repo()
        # 11:00+02:00 is 09:00Z — earlier than 10:00Z, whatever the
        # text comparison of the two offsets would say.
        repo.insert_message(
            message=dataclasses.replace(
                _make_message("<east@x>", subject="East"),
                received_at=datetime(2026, 4, 10, 11, 0, tzinfo=self.offset),
            )
        )
        repo.insert_message(
            message=dataclasses.replace(
                _make_message("<utc@x>", subject="UTC"),
                received_at=datetime(2026, 4, 10, 10, 0, tzinfo=UTC),
            )
        )

        rows = repo.list_folder_message_summaries(folder=self.folder)

        self.assertEqual([r.subject for r in rows], ["UTC", "East"])


class OneConnectionPerThreadTest(unittest.TestCase):
    """A thread's transaction is its own, and reaches no other thread.

    Batch state used to be held on the repository, so two threads shared
    one transaction and one depth counter: a write made while a sync's
    batch was open joined that transaction, reported success, and was
    discarded by the sync's rollback.  Each thread now has its own
    connection, so the only work it can commit or roll back is its own
    -- and the bounded commit is what keeps a long rebuild visible to
    the other threads as it goes.
    """

    def setUp(self) -> None:
        self.path = TMP_ROOT / f"one-conn-{uuid4().hex}" / "index.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.repo = SqliteIndexRepository(database_path=self.path)
        self.repo.initialize()
        self.addCleanup(self.repo.close)

    def _insert(self, name: str) -> None:
        self.repo.insert_message(message=_make_message(name, subject=name))

    def test_a_reader_sees_a_batch_once_it_commits(self) -> None:
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        during: list[int] = []

        def count() -> None:
            during.append(len(self.repo.list_folder_messages(folder=folder)))

        with self.repo.connection():
            self._insert("during the batch")
            reader = threading.Thread(target=count)
            reader.start()
            reader.join(timeout=10)

        after = len(self.repo.list_folder_messages(folder=folder))
        # Mid-batch the other thread has its own connection and its own
        # snapshot, so it does not see the uncommitted row.  What keeps a
        # rebuild visible is the bounded commit, not a shared
        # transaction -- see test_a_long_batch_commits_as_it_goes.
        self.assertEqual(during, [0])
        self.assertEqual(after, 1)

    def test_one_thread_cannot_roll_back_another_threads_write(self) -> None:
        """The defect this design exists to remove, in its own terms."""
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        batch_open = threading.Event()
        failed: list[BaseException] = []

        def writer() -> None:
            # A plain single write, with no batch of its own, while the
            # other thread holds one open.  It waits for that thread's
            # transaction rather than joining it.
            batch_open.wait(timeout=10)
            try:
                self._insert("thread B")
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                failed.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        with contextlib.suppress(RuntimeError), self.repo.connection():
            self._insert("thread A")
            batch_open.set()
            # Long enough for the other thread to be inside its write.
            time.sleep(0.2)
            raise RuntimeError("thread A's folder failed")
        thread.join(timeout=10)

        self.assertEqual(failed, [])
        subjects = {m.subject for m in self.repo.list_folder_messages(folder=folder)}
        # B's write returned normally, so it has to still be there; A's
        # rollback may only take A's own row with it.
        self.assertEqual(subjects, {"thread B"})

    def test_a_failed_statement_keeps_the_rest_of_its_batch(self) -> None:
        """A sync logs a failed operation and carries on with the next."""
        folder = FolderRef(account_name="personal", folder_name="INBOX")

        with self.repo.connection():
            self._insert("first")
            self.repo.insert_message(
                message=_make_message("<u7@x>", subject="holds uid 7", uid=7),
            )
            with contextlib.suppress(sqlite3.IntegrityError):
                # One operation fails; the batch is not what failed, and
                # up to _BATCH_COMMIT_ROWS rows of applied work must not
                # go down with it.
                self.repo.insert_message(
                    message=_make_message("<dup@x>", subject="uid 7 again", uid=7),
                )
            self._insert("second")

        subjects = {m.subject for m in self.repo.list_folder_messages(folder=folder)}
        self.assertEqual(subjects, {"first", "holds uid 7", "second"})

    def test_batches_on_two_threads_queue_instead_of_colliding(self) -> None:
        """Two batches are two transactions, taken one after the other.

        A shared depth counter made the second entrant a no-op inside
        the first one's transaction, and an out-of-order exit left the
        counter at ``-1``, after which no block committed or rolled
        back for the life of the process.
        """
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        start = threading.Barrier(3, timeout=10)
        errors: list[BaseException] = []

        def batch(name: str) -> None:
            try:
                start.wait()
                with self.repo.connection():
                    self._insert(f"{name} outer")
                    with self.repo.connection():
                        self._insert(f"{name} inner")
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                errors.append(exc)

        threads = [threading.Thread(target=batch, args=(n,)) for n in ("one", "two")]
        for thread in threads:
            thread.start()
        start.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        subjects = {m.subject for m in self.repo.list_folder_messages(folder=folder)}
        self.assertEqual(
            subjects,
            {"one outer", "one inner", "two outer", "two inner"},
        )
        # And a batch opened afterwards still commits.
        with self.repo.connection():
            self._insert("afterwards")
        self.assertEqual(len(self.repo.list_folder_messages(folder=folder)), 5)

    def test_reads_do_not_count_toward_the_commit_interval(self) -> None:
        """ "Commit every N writes" used to mean "every N calls"."""
        folder = FolderRef(account_name="personal", folder_name="INBOX")

        with contextlib.suppress(RuntimeError), self.repo.connection():
            self._insert("the only write")
            for _ in range(_BATCH_COMMIT_ROWS + 10):
                self.repo.list_folder_messages(folder=folder)
            raise RuntimeError("killed")

        # Nothing committed along the way, so the rollback takes the one
        # write with it.
        self.assertEqual(self.repo.list_folder_messages(folder=folder), ())

    def test_close_is_safe_while_another_thread_holds_a_batch(self) -> None:
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        in_batch = threading.Event()
        closed = threading.Event()
        errors: list[BaseException] = []

        def batch() -> None:
            try:
                with self.repo.connection():
                    self._insert("mid-batch")
                    in_batch.set()
                    closed.wait(timeout=10)
                    self._insert("after the close")
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                errors.append(exc)

        thread = threading.Thread(target=batch)
        thread.start()
        self.assertTrue(in_batch.wait(timeout=10))
        self.repo.close()
        closed.set()
        thread.join(timeout=10)

        self.assertEqual(errors, [])
        subjects = {m.subject for m in self.repo.list_folder_messages(folder=folder)}
        self.assertEqual(subjects, {"mid-batch", "after the close"})

    def test_a_long_batch_commits_as_it_goes(self) -> None:
        """A killed rebuild keeps what it had done, minus the last batch."""
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        written = _BATCH_COMMIT_ROWS + 10

        with contextlib.suppress(RuntimeError), self.repo.connection():
            for n in range(written):
                self._insert(f"message {n}")
            raise RuntimeError("killed")

        # Everything up to the last commit point survived; only the
        # trailing partial batch was rolled back.
        kept = len(self.repo.list_folder_messages(folder=folder))
        self.assertEqual(kept, _BATCH_COMMIT_ROWS)

    def test_a_nested_block_does_not_end_the_batch(self) -> None:
        folder = FolderRef(account_name="personal", folder_name="INBOX")

        with contextlib.suppress(RuntimeError), self.repo.connection():
            with self.repo.connection():
                self._insert("inner")
            raise RuntimeError("abandoned")

        self.assertEqual(len(self.repo.list_folder_messages(folder=folder)), 0)

    def test_closing_commits_and_releases_the_file(self) -> None:
        self._insert("kept")
        self.repo.close()

        reopened = SqliteIndexRepository(database_path=self.path)
        self.addCleanup(reopened.close)
        rows = reopened.list_folder_messages(
            folder=FolderRef(account_name="personal", folder_name="INBOX"),
        )
        self.assertEqual(len(rows), 1)


class SearchFolderScopeTest(unittest.TestCase):
    """A folder-scoped search must be scoped in the query.

    Both front ends asked for the account's hits and dropped the other
    folders afterwards, which put the filter on the wrong side of
    `limit`: one matching message in INBOX behind 500 matching ones in
    Archive came back as "nothing found" in the browser and an empty
    list in the terminal.
    """

    def setUp(self) -> None:
        self.index = _fresh_repo()

    def _message(self, *, folder: str, uid: int, when: datetime) -> None:
        message = _make_message(
            f"{folder}-{uid}",
            folder_name=folder,
            uid=uid,
            subject="needle in here",
        )
        self.index.insert_message(
            message=dataclasses.replace(message, received_at=when)
        )

    def test_a_scoped_search_reaches_past_the_limit(self) -> None:
        base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        # The one INBOX hit is the oldest, so an unscoped search ordered
        # by date puts it behind every Archive hit.
        self._message(folder="INBOX", uid=1, when=base)
        for n in range(520):
            self._message(
                folder="Archive",
                uid=100 + n,
                when=base + timedelta(minutes=n + 1),
            )

        query = parse_query("needle")
        unscoped = self.index.search(query=query, account_name="personal")
        scoped = self.index.search(
            query=query, account_name="personal", folder_name="INBOX"
        )

        # What the old code did: ask for the account, filter afterwards.
        self.assertEqual(
            [],
            [m for m in unscoped if m.message_ref.folder_name == "INBOX"],
        )
        # What it does now.
        self.assertEqual(1, len(scoped))
        self.assertEqual("INBOX", scoped[0].message_ref.folder_name)
