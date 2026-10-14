"""SQLite-backed metadata index."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sqlite3
from collections.abc import Generator, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from pathlib import Path
from typing import cast

from pony.sqlite import ThreadLocalSqlite, chunked, datetime_to_sql, sql_to_datetime

from .contact_merge import merged_contact
from .contact_naming import clean_display_name, split_display_name
from .domain import (
    Contact,
    FolderMessageSummary,
    FolderRef,
    FolderSyncState,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
    PendingPush,
    SearchQuery,
    SlowPathRow,
)
from .protocols import ContactRepository, IndexRepository

# Bumped when the schema, FTS tables, or triggers change in a way that
# existing databases cannot be migrated to in-place.  Schema version 3
# is the per-row identity rewrite documented under "Schema" in
# docs/synchronization.md: the messages table loses its
# (account, folder, message_id) primary key and gains an
# autoincrement ``id`` plus ``source_folder`` / ``source_uid`` columns.
# Version 4 drops the per-row ``uid_validity`` -- the epoch belongs to
# the folder and to the mirror filename, and nothing ever read the
# column -- and renames ``body_preview`` to ``body_text``, which is
# what it holds.  ``initialize`` migrates a v2 or v3 DB in place (a v2
# one through both steps, dropping synthetic-mid orphans en route);
# anything older is refused and pointed at the reset workflow.
_SCHEMA_VERSION = 4

# How many writes a batch accumulates before it commits and opens a new
# transaction.  Small enough that a killed rebuild loses a second of
# work and the WAL stays checkpointable; large enough that the commit
# cost disappears into the work.
_BATCH_COMMIT_ROWS = 500
_MIGRATABLE_FROM: tuple[int, ...] = (2, 3)


class ContactEmailConflictError(RuntimeError):
    """An email address is already held by a different contact.

    ``contact_emails`` keys on the address, so one address belongs to
    exactly one contact.  The insert used to be ``INSERT OR IGNORE``, so
    a save that named a taken address silently dropped it — and a save
    that *replaced* the contact's only address with a taken one left the
    contact with no address at all, reporting success either way.
    """

    def __init__(self, *, email_address: str, owner_id: int, owner_name: str) -> None:
        self.email_address = email_address
        self.owner_id = owner_id
        self.owner_name = owner_name
        owner = owner_name or f"contact {owner_id}"
        super().__init__(f"{email_address} already belongs to {owner}")


class SchemaMismatchError(RuntimeError):
    """Raised by :meth:`SqliteIndexRepository.initialize` on a legacy DB.

    Carries the path of the offending database so the CLI's recovery
    flow can show it to the user and offer to reset.
    """

    def __init__(self, *, database_path: Path, found: int, expected: int) -> None:
        super().__init__(
            f"Index schema version {found} is below the required {expected} "
            f"at {database_path}",
        )
        self.database_path = database_path
        self.found = found
        self.expected = expected


# Column manifest used by every messages-table query.  Order matches
# the CREATE TABLE in initialize().  ``id`` is the autoincrement
# primary key — omitted from INSERT, never updated.
_FULL_COLS: tuple[str, ...] = (
    "id",
    "account_name",
    "folder_name",
    "uid",
    "message_id",
    "sender",
    "recipients",
    "cc",
    "subject",
    "body_text",
    "storage_key",
    "has_attachments",
    "local_flags",
    "base_flags",
    "server_flags",
    "extra_imap_flags",
    "local_status",
    "received_at",
    "trashed_at",
    "synced_at",
    "source_folder",
    "source_uid",
)
_FULL_SELECT = ", ".join(_FULL_COLS)
_INSERT_COLS = ", ".join(_FULL_COLS[1:])  # skip id
_INSERT_QS = ", ".join(["?"] * (len(_FULL_COLS) - 1))
_UPDATE_ASSIGNS = ", ".join(f"{c} = ?" for c in _FULL_COLS[1:])


def _row_params(message: IndexedMessage) -> tuple[object, ...]:
    """Convert an IndexedMessage to the parameter tuple used by INSERT/UPDATE."""
    trashed_at = datetime_to_sql(message.trashed_at)
    synced_at = datetime_to_sql(message.synced_at)
    return (
        message.message_ref.account_name,
        message.message_ref.folder_name,
        message.uid,
        message.message_id,
        message.sender,
        message.recipients,
        message.cc,
        message.subject,
        message.body_text,
        message.storage_key,
        int(message.has_attachments),
        _flags_to_csv(message.local_flags),
        _flags_to_csv(message.base_flags),
        _flags_to_csv(message.server_flags),
        ",".join(sorted(message.extra_imap_flags)),
        message.local_status.value,
        datetime_to_sql(message.received_at),
        trashed_at,
        synced_at,
        message.source_folder,
        message.source_uid,
    )


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Migrate the v2 messages table to the v3 per-row schema.

    Runs inside the calling transaction.  Drops synthetic-mid orphans
    (the ``<synthetic-…@pony.local>`` rows from the cross-folder
    Message-ID identity model — they never round-tripped to the server
    and were the root of the duplicate-APPEND loop) and the dead
    ``pending_operations`` write-ahead buffer.  The FTS triggers and
    auxiliary tables are recreated from scratch by the caller after
    this returns.
    """
    # Drop FTS triggers and the FTS table itself before touching the
    # base table — the old triggers reference the old column layout
    # and the rebuild path will re-create them once the new schema is
    # in place.
    for trigger in (
        "messages_ai",
        "messages_ad",
        "messages_au",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS messages_fts")

    conn.execute(
        """
        CREATE TABLE messages_new (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name     TEXT    NOT NULL,
            folder_name      TEXT    NOT NULL,
            uid              INTEGER,
            uid_validity     INTEGER NOT NULL DEFAULT 0,
            message_id       TEXT    NOT NULL DEFAULT '',
            sender           TEXT    NOT NULL,
            recipients       TEXT    NOT NULL,
            cc               TEXT    NOT NULL,
            subject          TEXT    NOT NULL,
            body_preview     TEXT    NOT NULL,
            storage_key      TEXT    NOT NULL DEFAULT '',
            has_attachments  INTEGER NOT NULL DEFAULT 0,
            local_flags      TEXT    NOT NULL,
            base_flags       TEXT    NOT NULL,
            server_flags     TEXT    NOT NULL DEFAULT '',
            extra_imap_flags TEXT    NOT NULL DEFAULT '',
            local_status     TEXT    NOT NULL,
            received_at      TEXT    NOT NULL,
            trashed_at       TEXT,
            synced_at        TEXT,
            source_folder    TEXT,
            source_uid       INTEGER
        )
        """
    )
    # Carry over every non-synthetic row.  uid_validity is backfilled
    # from folder_sync_state so the planner can detect stale UIDs in
    # one query without a join.  Synthetic-mid orphans (uid IS NULL +
    # `<synthetic-...@pony.local>` mid) are the rows that drove the
    # APPEND loop — they never existed on the server and must not
    # carry over.
    conn.execute(
        """
        INSERT INTO messages_new (
            account_name, folder_name, uid, uid_validity, message_id,
            sender, recipients, cc, subject, body_preview,
            storage_key, has_attachments,
            local_flags, base_flags, server_flags, extra_imap_flags,
            local_status, received_at, trashed_at, synced_at,
            source_folder, source_uid
        )
        SELECT
            m.account_name, m.folder_name, m.uid,
            COALESCE(s.uid_validity, 0),
            m.message_id,
            m.sender, m.recipients, m.cc, m.subject, m.body_preview,
            m.storage_key, m.has_attachments,
            m.local_flags, m.base_flags,
            m.server_flags, m.extra_imap_flags,
            m.local_status, m.received_at, m.trashed_at, m.synced_at,
            NULL, NULL
        FROM messages m
        LEFT JOIN folder_sync_state s
            ON s.account_name = m.account_name
           AND s.folder_name  = m.folder_name
        WHERE NOT (
            m.uid IS NULL
            AND m.message_id LIKE '<synthetic-%@pony.local>'
        )
        """
    )
    conn.execute("DROP TABLE messages")
    conn.execute("ALTER TABLE messages_new RENAME TO messages")
    # The old pending_operations table was the never-implemented
    # write-ahead buffer; the rewrite drops it.
    conn.execute("DROP TABLE IF EXISTS pending_operations")


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Migrate the v3 messages table to v4: no per-row epoch, named body.

    Runs inside the calling transaction.  Two changes, both of them
    subtractions:

    ``uid_validity`` goes.  A UID is only ever looked up for the folder
    it was found in, so the epoch is the folder's -- it lives in
    ``folder_sync_state``, which is what a sync compares against the
    server, and in the mirror filename, which is what ``pony reindex``
    reads when the index is gone.  The column was backfilled so "the
    planner can detect stale UIDs in one query without a join"; that
    query was never written and nothing else read it.

    ``body_preview`` becomes ``body_text``.  It holds the whole body as
    text, not a prefix of one, and the FTS index is built from it.

    The table is rebuilt rather than altered with ``DROP COLUMN`` and
    ``RENAME COLUMN``: both exist only in newer SQLite, and which one a
    user's Python bundles is not something a migration should depend
    on.  The FTS triggers and the FTS table go first because they name
    the old column; the caller recreates and repopulates them.
    """
    for trigger in ("messages_ai", "messages_ad", "messages_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS messages_fts")

    conn.execute(
        """
        CREATE TABLE messages_new (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name     TEXT    NOT NULL,
            folder_name      TEXT    NOT NULL,
            uid              INTEGER,
            message_id       TEXT    NOT NULL DEFAULT '',
            sender           TEXT    NOT NULL,
            recipients       TEXT    NOT NULL,
            cc               TEXT    NOT NULL,
            subject          TEXT    NOT NULL,
            body_text        TEXT    NOT NULL,
            storage_key      TEXT    NOT NULL DEFAULT '',
            has_attachments  INTEGER NOT NULL DEFAULT 0,
            local_flags      TEXT    NOT NULL,
            base_flags       TEXT    NOT NULL,
            server_flags     TEXT    NOT NULL DEFAULT '',
            extra_imap_flags TEXT    NOT NULL DEFAULT '',
            local_status     TEXT    NOT NULL,
            received_at      TEXT    NOT NULL,
            trashed_at       TEXT,
            synced_at        TEXT,
            source_folder    TEXT,
            source_uid       INTEGER
        )
        """
    )
    # ``id`` is carried over explicitly: it is the row identity every
    # ``MessageRef`` holds, and anything that remembered one -- the
    # reader's last view, a pending move's source row -- would point at
    # a different message if the rebuild renumbered them.
    conn.execute(
        """
        INSERT INTO messages_new (
            id, account_name, folder_name, uid, message_id,
            sender, recipients, cc, subject, body_text,
            storage_key, has_attachments,
            local_flags, base_flags, server_flags, extra_imap_flags,
            local_status, received_at, trashed_at, synced_at,
            source_folder, source_uid
        )
        SELECT
            id, account_name, folder_name, uid, message_id,
            sender, recipients, cc, subject, body_preview,
            storage_key, has_attachments,
            local_flags, base_flags, server_flags, extra_imap_flags,
            local_status, received_at, trashed_at, synced_at,
            source_folder, source_uid
        FROM messages
        """
    )
    conn.execute("DROP TABLE messages")
    conn.execute("ALTER TABLE messages_new RENAME TO messages")


class SqliteIndexRepository(IndexRepository, ContactRepository):
    """Persist indexed metadata and sync state in SQLite.

    **One connection per thread, and one transaction per thread with
    it.**  Transaction state describes a flow of control, so it is held
    in ``threading.local()``: a shared one let a write made on the UI
    thread join a sync's open transaction, report success, and then be
    thrown away by the sync's rollback — and let a failed statement on
    either side discard up to ``_BATCH_COMMIT_ROWS`` of the other's
    applied work.

    For bulk work (syncing a folder, rescanning an archive) callers wrap
    a block in::

        with repo.connection():
            repo.insert_message(...)
            ...

    which defers the commit — but only up to ``_BATCH_COMMIT_ROWS``
    writes, after which the batch commits and carries on.  An unbounded
    transaction grows the WAL until throughput collapses, and loses
    everything it has done if the process is killed; committing in
    bounded steps keeps a long rebuild both fast and resumable.  On
    exception whatever that transaction still holds is rolled back.
    Nesting is safe, and so is a nested block that raises and is caught:
    each one is fenced by a ``SAVEPOINT``, so a sync that logs a failed
    operation and moves to the next keeps what it already did.

    The bound is also what keeps a rebuild *visible*.  A reader on
    another thread has its own connection, so it sees a batch's work
    only once committed — in steps of ``_BATCH_COMMIT_ROWS`` rows, not
    after the whole 14-minute rescan, which is what made a single
    shared connection look attractive in the first place.
    """

    def __init__(self, *, database_path: Path) -> None:
        self._database_path = database_path
        self._db = ThreadLocalSqlite(database_path, commit_every=_BATCH_COMMIT_ROWS)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def __del__(self) -> None:
        # A repository that goes out of scope without close() still has
        # to let go of the file; nothing is imported here because this
        # can run during interpreter shutdown.
        with contextlib.suppress(Exception):
            self.close()

    def close(self) -> None:
        """Release this repository's connections.

        Safe to call while another thread is mid-batch: that thread's
        connection is left for it to commit, and no thread is handed a
        connection this call has closed.  Nothing in ``src/pony`` calls
        it yet — the mail index relies on ``__del__``.
        """
        self._db.close()

    @contextlib.contextmanager
    def connection(self) -> Generator[None]:
        """Hold one transaction across a batch of operations.

        Commits when the block exits cleanly, rolls back what the
        transaction still holds on exception, and commits along the way
        every ``_BATCH_COMMIT_ROWS`` writes so the transaction never
        grows without bound — which also means an exception cannot undo
        what an earlier bounded commit already wrote.

        Reentrant: nested blocks are units of work inside the
        outermost one, which is what ends the batch.
        """
        with self._db.transaction():
            yield

    @contextlib.contextmanager
    def _use_atomic(self) -> Generator[sqlite3.Connection]:
        """Yield this thread's connection for a call that writes twice.

        A ``_use()`` call runs in autocommit when no batch is open,
        which is right for one statement and wrong for several: a
        method that reads and then deletes what it read, or writes
        three tables, has to land all at once or not at all.  Inside a
        batch this is the same unit of work ``_use()`` is.
        """
        with self._db.transaction() as conn:
            yield conn

    @contextlib.contextmanager
    def _use(self) -> Generator[sqlite3.Connection]:
        """Yield this thread's connection for the duration of one call.

        Outside a batch the statement is committed when it completes, as
        a single-statement call expects.  Inside one, the commit is the
        batch's business -- except that a batch which has accumulated
        ``_BATCH_COMMIT_ROWS`` writes commits here and starts a new
        transaction, so a long rebuild stays bounded and resumable.
        """
        with self._db.statement() as conn:
            yield conn

    def initialize(self) -> None:
        """Create or migrate the schema, refusing DBs we cannot upgrade."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        # One transaction for the whole thing: the v2 -> v3 migration
        # rewrites the messages table in several statements and a
        # half-applied rewrite is not a schema anything can read.
        with self._use_atomic() as conn:
            version_row = conn.execute("PRAGMA user_version").fetchone()
            version = int(version_row[0]) if version_row is not None else 0
            has_messages = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
                ).fetchone()
                is not None
            )

            if has_messages and version not in (
                *_MIGRATABLE_FROM,
                _SCHEMA_VERSION,
            ):
                # A messages table at any unrecognised version is a
                # legacy DB.  The CLI offers an export-then-reset
                # recovery workflow.  ``version == 0`` is a fresh DB
                # only when there is no messages table yet.
                raise SchemaMismatchError(
                    database_path=self._database_path,
                    found=version,
                    expected=_SCHEMA_VERSION,
                )

            migrated = has_messages and version in _MIGRATABLE_FROM
            # One step at a time, so a v2 database arrives at v4 through
            # the same v3 rewrite a v3 one was built by.
            if has_messages and version == 2:
                _migrate_v2_to_v3(conn)
            if has_messages and version in (2, 3):
                _migrate_v3_to_v4(conn)

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name     TEXT    NOT NULL,
                    folder_name      TEXT    NOT NULL,
                    uid              INTEGER,
                    message_id       TEXT    NOT NULL DEFAULT '',
                    sender           TEXT    NOT NULL,
                    recipients       TEXT    NOT NULL,
                    cc               TEXT    NOT NULL,
                    subject          TEXT    NOT NULL,
                    body_text        TEXT    NOT NULL,
                    storage_key      TEXT    NOT NULL DEFAULT '',
                    has_attachments  INTEGER NOT NULL DEFAULT 0,
                    local_flags      TEXT    NOT NULL,
                    base_flags       TEXT    NOT NULL,
                    server_flags     TEXT    NOT NULL DEFAULT '',
                    extra_imap_flags TEXT    NOT NULL DEFAULT '',
                    local_status     TEXT    NOT NULL,
                    received_at      TEXT    NOT NULL,
                    trashed_at       TEXT,
                    synced_at        TEXT,
                    source_folder    TEXT,
                    source_uid       INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_uid
                ON messages (account_name, folder_name, uid)
                WHERE uid IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_messages_account_folder
                ON messages (account_name, folder_name)
                """
            )
            # Covers the folder list view: equality on (account_name,
            # folder_name) plus ORDER BY received_at DESC without a
            # separate sort step.  See list_folder_message_summaries.
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_messages_folder_received
                ON messages (account_name, folder_name, received_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_messages_message_id
                ON messages (account_name, message_id)
                WHERE message_id != ''
                """
            )
            # Covering index for the push planner's "what has no handle
            # yet" question, which the sync asks once per folder and
            # which `uid IS NULL` answers: partial, so it holds only
            # the rows that are actually waiting, and carrying the
            # columns the plan prints so the lookup needs no heap reads.
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_messages_pending
                    ON messages (
                        account_name, folder_name, sender, subject, received_at
                    )
                    WHERE uid IS NULL
                """
            )
            # Covering index for unread_counts_by_folder: the query filters
            # on (account_name, local_status) and groups by folder_name, with
            # a LIKE filter on local_flags.  A covering index lets SQLite
            # serve the query from the index B-tree alone (no heap reads),
            # cutting the scan time on large accounts (100k+ rows) by ~30×.
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_messages_unseen
                ON messages (account_name, local_status, folder_name, local_flags)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS folder_sync_state (
                    account_name   TEXT    NOT NULL,
                    folder_name    TEXT    NOT NULL,
                    uid_validity   INTEGER NOT NULL,
                    highest_uid    INTEGER NOT NULL,
                    uidnext        INTEGER NOT NULL DEFAULT 0,
                    highest_modseq INTEGER NOT NULL DEFAULT 0,
                    synced_at      TEXT    NOT NULL,
                    PRIMARY KEY (account_name, folder_name)
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
            # ``contact_emails`` is keyed on the address, so without
            # this every "which addresses does contact N have" -- the
            # rewrite inside ``upsert_contact``, the per-contact load --
            # scanned the whole table.
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_contact_emails_contact
                ON contact_emails (contact_id)
                """
            )
            # Serves find_address_less_contact_by_name: the BBDB import
            # asks it once per address-less record, and it used to answer
            # by reading the whole address book into Python objects.
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_contacts_name_folded
                ON contacts (lower(trim(last_name)), lower(trim(first_name)))
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
            if migrated:
                # The rows were copied into the new base table before the
                # triggers existed, so nothing populated the index.  An
                # external-content FTS5 table has to be told to catch up;
                # without this every search silently returns nothing and
                # no ordinary sync ever repairs it, because unchanged rows
                # are never rewritten.
                conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def insert_message(self, *, message: IndexedMessage) -> IndexedMessage:
        """Insert a fresh row, returning it with its assigned id."""
        params = _row_params(message)
        with self._use() as conn:
            cur = conn.execute(
                f"INSERT INTO messages ({_INSERT_COLS}) VALUES ({_INSERT_QS})",  # noqa: S608
                params,
            )
            new_id = cur.lastrowid or 0
        return dataclasses.replace(
            message,
            message_ref=MessageRef(
                account_name=message.message_ref.account_name,
                folder_name=message.message_ref.folder_name,
                id=new_id,
            ),
        )

    def update_message(self, *, message: IndexedMessage) -> None:
        """Update an existing row keyed by ``message_ref.id``."""
        if message.message_ref.id <= 0:
            raise ValueError(
                "update_message requires a row id; use insert_message for new rows"
            )
        params = _row_params(message) + (message.message_ref.id,)
        with self._use() as conn:
            conn.execute(
                f"UPDATE messages SET {_UPDATE_ASSIGNS} WHERE id = ?",  # noqa: S608
                params,
            )

    def upsert_message(self, *, message: IndexedMessage) -> IndexedMessage:
        """Insert when ``message_ref.id <= 0``, update otherwise."""
        if message.message_ref.id <= 0:
            return self.insert_message(message=message)
        self.update_message(message=message)
        return message

    def delete_message(self, *, message_ref: MessageRef) -> None:
        """Remove one message row from the index by id."""
        with self._use() as conn:
            conn.execute(
                "DELETE FROM messages WHERE id = ?",
                (message_ref.id,),
            )

    def purge_expired_trash(
        self, *, account_name: str, retention_days: int
    ) -> list[tuple[FolderRef, str]]:
        """Delete trashed messages older than *retention_days*.

        Returns ``[(folder_ref, storage_key), ...]`` for each purged row so
        the caller can clean up the corresponding mirror files.
        """
        cutoff = datetime_to_sql(datetime.now(tz=UTC) - timedelta(days=retention_days))
        with self._use_atomic() as conn:
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
                        account_name=str(r[0]),
                        folder_name=str(r[1]),
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
            rows = conn.execute("SELECT DISTINCT account_name FROM messages").fetchall()
        return [str(r[0]) for r in rows]

    def purge_account(self, *, account_name: str) -> None:
        """Remove all data for one account from every table."""
        with self._use() as conn:
            for table in ("messages", "folder_sync_state"):
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
        with self._use_atomic() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT folder_name FROM folder_sync_state
                WHERE account_name = ?
                """,
                (account_name,),
            ).fetchall()
            stale = [str(r[0]) for r in rows if str(r[0]) not in active_folders]
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
        """Return one indexed message by row id, or None."""
        with self._use() as conn:
            row = conn.execute(
                f"SELECT {_FULL_SELECT} FROM messages WHERE id = ?",  # noqa: S608
                (message_ref.id,),
            ).fetchone()
        if row is None:
            return None
        return _indexed_message_from_row(row)

    def find_messages_by_message_id(
        self,
        *,
        account_name: str,
        message_id: str,
        folder_name: str | None = None,
    ) -> Sequence[IndexedMessage]:
        """Return every row whose RFC 5322 ``Message-ID:`` matches.

        Rows store the header verbatim, i.e. with its angle brackets,
        but people type and paste the bare form.  Both are accepted, so
        a caller does not have to know which shape it holds — the CLI
        used to normalise and the MCP server did not, which meant the
        same lookup succeeded on one and returned nothing on the other.

        Empty Message-IDs never match — they would otherwise pull up
        every row that had no header at import time.
        """
        if not message_id.strip():
            return ()
        sql = (
            f"SELECT {_FULL_SELECT} FROM messages "  # noqa: S608
            "WHERE account_name = ? AND message_id IN (?, ?, ?)"
        )
        stripped = message_id.strip().strip("<>")
        params: list[object] = [
            account_name,
            message_id.strip(),
            f"<{stripped}>",
            stripped,
        ]
        if folder_name is not None:
            sql += " AND folder_name = ?"
            params.append(folder_name)
        sql += " ORDER BY received_at DESC, id ASC"
        with self._use() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_indexed_message_from_row(row) for row in rows)

    def find_pending_matches(
        self,
        *,
        folder: FolderRef,
        sender: str,
        subject: str,
        received_at: datetime,
    ) -> Sequence[IndexedMessage]:
        """UID-less rows in *folder* that describe the same message.

        What a message says about itself — sender, subject and date — is
        how a fetched message is matched against a row that is still
        waiting for a UID, when the Message-ID cannot settle it.

        The comparison is SQL, and a partial index covers it, because
        the caller runs once per fetched message: listing the folder and
        filtering in Python cost one full materialisation of the folder
        per message, which made a first sync quadratic in the folder's
        size -- 15,000 messages meant 15,000 scans of a growing table.
        In a freshly rebuilt index there are no UID-less rows at all, so
        this now answers from an empty index.
        """
        with self._use() as conn:
            rows = conn.execute(
                f"SELECT {_FULL_SELECT} FROM messages "  # noqa: S608
                "WHERE account_name = ? AND folder_name = ? AND uid IS NULL "
                "AND local_status IN (?, ?) "
                "AND sender = ? AND subject = ? AND received_at = ?",
                (
                    folder.account_name,
                    folder.folder_name,
                    MessageStatus.ACTIVE.value,
                    MessageStatus.TRASHED.value,
                    sender,
                    subject,
                    datetime_to_sql(received_at),
                ),
            ).fetchall()
        return tuple(_indexed_message_from_row(row) for row in rows)

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

    def unread_counts_by_folder(self, *, account_name: str) -> dict[str, int]:
        """Return ``{folder_name: unread_count}`` for one account.

        Done in one GROUP BY so the folder panel doesn't have to
        materialise every ``IndexedMessage`` row just to count.
        ``local_flags`` is stored as a CSV of flag values — the pattern
        wraps the column in commas so ``,seen,`` is the boundary match
        and ``unseen`` / ``foreseen`` can't false-positive.
        """
        if not self._database_path.exists():
            return {}
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT folder_name, COUNT(*)
                FROM messages
                WHERE account_name = ?
                  AND local_status = 'active'
                  AND (',' || local_flags || ',') NOT LIKE '%,seen,%'
                GROUP BY folder_name
                """,
                (account_name,),
            ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def list_folder_storage_keys(
        self, *, folder: FolderRef
    ) -> Mapping[str, MessageRef]:
        """Return ``{storage_key: message_ref}`` for every row in *folder*.

        Lean projection used by the local-mirror rescan: it only needs
        the storage_key set (to diff against disk) plus a MessageRef for
        each gone row (to delete it).  Pulling 23 columns and parsing
        flags/datetimes for every row was the dominant cost on cold
        startup of large archives.  Empty storage_keys (pending-append
        rows) are filtered in SQL.
        """
        with self._use() as conn:
            rows = conn.execute(
                "SELECT id, storage_key FROM messages "
                "WHERE account_name = ? AND folder_name = ? "
                "AND storage_key != ''",
                (folder.account_name, folder.folder_name),
            ).fetchall()
        return {
            str(r[1]): MessageRef(
                account_name=folder.account_name,
                folder_name=folder.folder_name,
                id=int(str(r[0])),
            )
            for r in rows
        }

    def adopt_uid(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
        uid: int,
    ) -> bool:
        """Give an existing handle-less row the handle its file carries.

        Only a row with ``uid IS NULL`` is touched, so this cannot
        renumber a row that already knows its handle.  Returns whether
        one was adopted.

        A rebuild needs this because a row may already exist without a
        handle -- a rescan creates exactly that -- and skipping it left
        the UID unrecovered while the folder watermark was still
        stamped, so the index asserted an epoch it could not honour.
        ``False`` also comes back when another row in the folder already
        holds that UID, which is a duplicated handle on disk and the
        caller's to report rather than this method's to resolve.
        """
        with self._use() as conn:
            try:
                cursor = conn.execute(
                    "UPDATE messages SET uid = ? "
                    "WHERE account_name = ? AND folder_name = ? "
                    "AND storage_key = ? AND uid IS NULL",
                    (
                        uid,
                        folder.account_name,
                        folder.folder_name,
                        storage_key,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return cursor.rowcount > 0

    def list_folder_messages(
        self, *, folder: FolderRef, limit: int | None = None
    ) -> Sequence[IndexedMessage]:
        """Return indexed messages for a folder ordered by received date.

        *limit* caps the result in SQL.  A caller that wants the newest
        hundred and slices the answer in Python has already paid for
        every row in the folder; ``ix_messages_folder_received`` serves
        the order, so the capped query stops after the rows it returns.
        """
        sql = (
            f"SELECT {_FULL_SELECT} FROM messages "  # noqa: S608
            "WHERE account_name = ? AND folder_name = ? "
            "ORDER BY received_at DESC"
        )
        params: list[object] = [folder.account_name, folder.folder_name]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._use() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_indexed_message_from_row(row) for row in rows)

    def list_folder_message_summaries(
        self,
        *,
        folder: FolderRef,
        active_only: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[FolderMessageSummary]:
        """Return a narrow per-row projection for the folder list view.

        See ``FolderMessageSummary`` for the motivation: the full
        ``list_folder_messages`` path materialises three datetime
        parses and three flag-set constructions per row — wasted work
        when the list only wants the correspondent, subject,
        received_at and a handful of booleans.  This path selects only those columns
        and skips the unused parses, and pushes ``local_status`` and
        ``received_at`` ordering into SQL so callers don't re-sort.
        """
        sql = (
            "SELECT id, account_name, folder_name, message_id, "
            "storage_key, sender, recipients, subject, received_at, "
            "has_attachments, local_flags, local_status "
            "FROM messages WHERE account_name = ? AND folder_name = ?"
        )
        params: list[object] = [folder.account_name, folder.folder_name]
        if active_only:
            # PENDING_MOVE rows live in their target folder awaiting a
            # server-side push; the user expects them visible there
            # immediately after archive/move, before the next sync.
            sql += " AND local_status IN (?, ?)"
            params.append(MessageStatus.ACTIVE.value)
            params.append(MessageStatus.PENDING_MOVE.value)
        sql += " ORDER BY received_at DESC"
        if limit is not None:
            # In SQL rather than in a caller's slice: a front end that
            # draws the first screenful should not pay for materialising
            # a folder of fifty thousand to throw it away.  SQLite needs
            # a LIMIT before it will take an OFFSET.
            sql += " LIMIT ? OFFSET ?"
            params.append(limit)
            params.append(offset)
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        with self._use() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_summary_from_row(row) for row in rows)

    def mark_folder_read(self, *, folder: FolderRef) -> int:
        """Add SEEN to every active, unseen message in *folder*.

        Uses a single UPDATE so no Python-level row materialisation is
        needed even for large folders.  Returns the number of rows changed.

        The CSV it writes has to come out in the order
        :func:`_flags_to_csv` produces, because
        :meth:`list_folder_push_candidates` compares ``local_flags``
        against ``base_flags`` as *strings*: a row whose flags differ
        only in their order would look changed on every sync and have
        them pushed again, for ever.  Appending is canonical only while
        ``seen`` sorts last of the flag values, which is an accident of
        the vocabulary rather than a decision --
        ``tests/test_index_store.py::MarkFolderReadTestCase::test_seen_sorts_last_which_is_what_lets_the_sql_append_it``
        is what fails when a flag is added after it.
        """
        with self._use_atomic() as conn:
            conn.execute(
                """
                UPDATE messages
                SET local_flags = CASE
                    WHEN local_flags = '' THEN 'seen'
                    ELSE local_flags || ',seen'
                END
                WHERE account_name = ?
                  AND folder_name = ?
                  AND local_status = 'active'
                  AND (',' || local_flags || ',') NOT LIKE '%,seen,%'
                """,
                (folder.account_name, folder.folder_name),
            )
            return int(conn.execute("SELECT changes()").fetchone()[0])

    def search(
        self,
        *,
        query: SearchQuery,
        account_name: str | None,
        folder_name: str | None = None,
        limit: int = 500,
    ) -> Sequence[IndexedMessage]:
        """Run an FTS5-backed metadata search.

        Folding is always on — the FTS5 ``unicode61`` tokenizer is case-
        and diacritic-insensitive by construction.

        *limit* bounds the rows materialised.  The query ran unbounded,
        and a broad term over a large mailbox takes as long as building
        every row — measured at 385 ms for 40 000 messages, and linear.
        Both front ends run it off their event loop, the terminal in the
        message list's own worker and the browser in the thread pool.

        *folder_name* narrows the search here rather than in the caller.
        Both front ends used to ask for the account's hits and drop the
        other folders afterwards, which put the filter on the wrong side
        of *limit*: one matching message in INBOX behind 500 in Archive
        came back as "nothing found".
        """
        match_expr = _build_fts_match(query)
        # Trashed rows stay in the table until retention reaps them, so
        # without this the duplicates `pony folder dedup` just hid come
        # straight back in every search hit.  Same statuses the folder
        # list shows: PENDING_MOVE rows are real mail awaiting a push.
        clauses: list[str] = ["m.local_status IN (?, ?)"]
        params: list[object] = [
            MessageStatus.ACTIVE.value,
            MessageStatus.PENDING_MOVE.value,
        ]

        if match_expr:
            clauses.append("messages_fts MATCH ?")
            params.append(match_expr)
        if account_name is not None:
            clauses.append("m.account_name = ?")
            params.append(account_name)
        if folder_name is not None:
            clauses.append("m.folder_name = ?")
            params.append(folder_name)
        where_sql = " AND ".join(clauses)
        join_sql = "JOIN messages_fts f ON f.rowid = m.rowid" if match_expr else ""

        select_cols = ", ".join(f"m.{c}" for c in _FULL_COLS)
        with self._use() as conn:
            rows = conn.execute(
                f"""
                SELECT {select_cols}
                FROM messages m
                {join_sql}
                WHERE {where_sql}
                ORDER BY m.received_at DESC
                LIMIT ?
                """,  # noqa: S608
                [*params, limit],
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
                    account_name, folder_name, uid_validity, highest_uid,
                    uidnext, highest_modseq, synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_name, folder_name)
                DO UPDATE SET
                    uid_validity   = excluded.uid_validity,
                    highest_uid    = excluded.highest_uid,
                    uidnext        = excluded.uidnext,
                    highest_modseq = excluded.highest_modseq,
                    synced_at      = excluded.synced_at
                """,
                (
                    state.account_name,
                    state.folder_name,
                    state.uid_validity,
                    state.highest_uid,
                    state.uidnext,
                    state.highest_modseq,
                    datetime_to_sql(state.synced_at),
                ),
            )

    def get_folder_sync_state(
        self, *, account_name: str, folder_name: str
    ) -> FolderSyncState | None:
        """Load the sync watermark for one folder."""
        with self._use() as conn:
            row = conn.execute(
                """
                SELECT account_name, folder_name, uid_validity, highest_uid,
                       uidnext, highest_modseq, synced_at
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
            uidnext=int(row[4]),
            highest_modseq=int(row[5]),
            synced_at=_sql_datetime(row[6]),
        )

    def list_folder_sync_states(self, *, account_name: str) -> list[FolderSyncState]:
        """Return all sync watermarks for one account."""
        if not self._database_path.exists():
            return []
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT account_name, folder_name, uid_validity, highest_uid,
                       uidnext, highest_modseq, synced_at
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
                uidnext=int(r[4]),
                highest_modseq=int(r[5]),
                synced_at=_sql_datetime(r[6]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Planner-facing queries
    # ------------------------------------------------------------------

    def count_uids_for_folder(self, *, account_name: str, folder_name: str) -> int:
        """Return ``COUNT(*)`` of rows with ``uid IS NOT NULL`` for one folder."""
        with self._use() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE account_name = ? AND folder_name = ? AND uid IS NOT NULL
                """,
                (account_name, folder_name),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_folder_uids(self, *, account_name: str, folder_name: str) -> set[int]:
        """Return the set of locally-known UIDs for one folder."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT uid FROM messages
                WHERE account_name = ? AND folder_name = ? AND uid IS NOT NULL
                """,
                (account_name, folder_name),
            ).fetchall()
        return {int(str(r[0])) for r in rows}

    def list_folder_push_candidates(
        self, *, account_name: str, folder_name: str
    ) -> Sequence[PendingPush]:
        """Return rows needing a server-side push (SQL-filtered)."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT id, message_id, local_status, uid, storage_key,
                       local_flags, extra_imap_flags, trashed_at,
                       source_folder, source_uid
                FROM messages
                WHERE account_name = ? AND folder_name = ?
                  AND (
                    local_status = ?
                    OR local_status = ?
                    OR (local_status = ? AND uid IS NULL)
                    OR (
                        local_status = ?
                        AND uid IS NOT NULL
                        AND local_flags != base_flags
                    )
                  )
                """,
                (
                    account_name,
                    folder_name,
                    MessageStatus.TRASHED.value,
                    MessageStatus.PENDING_MOVE.value,
                    MessageStatus.ACTIVE.value,
                    MessageStatus.ACTIVE.value,
                ),
            ).fetchall()
        return tuple(
            PendingPush(
                message_ref=MessageRef(
                    account_name=account_name,
                    folder_name=folder_name,
                    id=int(str(r[0])),
                ),
                message_id=str(r[1]),
                local_status=MessageStatus(str(r[2])),
                uid=int(str(r[3])) if r[3] is not None else None,
                storage_key=str(r[4]),
                local_flags=_flags_from_csv(str(r[5])),
                extra_imap_flags=(
                    frozenset(str(r[6]).split(",")) - {""} if r[6] else frozenset()
                ),
                trashed_at=_sql_datetime_or_none(r[7]),
                source_folder=str(r[8]) if r[8] is not None else None,
                source_uid=int(str(r[9])) if r[9] is not None else None,
            )
            for r in rows
        )

    def list_folder_slow_path_rows(
        self, *, account_name: str, folder_name: str
    ) -> Sequence[SlowPathRow]:
        """Return the narrow per-row projection the slow-path planner uses."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT id, message_id, local_status, uid, storage_key,
                       local_flags, base_flags, extra_imap_flags,
                       source_folder, source_uid
                FROM messages
                WHERE account_name = ? AND folder_name = ?
                """,
                (account_name, folder_name),
            ).fetchall()
        return tuple(
            _slow_path_row_from_projection(account_name, folder_name, r) for r in rows
        )

    def list_folder_slow_path_rows_by_uid(
        self,
        *,
        account_name: str,
        folder_name: str,
        uids: Sequence[int],
    ) -> Sequence[SlowPathRow]:
        """Return slow-path rows for a specific UID set."""
        if not uids:
            return ()
        result: list[SlowPathRow] = []
        ordered_uids = tuple(dict.fromkeys(int(uid) for uid in uids))
        for chunk in chunked(ordered_uids):
            placeholders = ", ".join("?" for _ in chunk)
            with self._use() as conn:
                rows = conn.execute(
                    f"""
                    SELECT id, message_id, local_status, uid, storage_key,
                           local_flags, base_flags, extra_imap_flags,
                           source_folder, source_uid
                    FROM messages
                    WHERE account_name = ? AND folder_name = ?
                      AND uid IN ({placeholders})
                    """,  # noqa: S608
                    (account_name, folder_name, *chunk),
                ).fetchall()
            result.extend(
                _slow_path_row_from_projection(account_name, folder_name, r)
                for r in rows
            )
        return tuple(result)

    def list_folder_base_flags(
        self, *, account_name: str, folder_name: str
    ) -> dict[int, tuple[frozenset[MessageFlag], frozenset[str]]]:
        """Return ``{uid: (base_flags, extra_imap_flags)}`` for UID-bearing rows."""
        with self._use() as conn:
            rows = conn.execute(
                """
                SELECT uid, base_flags, extra_imap_flags
                FROM messages
                WHERE account_name = ? AND folder_name = ? AND uid IS NOT NULL
                """,
                (account_name, folder_name),
            ).fetchall()
        result: dict[int, tuple[frozenset[MessageFlag], frozenset[str]]] = {}
        for uid, base_flags, extra_imap_flags in rows:
            extras = (
                frozenset(str(extra_imap_flags).split(",")) - {""}
                if extra_imap_flags
                else frozenset()
            )
            result[int(str(uid))] = (
                _flags_from_csv(str(base_flags)),
                extras,
            )
        return result

    def remap_storage_keys(self, *, folder: FolderRef, remap: dict[str, str]) -> int:
        """Apply a mirror's ``{old_key: new_key}`` to this folder's rows.

        Compacting an mbox renumbers its positional keys.  That is the
        one moment the numbering moves, and the mirror hands the mapping
        over rather than letting it shift under rows written long ago —
        so this is what keeps the index pointing at the right messages.

        Done in one statement inside the caller's transaction, so a
        half-applied remap cannot survive a failure.  Returns the number
        of rows changed.
        """
        if not remap:
            return 0
        changed = 0
        with self._use_atomic() as conn:
            for old_key, new_key in remap.items():
                if old_key == new_key:
                    continue
                conn.execute(
                    """
                    UPDATE messages SET storage_key = ?
                    WHERE account_name = ? AND folder_name = ? AND storage_key = ?
                    """,
                    (new_key, folder.account_name, folder.folder_name, old_key),
                )
                changed += int(conn.execute("SELECT changes()").fetchone()[0])
        return changed

    def clear_uids_for_folder(self, *, account_name: str, folder_name: str) -> None:
        """Drop the stale UID epoch from a folder on UIDVALIDITY reset.

        A UIDVALIDITY change means the server's UIDs are meaningless, not
        that the mail is gone.  This used to DELETE every ACTIVE row —
        which, when the folder had also been emptied server-side, threw
        away the user's only copy without the confirmation that the same
        event triggers when UIDVALIDITY is unchanged.

        The rows keep everything except the UID.  The server re-reports
        the same messages under the new epoch and the planner pairs each
        one with the row it belongs to (``AdoptUidOp``), so no message is
        re-downloaded, re-uploaded or duplicated.  A row with no
        counterpart on the server is then genuinely local-only and is
        pushed, which is the non-destructive answer.
        """
        with self._use_atomic() as conn:
            # Every row, whatever its status: a stale UID must not
            # accidentally match one in the new epoch, and a TRASHED row
            # needs re-adopting before its deletion can be pushed.
            conn.execute(
                """
                UPDATE messages
                SET uid = NULL,
                    server_flags = '', extra_imap_flags = '',
                    synced_at = NULL
                WHERE account_name = ? AND folder_name = ?
                """,
                (account_name, folder_name),
            )
            # A move out of this folder is still waiting to be pushed,
            # and its handle on the server side is a UID in the epoch
            # that just ended.  Clearing it lets the re-fetch re-adopt
            # the source, instead of the move being pushed as an append
            # while the original stays where it was.
            conn.execute(
                """
                UPDATE messages
                SET source_uid = NULL
                WHERE account_name = ? AND source_folder = ?
                """,
                (account_name, folder_name),
            )

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def upsert_contact(self, *, contact: Contact) -> Contact:
        """Insert or update a contact record.  Returns the saved contact."""
        now = datetime.now(tz=UTC)
        updated_at = datetime_to_sql(contact.updated_at)
        with self._use_atomic() as conn:
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
                        contact.first_name,
                        contact.last_name,
                        json.dumps(list(contact.affix)),
                        contact.organization,
                        contact.notes,
                        contact.message_count,
                        datetime_to_sql(contact.last_seen),
                        datetime_to_sql(now),
                        contact.id,
                    ),
                )
                contact_id = contact.id
            else:
                created_at = datetime_to_sql(contact.created_at)
                cur = conn.execute(
                    """
                    INSERT INTO contacts (
                        first_name, last_name, affix, organization, notes,
                        message_count, last_seen, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contact.first_name,
                        contact.last_name,
                        json.dumps(list(contact.affix)),
                        contact.organization,
                        contact.notes,
                        contact.message_count,
                        datetime_to_sql(contact.last_seen),
                        created_at,
                        updated_at,
                    ),
                )
                contact_id = cur.lastrowid or 0
            # Replace emails.
            conn.execute(
                "DELETE FROM contact_emails WHERE contact_id = ?",
                (contact_id,),
            )
            for email in contact.emails:
                owner = conn.execute(
                    "SELECT contact_id FROM contact_emails WHERE email_address = ?",
                    (_normalized_address(email),),
                ).fetchone()
                if owner is not None and int(owner[0]) != contact_id:
                    other = self._load_contact_or_none(conn, int(owner[0]))
                    raise ContactEmailConflictError(
                        email_address=email,
                        owner_id=int(owner[0]),
                        owner_name=other.display_name if other else "",
                    )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO contact_emails
                        (contact_id, email_address)
                    VALUES (?, ?)
                    """,
                    (contact_id, _normalized_address(email)),
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
        addr = _normalized_address(email_address)
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
        match_expr = _contact_fts_query(prefix)
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

    def get_contact(self, *, contact_id: int) -> Contact | None:
        """One contact by id, or None.

        A contact detail page, a contact form and the merge prompt each
        want one record.  Reading the whole address book and filtering
        on ``c.id`` in Python cost three queries and a ``Contact`` per
        row to find it.
        """
        found = self._load_contacts_by_ids([contact_id])
        return found[0] if found else None

    def find_address_less_contact_by_name(
        self, *, first_name: str, last_name: str
    ) -> Contact | None:
        """An address-less contact with this (folded) name, if any.

        The BBDB import's fallback for a record that carries no email at
        all: there is nothing else to match it on, and matching only
        other address-less records keeps a namesake who does have mail
        out of it.  In SQL because the import asks once per such record,
        and reading the whole book each time made the import quadratic
        in the size of the book.

        Folding and trimming mirror the Python comparison this replaced,
        and the order is ``list_all_contacts``'s, so the same candidate
        wins when several match.
        """
        first = first_name.strip().lower()
        last = last_name.strip().lower()
        if not (first or last):
            return None
        with self._use() as conn:
            row = conn.execute(
                """
                SELECT c.id FROM contacts c
                WHERE lower(trim(c.last_name)) = ?
                  AND lower(trim(c.first_name)) = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM contact_emails e WHERE e.contact_id = c.id
                  )
                ORDER BY c.last_name COLLATE NOCASE, c.first_name COLLATE NOCASE
                LIMIT 1
                """,
                (last, first),
            ).fetchone()
        if row is None:
            return None
        return self.get_contact(contact_id=int(str(row[0])))

    def list_all_contacts(self) -> list[Contact]:
        """Return every contact record (for export)."""
        with self._use() as conn:
            rows = conn.execute(
                "SELECT id FROM contacts "
                "ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE"
            ).fetchall()
        return self._load_contacts_by_ids([int(r[0]) for r in rows])

    def delete_contact(self, *, contact_id: int) -> None:
        """Delete a contact and its emails/aliases."""
        with self._use() as conn:
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))

    def merge_contacts(self, *, target_id: int, source_ids: list[int]) -> Contact:
        """Merge *source_ids* into *target_id* and return the result.

        Field-level: the surviving record gains everything the sources
        knew rather than only their emails and aliases.  See
        :func:`pony.contact_merge.merged_contact` for the policy and for
        what used to be silently destroyed here.

        A source that is the target, or that no longer exists, is
        skipped — both are reachable from the browser when a marked row
        was deleted between marking and merging, and neither is a reason
        to fail.
        """
        with self._use_atomic() as conn:
            target = self._load_contact_or_none(conn, target_id)
            if target is None:
                raise KeyError(f"contact not found: {target_id}")
            for src_id in source_ids:
                if src_id == target_id:
                    continue
                source = self._load_contact_or_none(conn, src_id)
                if source is None:
                    continue
                target = merged_contact(target, source)
                # Move emails and aliases before the source row goes, so
                # the unique constraints see one owner at a time.
                conn.execute(
                    """
                    UPDATE OR IGNORE contact_emails
                    SET contact_id = ? WHERE contact_id = ?
                    """,
                    (target_id, src_id),
                )
                conn.execute(
                    """
                    UPDATE OR IGNORE contact_aliases
                    SET contact_id = ? WHERE contact_id = ?
                    """,
                    (target_id, src_id),
                )
                conn.execute("DELETE FROM contacts WHERE id = ?", (src_id,))
            self._write_contact_fields(conn, target)
            # merged_contact can introduce an alias that exists in
            # neither row — the source's name, kept as an AKA rather than
            # discarded — so the alias rows need syncing, not just moving.
            for alias in target.aliases:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO contact_aliases (contact_id, alias)
                    VALUES (?, ?)
                    """,
                    (target_id, alias),
                )
        return self._load_contact(target_id)

    def _write_contact_fields(self, conn: sqlite3.Connection, contact: Contact) -> None:
        """Persist the scalar columns of an existing contact row."""
        conn.execute(
            """
            UPDATE contacts SET
                first_name = ?, last_name = ?, affix = ?, organization = ?,
                notes = ?, message_count = ?, last_seen = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                contact.first_name,
                contact.last_name,
                json.dumps(list(contact.affix)),
                contact.organization,
                contact.notes,
                contact.message_count,
                datetime_to_sql(contact.last_seen),
                datetime_to_sql(datetime.now(tz=UTC)),
                contact.id,
            ),
        )

    def harvest_contacts(self, messages: Iterable[IndexedMessage]) -> None:
        """Bulk-harvest addresses from *messages*."""
        with self._use_atomic() as conn:
            for message in messages:
                _harvest_message_contacts(conn, message)

    def _load_contact_or_none(
        self, conn: sqlite3.Connection, contact_id: int
    ) -> Contact | None:
        """Load one contact on an existing connection, or None.

        Reads the caller's connection directly, because the caller is
        part-way through a transaction on it and is asking about a row
        that transaction has written.  ``_load_contacts_by_ids`` would
        answer correctly today -- a thread's calls share its one
        connection -- but it is three queries to settle one row, and it
        is one ``connection()`` away from being a different snapshot
        again.
        """
        row = conn.execute(
            """
            SELECT id, first_name, last_name, affix, organization,
                   notes, message_count, last_seen, created_at, updated_at
            FROM contacts WHERE id = ?
            """,
            (contact_id,),
        ).fetchone()
        if row is None:
            return None
        email_rows = conn.execute(
            "SELECT email_address FROM contact_emails WHERE contact_id = ?"
            " ORDER BY rowid",
            (contact_id,),
        ).fetchall()
        alias_rows = conn.execute(
            "SELECT alias FROM contact_aliases WHERE contact_id = ? ORDER BY alias",
            (contact_id,),
        ).fetchall()
        return _build_contact(tuple(row), list(email_rows), list(alias_rows))

    def _load_contact(self, contact_id: int) -> Contact:
        """Load a full contact record by id."""
        results = self._load_contacts_by_ids([contact_id])
        if not results:
            raise KeyError(f"contact not found: {contact_id}")
        return results[0]

    def _load_contacts_by_ids(self, ids: list[int]) -> list[Contact]:
        """Batch-load contacts with their emails and aliases in 3 queries.

        Returns them in the order *ids* were given.  Callers compute an
        order that matters — ``search_contacts`` ranks by how often you
        write to someone, ``list_all_contacts`` by name — and a bare
        ``WHERE id IN (…)`` returns rowid order, which silently replaced
        both with "whichever contact was created first".
        """
        if not ids:
            return []
        rows: list[tuple[object, ...]] = []
        email_rows: list[tuple[object, ...]] = []
        alias_rows: list[tuple[object, ...]] = []
        with self._use() as conn:
            # In runs, because SQLite caps host parameters per statement
            # and the id list is as long as the address book.  The cap is
            # a build option -- 999 before SQLite 3.32, 32,766 by default
            # since -- and past it a whole-book listing or export raised
            # instead of answering.
            for chunk in chunked(ids):
                placeholders = ",".join("?" * len(chunk))
                rows.extend(
                    conn.execute(
                        f"""
                        SELECT id, first_name, last_name, affix, organization,
                               notes, message_count, last_seen, created_at,
                               updated_at
                        FROM contacts WHERE id IN ({placeholders})
                        """,  # noqa: S608
                        chunk,
                    ).fetchall()
                )
                email_rows.extend(
                    conn.execute(
                        f"""
                        SELECT contact_id, email_address
                        FROM contact_emails WHERE contact_id IN ({placeholders})
                        """,  # noqa: S608
                        chunk,
                    ).fetchall()
                )
                alias_rows.extend(
                    conn.execute(
                        f"""
                        SELECT contact_id, alias
                        FROM contact_aliases WHERE contact_id IN ({placeholders})
                        """,  # noqa: S608
                        chunk,
                    ).fetchall()
                )

        emails_by_id: dict[int, list[str]] = {}
        for cid, addr in email_rows:
            emails_by_id.setdefault(int(str(cid)), []).append(str(addr))
        aliases_by_id: dict[int, list[str]] = {}
        for cid, alias in alias_rows:
            aliases_by_id.setdefault(int(str(cid)), []).append(str(alias))

        by_id: dict[int, Contact] = {}
        for row in rows:
            cid = int(str(row[0]))
            by_id[cid] = _build_contact(
                row,
                [(e,) for e in emails_by_id.get(cid, [])],
                [(a,) for a in aliases_by_id.get(cid, [])],
            )
        # Restore the caller's order; SQLite returned rowid order.
        return [by_id[cid] for cid in ids if cid in by_id]

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


def load_contacts_for_backup(*, database_path: Path) -> list[Contact]:
    """Read contacts from *database_path* without going through ``initialize``.

    The schema-mismatch recovery path needs to back up contacts out of a
    database that :meth:`SqliteIndexRepository.initialize` now refuses to
    open.  The ``contacts`` / ``contact_emails`` / ``contact_aliases``
    tables have the same shape across every released schema version, so
    a direct read is safe.  Returns ``[]`` if the file or tables are
    missing — the caller can still reset.
    """
    if not database_path.exists():
        return []
    conn = sqlite3.connect(database_path, timeout=10)
    try:
        has_contacts = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contacts'",
            ).fetchone()
            is not None
        )
        if not has_contacts:
            return []
        rows = conn.execute(
            """
            SELECT id, first_name, last_name, affix, organization,
                   notes, message_count, last_seen, created_at, updated_at
            FROM contacts
            """,
        ).fetchall()
        emails = conn.execute(
            "SELECT contact_id, email_address FROM contact_emails",
        ).fetchall()
        aliases = conn.execute(
            "SELECT contact_id, alias FROM contact_aliases",
        ).fetchall()
    finally:
        conn.close()

    emails_by_id: dict[int, list[str]] = {}
    for cid, addr in emails:
        emails_by_id.setdefault(int(str(cid)), []).append(str(addr))
    aliases_by_id: dict[int, list[str]] = {}
    for cid, alias in aliases:
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


def _sql_datetime(value: object) -> datetime:
    """Read back a timestamp from a column that cannot be NULL."""
    return cast("datetime", sql_to_datetime(str(value)))


def _sql_datetime_or_none(value: object) -> datetime | None:
    """Read back a timestamp from a nullable column."""
    return sql_to_datetime(str(value)) if value else None


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


def _contact_fts_query(text: str) -> str:
    """Build a prefix query whose terms may match different FTS columns.

    Contact names are stored as separate ``first_name`` and ``last_name``
    columns.  A single FTS phrase cannot cross that column boundary, so a
    query such as ``"Marina Nunez"*`` misses a contact whose first name is
    Marina and whose last name is Nunez Robles.  Combining individually
    quoted tokens lets each one match any indexed contact field while keeping
    user-supplied FTS operators inert.  Only the final token is a prefix.
    """
    tokens = text.split()
    return " AND ".join(
        _fts5_query(token, prefix=index == len(tokens) - 1)
        for index, token in enumerate(tokens)
    )


def _fts5_terms(text: str) -> list[str]:
    """One safely-quoted FTS5 phrase per whitespace-separated token.

    Splitting matters: a single phrase requires the words to appear
    adjacently and in the same column, so ``invoice paid`` found nothing
    unless a message contained exactly that pair in that order.  Callers
    combine the terms with ``AND`` instead, which is what someone typing
    two words means.  Only the last token is a prefix, matching the
    contact search.
    """
    tokens = text.split()
    return [
        _fts5_query(token, prefix=index == len(tokens) - 1)
        for index, token in enumerate(tokens)
    ]


def _fts5_field_match(column: str, text: str) -> str:
    """``AND`` every term in *text* against one FTS column."""
    return " AND ".join(f"{column}:{term}" for term in _fts5_terms(text))


def _build_fts_match(query: SearchQuery) -> str:
    """Build an FTS5 MATCH expression from a :class:`SearchQuery`.

    Returns ``""`` when every field is empty — callers should then fall
    back to a plain ``SELECT`` without a MATCH clause.
    """
    parts: list[str] = []
    if query.from_address.strip():
        parts.append(_fts5_field_match("sender", query.from_address))
    if query.to_address.strip():
        parts.append(_fts5_field_match("recipients", query.to_address))
    if query.cc_address.strip():
        parts.append(_fts5_field_match("cc", query.cc_address))
    if query.subject.strip():
        parts.append(_fts5_field_match("subject", query.subject))
    if query.body.strip():
        parts.append(_fts5_field_match("body_text", query.body))
    if query.text.strip():
        parts.append(
            " AND ".join(
                f"(subject:{term} OR body_text:{term})"
                for term in _fts5_terms(query.text)
            )
        )
    return " AND ".join(parts)


def _create_fts_tables(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual tables backing message and contact search."""
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            sender, recipients, cc, subject, body_text,
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
    # Contact FTS triggers are dropped and recreated on every initialize() so
    # that changes to the trigger bodies (e.g. bug fixes) take effect on
    # existing databases without a schema-version bump.
    for name in (
        "contacts_ai",
        "contacts_ad",
        "contacts_au",
        "contact_emails_ai",
        "contact_emails_ad",
        "contact_emails_au",
        "contact_aliases_ai",
        "contact_aliases_ad",
        "contact_aliases_au",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")  # noqa: S608

    # messages <-> messages_fts (external-content mode, standard pattern).
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(
                rowid, sender, recipients, cc, subject, body_text
            ) VALUES (
                new.rowid, new.sender, new.recipients, new.cc,
                new.subject, new.body_text
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(
                messages_fts, rowid, sender, recipients, cc, subject, body_text
            ) VALUES (
                'delete', old.rowid, old.sender, old.recipients, old.cc,
                old.subject, old.body_text
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(
                messages_fts, rowid, sender, recipients, cc, subject, body_text
            ) VALUES (
                'delete', old.rowid, old.sender, old.recipients, old.cc,
                old.subject, old.body_text
            );
            INSERT INTO messages_fts(
                rowid, sender, recipients, cc, subject, body_text
            ) VALUES (
                new.rowid, new.sender, new.recipients, new.cc,
                new.subject, new.body_text
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
    contact_refresh = """
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
        """
        CREATE TRIGGER IF NOT EXISTS contacts_au AFTER UPDATE ON contacts
        BEGIN
            DELETE FROM contacts_fts WHERE rowid = old.id;
            INSERT INTO contacts_fts(
                rowid, first_name, last_name, email_addresses, aliases
            )
            SELECT c.id, c.first_name, c.last_name,
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
            FROM contacts c WHERE c.id = new.id;
        END
        """
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
    """Hydrate a row using the order in :data:`_FULL_COLS`."""
    # 0 id  1 account_name  2 folder_name  3 uid  4 message_id
    # 5 sender  6 recipients  7 cc  8 subject  9 body_text
    # 10 storage_key  11 has_attachments
    # 12 local_flags  13 base_flags  14 server_flags  15 extra_imap_flags
    # 16 local_status  17 received_at
    # 18 trashed_at  19 synced_at  20 source_folder  21 source_uid
    uid = int(str(row[3])) if row[3] is not None else None
    extra_raw = str(row[15]) if row[15] is not None else ""
    extras: frozenset[str] = (
        frozenset(extra_raw.split(",")) - {""} if extra_raw else frozenset()
    )
    trashed_at = _sql_datetime_or_none(row[18])
    synced_at = _sql_datetime_or_none(row[19])
    return IndexedMessage(
        message_ref=MessageRef(
            account_name=str(row[1]),
            folder_name=str(row[2]),
            id=int(str(row[0])),
        ),
        message_id=str(row[4]),
        sender=str(row[5]),
        recipients=str(row[6]),
        cc=str(row[7]),
        subject=str(row[8]),
        body_text=str(row[9]),
        storage_key=str(row[10]),
        has_attachments=bool(row[11]),
        local_flags=_flags_from_csv(str(row[12])),
        base_flags=_flags_from_csv(str(row[13])),
        server_flags=_flags_from_csv(str(row[14])),
        extra_imap_flags=extras,
        local_status=MessageStatus(str(row[16])),
        received_at=_sql_datetime(row[17]),
        uid=uid,
        trashed_at=trashed_at,
        synced_at=synced_at,
        source_folder=str(row[20]) if row[20] is not None else None,
        source_uid=int(str(row[21])) if row[21] is not None else None,
    )


def _slow_path_row_from_projection(
    account_name: str,
    folder_name: str,
    row: sqlite3.Row,
) -> SlowPathRow:
    return SlowPathRow(
        message_ref=MessageRef(
            account_name=account_name,
            folder_name=folder_name,
            id=int(str(row[0])),
        ),
        message_id=str(row[1]),
        local_status=MessageStatus(str(row[2])),
        uid=int(str(row[3])) if row[3] is not None else None,
        storage_key=str(row[4]),
        local_flags=_flags_from_csv(str(row[5])),
        base_flags=_flags_from_csv(str(row[6])),
        extra_imap_flags=(
            frozenset(str(row[7]).split(",")) - {""} if row[7] else frozenset()
        ),
        source_folder=str(row[8]) if row[8] is not None else None,
        source_uid=int(str(row[9])) if row[9] is not None else None,
    )


def _summary_from_row(row: sqlite3.Row) -> FolderMessageSummary:
    # Column order (matches list_folder_message_summaries SELECT):
    #  0 id  1 account_name  2 folder_name  3 message_id
    #  4 storage_key  5 sender  6 recipients  7 subject  8 received_at
    #  9 has_attachments  10 local_flags  11 local_status
    return FolderMessageSummary(
        message_ref=MessageRef(
            account_name=str(row[1]),
            folder_name=str(row[2]),
            id=int(str(row[0])),
        ),
        message_id=str(row[3]),
        storage_key=str(row[4]),
        sender=str(row[5]),
        recipients=str(row[6]),
        subject=str(row[7]),
        received_at=_sql_datetime(row[8]),
        has_attachments=bool(row[9]),
        local_flags=_flags_from_csv(str(row[10])),
        local_status=MessageStatus(str(row[11])),
    )


def _build_contact(
    row: tuple[object, ...],
    email_rows: list[tuple[object, ...]],
    alias_rows: list[tuple[object, ...]],
) -> Contact:
    """Assemble a Contact from a contacts row + related rows."""
    last_seen_raw = row[7]
    last_seen = _sql_datetime_or_none(last_seen_raw)
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
        created_at=_sql_datetime(row[8]),
        updated_at=_sql_datetime(row[9]),
        emails=tuple(str(r[0]) for r in email_rows),
        aliases=tuple(str(r[0]) for r in alias_rows),
    )


def _looks_like_address(value: str) -> bool:
    """`contact_actions.looks_like_address`, which owns the rule.

    Imported rather than spelled again: this store and
    `harvest_addresses` are the two places that decide what counts as
    an address, and they decided differently -- so the same malformed
    header that this one refused went into the address book through the
    other.
    """
    from .contact_actions import looks_like_address

    return looks_like_address(value)


def _normalized_address(value: str) -> str:
    """`contact_actions.normalize_address`, for the same reason.

    ``contact_emails.email_address`` is a unique key, so the spelling
    the store writes has to be the spelling every lookup uses: four
    sites here wrote `.lower().strip()` and `.strip().lower()` out by
    hand, next to a function whose docstring says it exists "so two
    stores cannot disagree".
    """
    from .contact_actions import normalize_address

    return normalize_address(value)


def _harvest_message_contacts(
    conn: sqlite3.Connection, message: IndexedMessage
) -> None:
    """Parse To/Cc fields of *message* and upsert each address.

    Sender is intentionally excluded — only addresses the user wrote *to*
    (recipients and Cc) are added to the contacts store.
    """
    now = datetime_to_sql(datetime.now(tz=UTC))
    raw = ", ".join(filter(None, [message.recipients, message.cc]))
    for display_name, addr in getaddresses([raw]):
        addr = _normalized_address(addr)
        # getaddresses returns whatever it can make of a malformed
        # header, so a To: line reading "not an address at all" yields
        # the bare word "not".  Requiring an @ with something either
        # side of it keeps that out of the address book and out of
        # compose autocompletion.
        if not _looks_like_address(addr):
            continue
        display_name = clean_display_name(display_name)
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
            # Update name fields that are blank or still hold an email
            # address (a placeholder from a previous email-as-name harvest).
            if display_name.strip():
                first, last = split_display_name(display_name)
                conn.execute(
                    """
                    UPDATE contacts SET
                        first_name = CASE
                            WHEN first_name = '' OR first_name LIKE '%@%'
                            THEN ? ELSE first_name END,
                        last_name = CASE
                            WHEN last_name = '' OR last_name LIKE '%@%'
                            THEN ? ELSE last_name END
                    WHERE id = ?
                    """,
                    (first, last, int(existing[0])),
                )
        else:
            # Create new contact.
            first, last = split_display_name(display_name)
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
