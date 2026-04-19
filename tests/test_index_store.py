"""Tests for the SQLite index repository."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import (
    FolderRef,
    FolderSyncState,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
    OperationType,
    PendingOperation,
    SearchQuery,
)
from pony.index_store import SqliteIndexRepository


class UpsertAndListTestCase(unittest.TestCase):
    """Validate basic insert, upsert idempotence, and folder listing."""

    def test_upsert_and_list_folder_messages(self) -> None:
        repo = _fresh_repo()
        folder = FolderRef(account_name="personal", folder_name="INBOX")

        repo.upsert_message(message=_make_message("m-1", subject="Alpha"))
        repo.upsert_message(message=_make_message("m-2", subject="Beta"))

        rows = repo.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 2)
        ids = {r.message_ref.rfc5322_id for r in rows}
        self.assertEqual(ids, {"m-1", "m-2"})

    def test_upsert_is_idempotent(self) -> None:
        repo = _fresh_repo()
        folder = FolderRef(account_name="personal", folder_name="INBOX")

        repo.upsert_message(message=_make_message("m-1", subject="Original"))
        repo.upsert_message(message=_make_message("m-1", subject="Updated"))

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
        repo.upsert_message(message=_make_message("m-1"))

        ref = MessageRef(account_name="personal", folder_name="INBOX", rfc5322_id="m-1")
        repo.delete_message(message_ref=ref)

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        self.assertEqual(repo.list_folder_messages(folder=folder), ())

    def test_delete_nonexistent_is_silent(self) -> None:
        repo = _fresh_repo()
        ref = MessageRef(
            account_name="personal", folder_name="INBOX", rfc5322_id="ghost"
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
        ids = {h.message_ref.rfc5322_id for h in hits}
        self.assertEqual(ids, {"m-1"})

    def test_to_address_filter(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(to_address="alice"), account_name="personal"
        )
        ids = {h.message_ref.rfc5322_id for h in hits}
        self.assertEqual(ids, {"m-2"})

    def test_cc_address_filter(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(cc_address="carol"), account_name="personal"
        )
        ids = {h.message_ref.rfc5322_id for h in hits}
        self.assertEqual(ids, {"m-1"})

    def test_subject_filter(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(subject="Re:"), account_name="personal"
        )
        ids = {h.message_ref.rfc5322_id for h in hits}
        self.assertEqual(ids, {"m-2"})

    def test_body_filter(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(body="looks good"), account_name="personal"
        )
        ids = {h.message_ref.rfc5322_id for h in hits}
        self.assertEqual(ids, {"m-2"})

    def test_combined_filters_narrow_results(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(from_address="alice", subject="Quarterly"),
            account_name="personal",
        )
        ids = {h.message_ref.rfc5322_id for h in hits}
        self.assertEqual(ids, {"m-1"})

    def test_case_insensitive_by_default(self) -> None:
        hits = self.repo.search(
            query=SearchQuery(subject="quarterly report"), account_name="personal"
        )
        self.assertEqual(len(hits), 2)

    def test_case_sensitive_flag_is_ignored(self) -> None:
        # The legacy ``case_sensitive`` flag on SearchQuery is accepted
        # for backwards compatibility but has no effect — FTS5's
        # ``unicode61`` tokenizer folds case (and diacritics)
        # unconditionally.  Both values must return the same results.
        hits_on = self.repo.search(
            query=SearchQuery(subject="quarterly report", case_sensitive=True),
            account_name="personal",
        )
        hits_off = self.repo.search(
            query=SearchQuery(subject="quarterly report", case_sensitive=False),
            account_name="personal",
        )
        self.assertEqual(len(hits_on), 2)
        self.assertEqual(len(hits_off), 2)

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
        ids = {h.message_ref.rfc5322_id for h in hits}
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
        ids = {h.message_ref.rfc5322_id for h in hits}
        self.assertNotIn("m-3", ids)


class DiacriticAndCaseFoldingSearchTestCase(unittest.TestCase):
    """FTS5 ``unicode61`` tokenizer folds case + diacritics everywhere."""

    def test_ascii_query_matches_accented_subject(self) -> None:
        repo = _fresh_repo()
        repo.upsert_message(
            message=_make_message(
                "m-1", subject="Carta de María",
                body_preview="contenido cualquiera",
            ),
        )
        hits = repo.search(
            query=SearchQuery(subject="maria"), account_name="personal",
        )
        self.assertEqual(
            {h.message_ref.rfc5322_id for h in hits}, {"m-1"},
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
            {h.message_ref.rfc5322_id for h in hits}, {"m-1"},
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
            query=SearchQuery(subject="foo"), account_name="personal",
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


class PendingOperationsTestCase(unittest.TestCase):
    """Validate pending operation enqueue/list/complete lifecycle."""

    def test_enqueue_and_list(self) -> None:
        repo = _fresh_repo()
        op = _make_operation("op-1", OperationType.DELETE)
        repo.enqueue_operation(operation=op)

        pending = repo.list_pending_operations(account_name="personal")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].operation_type, OperationType.DELETE)

    def test_complete_removes_operation(self) -> None:
        repo = _fresh_repo()
        repo.enqueue_operation(operation=_make_operation("op-1", OperationType.DELETE))
        repo.enqueue_operation(
            operation=_make_operation("op-2", OperationType.MARK_READ)
        )

        repo.complete_operation(operation_id="op-1")
        pending = repo.list_pending_operations(account_name="personal")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].operation_id, "op-2")

    def test_complete_nonexistent_is_silent(self) -> None:
        repo = _fresh_repo()
        repo.complete_operation(operation_id="ghost")  # must not raise

    def test_list_isolates_accounts(self) -> None:
        repo = _fresh_repo()
        repo.enqueue_operation(
            operation=_make_operation(
                "op-1", OperationType.DELETE, account_name="personal"
            )
        )
        repo.enqueue_operation(
            operation=_make_operation("op-2", OperationType.FLAG, account_name="work")
        )
        personal = repo.list_pending_operations(account_name="personal")
        work = repo.list_pending_operations(account_name="work")
        self.assertEqual(len(personal), 1)
        self.assertEqual(len(work), 1)

    def test_all_operation_types_roundtrip(self) -> None:
        repo = _fresh_repo()
        for i, op_type in enumerate(OperationType):
            repo.enqueue_operation(operation=_make_operation(f"op-{i}", op_type))
        pending = repo.list_pending_operations(account_name="personal")
        types = {p.operation_type for p in pending}
        self.assertEqual(types, set(OperationType))


class SchemaVersionGateTestCase(unittest.TestCase):
    """initialize() refuses to open a DB older than the current schema."""

    def test_initialize_refuses_legacy_db(self) -> None:
        import sqlite3

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
        with self.assertRaises(SystemExit) as ctx:
            repo.initialize()
        self.assertIn(str(path), str(ctx.exception))

    def test_initialize_creates_fresh_db_at_current_version(self) -> None:
        import sqlite3

        repo = _fresh_repo()
        conn = sqlite3.connect(repo._database_path)  # noqa: SLF001
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
        self.assertGreaterEqual(version, 2)


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
        self.assertEqual(rows[0].message_ref.rfc5322_id, "m-0")

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
) -> IndexedMessage:
    return IndexedMessage(
        message_ref=MessageRef(
            account_name=account_name,
            folder_name=folder_name,
            rfc5322_id=message_id,
        ),
        sender=sender,
        recipients=recipients,
        cc=cc,
        subject=subject,
        body_preview=body_preview,
        storage_key=message_id,
        local_flags=local_flags,
        base_flags=base_flags,
        local_status=local_status,
        received_at=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
    )


def _make_operation(
    operation_id: str,
    operation_type: OperationType,
    *,
    account_name: str = "personal",
) -> PendingOperation:
    return PendingOperation(
        operation_id=operation_id,
        account_name=account_name,
        message_ref=MessageRef(
            account_name=account_name,
            folder_name="INBOX",
            rfc5322_id="m-1",
        ),
        operation_type=operation_type,
    )
