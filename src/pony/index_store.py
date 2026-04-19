"""SQLite-backed metadata index."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Generator, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from pathlib import Path

from .domain import (
    Contact,
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
from .protocols import ContactRepository, IndexRepository

# Bumped when the schema, FTS tables, or triggers change in a way that
# existing databases cannot satisfy without a rebuild.  initialize()
# refuses to open any DB whose user_version is lower than this.
_SCHEMA_VERSION = 2


class SqliteIndexRepository(IndexRepository, ContactRepository):
    """Persist indexed metadata and sync state in SQLite.

    By default every public method opens its own connection, executes one
    statement, commits, and closes.  For bulk operations (e.g. syncing a
    whole folder) callers can wrap a block of calls in::

        with repo.connection():
            repo.upsert_message(...)
            repo.upsert_message(...)
            ...

    All calls inside the block reuse a single connection and commit once
    at the end.  On exception the transaction is rolled back.  Nesting is
    safe — only the outermost ``connection()`` block commits.
    """

    def __init__(self, *, database_path: Path) -> None:
        self._database_path = database_path
        self._local = threading.local()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _open_connection(self) -> sqlite3.Connection:
        """Open a fresh connection with WAL mode and a busy timeout."""
        try:
            conn = sqlite3.connect(self._database_path, timeout=10)
        except sqlite3.OperationalError:
            # Stale lock — remove journal files and retry.
            for suffix in ("-wal", "-shm", "-journal"):
                p = self._database_path.parent / (
                    self._database_path.name + suffix
                )
                p.unlink(missing_ok=True)
            conn = sqlite3.connect(self._database_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextlib.contextmanager
    def connection(self) -> Generator[None]:
        """Hold a single connection for a batch of operations.

        All repository methods called inside this block reuse the same
        connection and skip per-call commits.  The transaction is committed
        when the block exits cleanly, or rolled back on exception.

        Reentrant: nested ``connection()`` blocks are no-ops — only the
        outermost block owns the connection lifecycle.
        """
        depth: int = getattr(self._local, "depth", 0)
        if depth > 0:
            # Nested — reuse the outer connection.
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return

        conn = self._open_connection()
        self._local.conn = conn
        self._local.depth = 1
        try:
            yield
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._local.depth = 0
            self._local.conn = None
            conn.close()

    def _conn(self) -> tuple[sqlite3.Connection, bool]:
        """Return ``(connection, is_managed)``.

        When inside a ``connection()`` block, returns the shared connection
        with ``is_managed=True`` (caller must **not** commit or close).
        Otherwise opens a fresh connection with ``is_managed=False``
        (caller must commit and close via the ``with`` statement).
        """
        active: sqlite3.Connection | None = getattr(
            self._local, "conn", None,
        )
        if active is not None:
            return active, True
        return self._open_connection(), False

    def _done(
        self, conn: sqlite3.Connection, managed: bool,
    ) -> None:
        """Commit and close *conn* if it is **not** managed."""
        if not managed:
            conn.commit()
            conn.close()

    @contextlib.contextmanager
    def _use(self) -> Generator[sqlite3.Connection]:
        """Convenience wrapper: yield a connection, auto-commit if unmanaged.

        Use this as ``with self._use() as conn:`` in every public method.
        Inside an outer ``connection()`` block it reuses the shared
        connection and skips commit.  Otherwise it behaves like the old
        ``with self._use() as conn:`` pattern.
        """
        conn, managed = self._conn()
        try:
            yield conn
        except BaseException:
            if not managed:
                conn.rollback()
            raise
        finally:
            self._done(conn, managed)

    def _connect(self) -> sqlite3.Connection:
        """Legacy alias — delegates to ``_open_connection``.

        Kept for ``initialize()`` which must always use its own connection.
        """
        return self._open_connection()

    def initialize(self) -> None:
        """Create required tables, or refuse to open an out-of-date DB."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._use() as conn:
            version_row = conn.execute("PRAGMA user_version").fetchone()
            version = int(version_row[0]) if version_row is not None else 0
            has_legacy_tables = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='messages'"
            ).fetchone() is not None

            if version < _SCHEMA_VERSION and has_legacy_tables:
                raise SystemExit(
                    "Pony's index schema has changed. The safest path right "
                    "now is to delete "
                    f"{self._database_path} and the mirror directory, then "
                    "run `pony sync` to redownload. A rebuild-from-mirror "
                    "command is planned but not yet available."
                )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
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
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_uid
                ON messages (account_name, folder_name, uid)
                WHERE uid IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS folder_sync_state (
                    account_name  TEXT    NOT NULL,
                    folder_name   TEXT    NOT NULL,
                    uid_validity  INTEGER NOT NULL,
                    highest_uid   INTEGER NOT NULL,
                    synced_at     TEXT    NOT NULL,
                    PRIMARY KEY (account_name, folder_name)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_operations (
                    operation_id   TEXT PRIMARY KEY,
                    account_name   TEXT NOT NULL,
                    folder_name    TEXT NOT NULL,
                    message_id     TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    created_at     TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    id             INTEGER PRIMARY KEY,
                    first_name     TEXT NOT NULL DEFAULT '',
                    last_name      TEXT NOT NULL DEFAULT '',
                    affix          TEXT NOT NULL DEFAULT '[]',
                    organization   TEXT NOT NULL DEFAULT '',
                    notes          TEXT NOT NULL DEFAULT '',
                    message_count  INTEGER NOT NULL DEFAULT 0,
                    last_seen      TEXT,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_emails (
                    contact_id    INTEGER NOT NULL REFERENCES contacts(id)
                                  ON DELETE CASCADE,
                    email_address TEXT NOT NULL,
                    PRIMARY KEY (email_address)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_aliases (
                    contact_id INTEGER NOT NULL REFERENCES contacts(id)
                               ON DELETE CASCADE,
                    alias      TEXT NOT NULL,
                    PRIMARY KEY (contact_id, alias)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    account_name TEXT PRIMARY KEY NOT NULL,
                    encrypted    BLOB NOT NULL
                )
                """
            )
            _create_fts_tables(conn)
            _create_fts_triggers(conn)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def upsert_message(self, *, message: IndexedMessage) -> None:
        """Store or replace one message metadata row and harvest its addresses."""
        trashed_at_str = (
            message.trashed_at.isoformat() if message.trashed_at else None
        )
        synced_at_str = (
            message.synced_at.isoformat() if message.synced_at else None
        )
        with self._use() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    account_name, folder_name, message_id,
                    sender, recipients, cc, subject, body_preview,
                    storage_key, has_attachments,
                    local_flags, base_flags, local_status, received_at,
                    uid, server_flags, extra_imap_flags,
                    trashed_at, synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_name, folder_name, message_id)
                DO UPDATE SET
                    sender           = excluded.sender,
                    recipients       = excluded.recipients,
                    cc               = excluded.cc,
                    subject          = excluded.subject,
                    body_preview     = excluded.body_preview,
                    storage_key      = excluded.storage_key,
                    has_attachments  = excluded.has_attachments,
                    local_flags      = excluded.local_flags,
                    base_flags       = excluded.base_flags,
                    local_status     = excluded.local_status,
                    received_at      = excluded.received_at,
                    uid              = excluded.uid,
                    server_flags     = excluded.server_flags,
                    extra_imap_flags = excluded.extra_imap_flags,
                    trashed_at       = excluded.trashed_at,
                    synced_at        = excluded.synced_at
                """,
                (
                    message.message_ref.account_name,
                    message.message_ref.folder_name,
                    message.message_ref.rfc5322_id,
                    message.sender,
                    message.recipients,
                    message.cc,
                    message.subject,
                    message.body_preview,
                    message.storage_key,
                    int(message.has_attachments),
                    _flags_to_csv(message.local_flags),
                    _flags_to_csv(message.base_flags),
                    message.local_status.value,
                    message.received_at.isoformat(),
                    message.uid,
                    _flags_to_csv(message.server_flags),
                    ",".join(sorted(message.extra_imap_flags)),
                    trashed_at_str,
                    synced_at_str,
                ),
            )

    def delete_message(self, *, message_ref: MessageRef) -> None:
        """Remove one message row from the index."""
        with self._use() as conn:
            conn.execute(
                """
                DELETE FROM messages
                WHERE account_name = ? AND folder_name = ? AND message_id = ?
                """,
                (
                    message_ref.account_name,
                    message_ref.folder_name,
                    message_ref.rfc5322_id,
                ),
            )

    def purge_expired_trash(
        self, *, account_name: str, retention_days: int
    ) -> list[tuple[FolderRef, str]]:
        """Delete trashed messages older than *retention_days*.

        Returns ``[(folder_ref, storage_key), ...]`` for each purged row so
        the caller can clean up the corresponding mirror files.
        """
        cutoff = (
            datetime.now(tz=UTC) - timedelta(days=retention_days)
        ).isoformat()
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT account_name, folder_name, storage_key
                FROM messages
                WHERE account_name = ?
                  AND local_status = ?
                  AND trashed_at IS NOT NULL
                  AND trashed_at <= ?
                """,
                (account_name, MessageStatus.TRASHED.value, cutoff),
            ).fetchall()
            entries = [
                (
                    FolderRef(
                        account_name=str(r[0]), folder_name=str(r[1]),
                    ),
                    str(r[2]),
                )
                for r in rows
            ]
            if entries:
                conn.execute(
                    """
                    DELETE FROM messages
                    WHERE account_name = ?
                      AND local_status = ?
                      AND trashed_at IS NOT NULL
                      AND trashed_at <= ?
                    """,
                    (account_name, MessageStatus.TRASHED.value, cutoff),
                )
        return entries

    def list_indexed_accounts(self) -> list[str]:
        """Return all distinct account names from the messages table."""
        if not self._database_path.exists():
            return []
        with self._use() as conn:
            rows = conn.execute(
                "SELECT DISTINCT account_name FROM messages"
            ).fetchall()
        return [str(r[0]) for r in rows]

    def purge_account(self, *, account_name: str) -> None:
        """Remove all data for one account from every table."""
        with self._use() as conn:
            for table in (
                "messages",
                "folder_sync_state",
                "pending_operations",
            ):
                conn.execute(
                    f"DELETE FROM {table} WHERE account_name = ?",  # noqa: S608
                    (account_name,),
                )

    def purge_stale_folders(
        self,
        *,
        account_name: str,
        active_folders: frozenset[str],
    ) -> list[str]:
        """Remove sync/server state for folders not in *active_folders*."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT folder_name FROM folder_sync_state
                WHERE account_name = ?
                """,
                (account_name,),
            ).fetchall()
            stale = [
                str(r[0]) for r in rows if str(r[0]) not in active_folders
            ]
            for folder_name in stale:
                conn.execute(
                    """
                    DELETE FROM folder_sync_state
                    WHERE account_name = ? AND folder_name = ?
                    """,
                    (account_name, folder_name),
                )
        return stale

    def get_message(self, *, message_ref: MessageRef) -> IndexedMessage | None:
        """Return one indexed message by primary key, or None."""
        with self._use() as conn:
            row = conn.execute(
                """
                SELECT
                    account_name, folder_name, message_id,
                    sender, recipients, cc, subject, body_preview,
                    storage_key, has_attachments,
                    local_flags, base_flags, local_status, received_at,
                    uid, server_flags, extra_imap_flags,
                    trashed_at, synced_at
                FROM messages
                WHERE account_name = ? AND folder_name = ? AND message_id = ?
                """,
                (
                    message_ref.account_name,
                    message_ref.folder_name,
                    message_ref.rfc5322_id,
                ),
            ).fetchone()
        if row is None:
            return None
        return _indexed_message_from_row(row)

    def count_folder_messages(self, *, folder: FolderRef) -> int:
        """Return the number of indexed messages in a folder."""
        with self._use() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE account_name = ? AND folder_name = ?
                """,
                (folder.account_name, folder.folder_name),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_folder_messages(self, *, folder: FolderRef) -> Sequence[IndexedMessage]:
        """Return indexed messages for a folder ordered by received date."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT
                    account_name, folder_name, message_id,
                    sender, recipients, cc, subject, body_preview,
                    storage_key, has_attachments,
                    local_flags, base_flags, local_status, received_at,
                    uid, server_flags, extra_imap_flags,
                    trashed_at, synced_at
                FROM messages
                WHERE account_name = ? AND folder_name = ?
                ORDER BY received_at DESC
                """,
                (folder.account_name, folder.folder_name),
            ).fetchall()
        return tuple(_indexed_message_from_row(row) for row in rows)

    def search(
        self, *, query: SearchQuery, account_name: str | None
    ) -> Sequence[IndexedMessage]:
        """Run an FTS5-backed metadata search.

        Folding is always on: ``case_sensitive`` on *query* is accepted
        for backwards-compatibility but ignored — the FTS5 ``unicode61``
        tokenizer is case- and diacritic-insensitive by construction.
        """
        match_expr = _build_fts_match(query)
        clauses: list[str] = []
        params: list[object] = []

        if match_expr:
            clauses.append("messages_fts MATCH ?")
            params.append(match_expr)
        if account_name is not None:
            clauses.append("m.account_name = ?")
            params.append(account_name)
        where_sql = " AND ".join(clauses) if clauses else "1=1"
        join_sql = (
            "JOIN messages_fts f ON f.rowid = m.rowid" if match_expr else ""
        )

        with self._use() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    m.account_name, m.folder_name, m.message_id,
                    m.sender, m.recipients, m.cc, m.subject, m.body_preview,
                    m.storage_key, m.has_attachments,
                    m.local_flags, m.base_flags, m.local_status, m.received_at,
                    m.uid, m.server_flags, m.extra_imap_flags,
                    m.trashed_at, m.synced_at
                FROM messages m
                {join_sql}
                WHERE {where_sql}
                ORDER BY m.received_at DESC
                """,  # noqa: S608
                params,
            ).fetchall()

        return tuple(_indexed_message_from_row(row) for row in rows)

    # ------------------------------------------------------------------
    # Folder sync state
    # ------------------------------------------------------------------

    def record_folder_sync_state(self, *, state: FolderSyncState) -> None:
        """Store one folder sync watermark."""
        with self._use() as conn:
            conn.execute(
                """
                INSERT INTO folder_sync_state (
                    account_name, folder_name, uid_validity, highest_uid, synced_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_name, folder_name)
                DO UPDATE SET
                    uid_validity = excluded.uid_validity,
                    highest_uid  = excluded.highest_uid,
                    synced_at    = excluded.synced_at
                """,
                (
                    state.account_name,
                    state.folder_name,
                    state.uid_validity,
                    state.highest_uid,
                    state.synced_at.isoformat(),
                ),
            )

    def get_folder_sync_state(
        self, *, account_name: str, folder_name: str
    ) -> FolderSyncState | None:
        """Load the sync watermark for one folder."""
        with self._use() as conn:
            row = conn.execute(
                """
                SELECT account_name, folder_name, uid_validity, highest_uid, synced_at
                FROM folder_sync_state
                WHERE account_name = ? AND folder_name = ?
                """,
                (account_name, folder_name),
            ).fetchone()
        if row is None:
            return None
        return FolderSyncState(
            account_name=str(row[0]),
            folder_name=str(row[1]),
            uid_validity=int(row[2]),
            highest_uid=int(row[3]),
            synced_at=datetime.fromisoformat(str(row[4])),
        )

    def list_folder_sync_states(
        self, *, account_name: str
    ) -> list[FolderSyncState]:
        """Return all sync watermarks for one account."""
        if not self._database_path.exists():
            return []
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT account_name, folder_name, uid_validity, highest_uid, synced_at
                FROM folder_sync_state
                WHERE account_name = ?
                """,
                (account_name,),
            ).fetchall()
        return [
            FolderSyncState(
                account_name=str(r[0]),
                folder_name=str(r[1]),
                uid_validity=int(r[2]),
                highest_uid=int(r[3]),
                synced_at=datetime.fromisoformat(str(r[4])),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # UID / server-state queries (unified in the messages table)
    # ------------------------------------------------------------------

    def list_folder_messages_with_uid(
        self, *, account_name: str, folder_name: str
    ) -> Sequence[IndexedMessage]:
        """Return messages that have a non-NULL uid for one folder."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT
                    account_name, folder_name, message_id,
                    sender, recipients, cc, subject, body_preview,
                    storage_key, has_attachments,
                    local_flags, base_flags, local_status, received_at,
                    uid, server_flags, extra_imap_flags,
                    trashed_at, synced_at
                FROM messages
                WHERE account_name = ? AND folder_name = ? AND uid IS NOT NULL
                """,
                (account_name, folder_name),
            ).fetchall()
        return tuple(_indexed_message_from_row(row) for row in rows)

    def list_all_uids(
        self, *, account_name: str
    ) -> Sequence[IndexedMessage]:
        """Return all messages with a non-NULL uid for one account."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT
                    account_name, folder_name, message_id,
                    sender, recipients, cc, subject, body_preview,
                    storage_key, has_attachments,
                    local_flags, base_flags, local_status, received_at,
                    uid, server_flags, extra_imap_flags,
                    trashed_at, synced_at
                FROM messages
                WHERE account_name = ? AND uid IS NOT NULL
                """,
                (account_name,),
            ).fetchall()
        return tuple(_indexed_message_from_row(row) for row in rows)

    def list_pending_rows(
        self, *, account_name: str
    ) -> Sequence[IndexedMessage]:
        """Return ACTIVE rows with ``uid IS NULL`` for one account."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT
                    account_name, folder_name, message_id,
                    sender, recipients, cc, subject, body_preview,
                    storage_key, has_attachments,
                    local_flags, base_flags, local_status, received_at,
                    uid, server_flags, extra_imap_flags,
                    trashed_at, synced_at
                FROM messages
                WHERE account_name = ?
                  AND uid IS NULL
                  AND local_status = ?
                """,
                (account_name, MessageStatus.ACTIVE.value),
            ).fetchall()
        return tuple(_indexed_message_from_row(row) for row in rows)

    def clear_uids_for_folder(
        self, *, account_name: str, folder_name: str
    ) -> None:
        """NULL out uid and server-state columns for a folder (UIDVALIDITY reset)."""
        with self._use() as conn:
            conn.execute(
                """
                UPDATE messages
                SET uid = NULL, server_flags = '', extra_imap_flags = '',
                    synced_at = NULL
                WHERE account_name = ? AND folder_name = ?
                """,
                (account_name, folder_name),
            )

    # ------------------------------------------------------------------
    # Pending operations
    # ------------------------------------------------------------------

    def enqueue_operation(self, *, operation: PendingOperation) -> None:
        """Store one pending operation."""
        with self._use() as conn:
            conn.execute(
                """
                INSERT INTO pending_operations (
                    operation_id, account_name, folder_name,
                    message_id, operation_type, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id)
                DO UPDATE SET
                    account_name   = excluded.account_name,
                    folder_name    = excluded.folder_name,
                    message_id     = excluded.message_id,
                    operation_type = excluded.operation_type,
                    created_at     = excluded.created_at
                """,
                (
                    operation.operation_id,
                    operation.account_name,
                    operation.message_ref.folder_name,
                    operation.message_ref.rfc5322_id,
                    operation.operation_type.value,
                    operation.created_at.isoformat(),
                ),
            )

    def complete_operation(self, *, operation_id: str) -> None:
        """Delete one pending operation row by ID."""
        with self._use() as conn:
            conn.execute(
                "DELETE FROM pending_operations WHERE operation_id = ?",
                (operation_id,),
            )

    def list_pending_operations(
        self, *, account_name: str
    ) -> Sequence[PendingOperation]:
        """List pending operations for one account."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT operation_id, account_name, folder_name,
                       message_id, operation_type, created_at
                FROM pending_operations
                WHERE account_name = ?
                ORDER BY created_at ASC
                """,
                (account_name,),
            ).fetchall()

        return tuple(
            PendingOperation(
                operation_id=str(row[0]),
                account_name=str(row[1]),
                message_ref=MessageRef(
                    account_name=str(row[1]),
                    folder_name=str(row[2]),
                    rfc5322_id=str(row[3]),
                ),
                operation_type=OperationType(row[4]),
                created_at=datetime.fromisoformat(str(row[5])),
            )
            for row in rows
        )


    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def upsert_contact(self, *, contact: Contact) -> Contact:
        """Insert or update a contact record.  Returns the saved contact."""
        now = datetime.now(tz=UTC)
        updated_at = contact.updated_at.isoformat()
        with self._use() as conn:
            if contact.id is not None:
                conn.execute(
                    """
                    UPDATE contacts SET
                        first_name = ?, last_name = ?, affix = ?,
                        organization = ?, notes = ?, message_count = ?,
                        last_seen = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        contact.first_name, contact.last_name,
                        json.dumps(list(contact.affix)),
                        contact.organization, contact.notes,
                        contact.message_count,
                        contact.last_seen.isoformat() if contact.last_seen else None,
                        now.isoformat(), contact.id,
                    ),
                )
                contact_id = contact.id
            else:
                created_at = contact.created_at.isoformat()
                cur = conn.execute(
                    """
                    INSERT INTO contacts (
                        first_name, last_name, affix, organization, notes,
                        message_count, last_seen, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contact.first_name, contact.last_name,
                        json.dumps(list(contact.affix)),
                        contact.organization, contact.notes,
                        contact.message_count,
                        contact.last_seen.isoformat() if contact.last_seen else None,
                        created_at, updated_at,
                    ),
                )
                contact_id = cur.lastrowid or 0
            # Replace emails.
            conn.execute(
                "DELETE FROM contact_emails WHERE contact_id = ?",
                (contact_id,),
            )
            for email in contact.emails:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO contact_emails
                        (contact_id, email_address)
                    VALUES (?, ?)
                    """,
                    (contact_id, email.lower().strip()),
                )
            # Replace aliases.
            conn.execute(
                "DELETE FROM contact_aliases WHERE contact_id = ?",
                (contact_id,),
            )
            for alias in contact.aliases:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO contact_aliases
                        (contact_id, alias)
                    VALUES (?, ?)
                    """,
                    (contact_id, alias.strip()),
                )
        return self._load_contact(contact_id)

    def find_contact_by_email(self, *, email_address: str) -> Contact | None:
        """Look up a contact by one of its email addresses."""
        addr = email_address.lower().strip()
        with self._use() as conn:
            row = conn.execute(
                "SELECT contact_id FROM contact_emails WHERE email_address = ?",
                (addr,),
            ).fetchone()
        if row is None:
            return None
        return self._load_contact(int(row[0]))

    def search_contacts(self, *, prefix: str, limit: int = 10) -> list[Contact]:
        """Search contacts by name, alias, or email address (prefix match).

        Folding is always on (case + diacritics) via the FTS5
        ``unicode61`` tokenizer; a trailing ``*`` makes the last token a
        prefix so autocomplete-style typing works as the user types.
        """
        if not prefix.strip():
            return []
        match_expr = _fts5_query(prefix, prefix=True)
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT c.id FROM contacts c
                JOIN contacts_fts f ON f.rowid = c.id
                WHERE contacts_fts MATCH ?
                ORDER BY c.message_count DESC, c.last_seen DESC
                LIMIT ?
                """,
                (match_expr, limit),
            ).fetchall()
        return self._load_contacts_by_ids([int(r[0]) for r in rows])

    def list_all_contacts(self) -> list[Contact]:
        """Return every contact record (for export)."""
        with self._use() as conn:
            rows = conn.execute(
                "SELECT id FROM contacts ORDER BY last_name, first_name"
            ).fetchall()
        return self._load_contacts_by_ids([int(r[0]) for r in rows])

    def delete_contact(self, *, contact_id: int) -> None:
        """Delete a contact and its emails/aliases."""
        with self._use() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))

    def merge_contacts(
        self, *, target_id: int, source_ids: list[int]
    ) -> Contact:
        """Merge *source_ids* into *target_id*."""
        with self._use() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for src_id in source_ids:
                # Move emails to target (skip duplicates).
                conn.execute(
                    """
                    UPDATE OR IGNORE contact_emails
                    SET contact_id = ? WHERE contact_id = ?
                    """,
                    (target_id, src_id),
                )
                # Move aliases to target (skip duplicates).
                conn.execute(
                    """
                    UPDATE OR IGNORE contact_aliases
                    SET contact_id = ? WHERE contact_id = ?
                    """,
                    (target_id, src_id),
                )
                # Sum message_count.
                conn.execute(
                    """
                    UPDATE contacts SET
                        message_count = message_count + (
                            SELECT COALESCE(message_count, 0)
                            FROM contacts WHERE id = ?
                        ),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        src_id,
                        datetime.now(tz=UTC).isoformat(),
                        target_id,
                    ),
                )
                # Delete source.
                conn.execute(
                    "DELETE FROM contacts WHERE id = ?", (src_id,),
                )
        return self._load_contact(target_id)

    def harvest_contacts(self, messages: Iterable[IndexedMessage]) -> None:
        """Bulk-harvest addresses from *messages*."""
        with self._use() as conn:
            for message in messages:
                _harvest_message_contacts(conn, message)

    def _load_contact(self, contact_id: int) -> Contact:
        """Load a full contact record by id."""
        results = self._load_contacts_by_ids([contact_id])
        if not results:
            raise KeyError(f"contact not found: {contact_id}")
        return results[0]

    def _load_contacts_by_ids(self, ids: list[int]) -> list[Contact]:
        """Batch-load contacts with their emails and aliases in 3 queries."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        with self._use() as conn:
            rows = conn.execute(
                f"""
                SELECT id, first_name, last_name, affix, organization,
                       notes, message_count, last_seen, created_at,
                       updated_at
                FROM contacts WHERE id IN ({placeholders})
                """,  # noqa: S608
                ids,
            ).fetchall()
            email_rows = conn.execute(
                f"""
                SELECT contact_id, email_address
                FROM contact_emails WHERE contact_id IN ({placeholders})
                """,  # noqa: S608
                ids,
            ).fetchall()
            alias_rows = conn.execute(
                f"""
                SELECT contact_id, alias
                FROM contact_aliases WHERE contact_id IN ({placeholders})
                """,  # noqa: S608
                ids,
            ).fetchall()

        emails_by_id: dict[int, list[str]] = {}
        for cid, addr in email_rows:
            emails_by_id.setdefault(int(str(cid)), []).append(str(addr))
        aliases_by_id: dict[int, list[str]] = {}
        for cid, alias in alias_rows:
            aliases_by_id.setdefault(int(str(cid)), []).append(str(alias))

        contacts: list[Contact] = []
        for row in rows:
            cid = int(str(row[0]))
            contacts.append(
                _build_contact(
                    row,
                    [(e,) for e in emails_by_id.get(cid, [])],
                    [(a,) for a in aliases_by_id.get(cid, [])],
                )
            )
        return contacts

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def store_credential(self, *, account_name: str, encrypted: bytes) -> None:
        """Persist an encrypted credential blob for one account."""
        with self._use() as conn:
            conn.execute(
                """
                INSERT INTO credentials (account_name, encrypted)
                VALUES (?, ?)
                ON CONFLICT(account_name) DO UPDATE SET encrypted = excluded.encrypted
                """,
                (account_name, encrypted),
            )

    def get_credential(self, *, account_name: str) -> bytes | None:
        """Return the encrypted credential blob for one account, or None."""
        with self._use() as conn:
            row = conn.execute(
                "SELECT encrypted FROM credentials WHERE account_name = ?",
                (account_name,),
            ).fetchone()
        return bytes(row[0]) if row else None

    def delete_credential(self, *, account_name: str) -> None:
        """Remove the stored credential blob for one account."""
        with self._use() as conn:
            conn.execute(
                "DELETE FROM credentials WHERE account_name = ?",
                (account_name,),
            )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _flags_to_csv(flags: frozenset[MessageFlag]) -> str:
    return ",".join(sorted(flag.value for flag in flags))


def _flags_from_csv(value: str) -> frozenset[MessageFlag]:
    if not value:
        return frozenset()
    return frozenset(MessageFlag(item) for item in value.split(","))


def _fts5_query(text: str, *, prefix: bool = False) -> str:
    """Translate a user-supplied term into a safe FTS5 MATCH expression.

    The result is wrapped as a phrase — double quotes in the input are
    doubled as required by FTS5 — so reserved tokens (AND / OR / NOT /
    NEAR / parentheses / ``-`` / ``:`` / ``*``) cannot escape into
    operator position.  When *prefix* is True the phrase is suffixed with
    ``*`` so the final token matches as a prefix.
    """
    escaped = text.replace('"', '""')
    phrase = f'"{escaped}"'
    return phrase + "*" if prefix else phrase


def _build_fts_match(query: SearchQuery) -> str:
    """Build an FTS5 MATCH expression from a :class:`SearchQuery`.

    Returns ``""`` when every field is empty — callers should then fall
    back to a plain ``SELECT`` without a MATCH clause.
    """
    parts: list[str] = []
    if query.from_address:
        parts.append(f"sender:{_fts5_query(query.from_address)}")
    if query.to_address:
        parts.append(f"recipients:{_fts5_query(query.to_address)}")
    if query.cc_address:
        parts.append(f"cc:{_fts5_query(query.cc_address)}")
    if query.subject:
        parts.append(f"subject:{_fts5_query(query.subject)}")
    if query.body:
        parts.append(f"body_preview:{_fts5_query(query.body)}")
    if query.text:
        phrase = _fts5_query(query.text)
        parts.append(f"(subject:{phrase} OR body_preview:{phrase})")
    return " AND ".join(parts)


def _create_fts_tables(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual tables backing message and contact search."""
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            sender, recipients, cc, subject, body_preview,
            content='messages',
            content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS contacts_fts USING fts5(
            first_name, last_name, email_addresses, aliases,
            content='',
            contentless_delete=1,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )


def _create_fts_triggers(conn: sqlite3.Connection) -> None:
    """Create triggers that keep the FTS tables in sync with base tables."""
    # messages <-> messages_fts (external-content mode, standard pattern).
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(
                rowid, sender, recipients, cc, subject, body_preview
            ) VALUES (
                new.rowid, new.sender, new.recipients, new.cc,
                new.subject, new.body_preview
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(
                messages_fts, rowid, sender, recipients, cc, subject, body_preview
            ) VALUES (
                'delete', old.rowid, old.sender, old.recipients, old.cc,
                old.subject, old.body_preview
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(
                messages_fts, rowid, sender, recipients, cc, subject, body_preview
            ) VALUES (
                'delete', old.rowid, old.sender, old.recipients, old.cc,
                old.subject, old.body_preview
            );
            INSERT INTO messages_fts(
                rowid, sender, recipients, cc, subject, body_preview
            ) VALUES (
                new.rowid, new.sender, new.recipients, new.cc,
                new.subject, new.body_preview
            );
        END
        """
    )
    # contacts <-> contacts_fts (contentless mode — aggregated by hand).
    #
    # On any change to contacts, contact_emails, or contact_aliases we
    # delete any existing FTS row for the affected contact_id and
    # re-insert an aggregated row from the current state of all three
    # source tables.  For INSERT/DELETE we refresh exactly one row; for
    # UPDATE we refresh old and new in case the key changed.
    contact_refresh = (
        """
        DELETE FROM contacts_fts WHERE rowid = {cid};
        INSERT INTO contacts_fts(
            rowid, first_name, last_name, email_addresses, aliases
        )
        SELECT
            c.id,
            c.first_name,
            c.last_name,
            COALESCE(
                (SELECT GROUP_CONCAT(email_address, ' ')
                 FROM contact_emails WHERE contact_id = c.id),
                ''
            ),
            COALESCE(
                (SELECT GROUP_CONCAT(alias, ' ')
                 FROM contact_aliases WHERE contact_id = c.id),
                ''
            )
        FROM contacts c WHERE c.id = {cid};
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS contacts_ai AFTER INSERT ON contacts
        BEGIN
        {contact_refresh.format(cid="new.id")}
        END
        """  # noqa: S608
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS contacts_ad AFTER DELETE ON contacts
        BEGIN
            DELETE FROM contacts_fts WHERE rowid = old.id;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS contacts_au AFTER UPDATE ON contacts
        BEGIN
            DELETE FROM contacts_fts WHERE rowid = old.id;
            {contact_refresh.format(cid="new.id")}
        END
        """  # noqa: S608
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS contact_emails_ai
        AFTER INSERT ON contact_emails
        BEGIN
        {contact_refresh.format(cid="new.contact_id")}
        END
        """  # noqa: S608
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS contact_emails_ad
        AFTER DELETE ON contact_emails
        BEGIN
        {contact_refresh.format(cid="old.contact_id")}
        END
        """  # noqa: S608
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS contact_emails_au
        AFTER UPDATE ON contact_emails
        BEGIN
        {contact_refresh.format(cid="old.contact_id")}
        {contact_refresh.format(cid="new.contact_id")}
        END
        """  # noqa: S608
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS contact_aliases_ai
        AFTER INSERT ON contact_aliases
        BEGIN
        {contact_refresh.format(cid="new.contact_id")}
        END
        """  # noqa: S608
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS contact_aliases_ad
        AFTER DELETE ON contact_aliases
        BEGIN
        {contact_refresh.format(cid="old.contact_id")}
        END
        """  # noqa: S608
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS contact_aliases_au
        AFTER UPDATE ON contact_aliases
        BEGIN
        {contact_refresh.format(cid="old.contact_id")}
        {contact_refresh.format(cid="new.contact_id")}
        END
        """  # noqa: S608
    )


def _indexed_message_from_row(row: sqlite3.Row) -> IndexedMessage:
    # Column order:
    #  0  account_name    1  folder_name    2  message_id
    #  3  sender          4  recipients     5  cc
    #  6  subject         7  body_preview   8  storage_key
    #  9  has_attachments
    # 10  local_flags    11  base_flags     12  local_status
    # 13  received_at    14  uid            15  server_flags
    # 16  extra_imap_flags  17  trashed_at  18  synced_at
    uid_raw = row[14] if len(row) > 14 else None
    uid = int(str(uid_raw)) if uid_raw is not None else None

    server_flags_raw = str(row[15]) if len(row) > 15 else ""
    extra_raw = str(row[16]) if len(row) > 16 else ""
    extra: frozenset[str] = (
        frozenset(extra_raw.split(",")) - {""}
        if extra_raw
        else frozenset()
    )

    trashed_raw = row[17] if len(row) > 17 else None
    trashed_at = (
        datetime.fromisoformat(str(trashed_raw)).astimezone(UTC)
        if trashed_raw
        else None
    )
    synced_raw = row[18] if len(row) > 18 else None
    synced_at = (
        datetime.fromisoformat(str(synced_raw)).astimezone(UTC)
        if synced_raw
        else None
    )

    return IndexedMessage(
        message_ref=MessageRef(
            account_name=str(row[0]),
            folder_name=str(row[1]),
            rfc5322_id=str(row[2]),
        ),
        sender=str(row[3]),
        recipients=str(row[4]),
        cc=str(row[5]),
        subject=str(row[6]),
        body_preview=str(row[7]),
        storage_key=str(row[8]),
        has_attachments=bool(row[9]),
        local_flags=_flags_from_csv(str(row[10])),
        base_flags=_flags_from_csv(str(row[11])),
        local_status=MessageStatus(str(row[12])),
        received_at=datetime.fromisoformat(str(row[13])).astimezone(UTC),
        uid=uid,
        server_flags=_flags_from_csv(server_flags_raw),
        extra_imap_flags=extra,
        trashed_at=trashed_at,
        synced_at=synced_at,
    )


def _build_contact(
    row: tuple[object, ...],
    email_rows: list[tuple[object, ...]],
    alias_rows: list[tuple[object, ...]],
) -> Contact:
    """Assemble a Contact from a contacts row + related rows."""
    last_seen_raw = row[7]
    last_seen = (
        datetime.fromisoformat(str(last_seen_raw)).astimezone(UTC)
        if last_seen_raw
        else None
    )
    affix_raw = str(row[3])
    affix = tuple(json.loads(affix_raw)) if affix_raw and affix_raw != "[]" else ()
    return Contact(
        id=int(str(row[0])),
        first_name=str(row[1]),
        last_name=str(row[2]),
        affix=affix,
        organization=str(row[4]),
        notes=str(row[5]),
        message_count=int(str(row[6])),
        last_seen=last_seen,
        created_at=datetime.fromisoformat(str(row[8])).astimezone(UTC),
        updated_at=datetime.fromisoformat(str(row[9])).astimezone(UTC),
        emails=tuple(str(r[0]) for r in email_rows),
        aliases=tuple(str(r[0]) for r in alias_rows),
    )


def _split_display_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (first_name, last_name).

    Heuristic: the last whitespace-delimited token is the last name;
    everything before it is the first name.
    """
    parts = display_name.strip().split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (" ".join(parts[:-1]), parts[-1])


def _harvest_message_contacts(
    conn: sqlite3.Connection, message: IndexedMessage
) -> None:
    """Parse To/Cc fields of *message* and upsert each address.

    Sender is intentionally excluded — only addresses the user wrote *to*
    (recipients and Cc) are added to the contacts store.
    """
    now = datetime.now(tz=UTC).isoformat()
    raw = ", ".join(filter(None, [message.recipients, message.cc]))
    for display_name, addr in getaddresses([raw]):
        addr = addr.lower().strip()
        if not addr:
            continue
        # Check if email already belongs to a contact.
        existing = conn.execute(
            "SELECT contact_id FROM contact_emails WHERE email_address = ?",
            (addr,),
        ).fetchone()
        if existing:
            # Update stats on existing contact.
            conn.execute(
                """
                UPDATE contacts SET
                    message_count = message_count + 1,
                    last_seen = MAX(COALESCE(last_seen, ''), ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, int(existing[0])),
            )
            # Update name if we have one and the contact's name is empty.
            if display_name.strip():
                first, last = _split_display_name(display_name)
                conn.execute(
                    """
                    UPDATE contacts SET
                        first_name = CASE
                            WHEN first_name = '' THEN ?
                            ELSE first_name END,
                        last_name = CASE
                            WHEN last_name = '' THEN ?
                            ELSE last_name END
                    WHERE id = ?
                    """,
                    (first, last, int(existing[0])),
                )
        else:
            # Create new contact.
            first, last = _split_display_name(display_name)
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    first_name, last_name, message_count, last_seen,
                    created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?)
                """,
                (first, last, now, now, now),
            )
            contact_id = cur.lastrowid
            conn.execute(
                """
                INSERT OR IGNORE INTO contact_emails
                    (contact_id, email_address)
                VALUES (?, ?)
                """,
                (contact_id, addr),
            )
