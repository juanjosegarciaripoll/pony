"""Service and repository protocols."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator, Iterable, Sequence
from typing import Protocol

from .domain import (
    AccountConfig,
    Contact,
    DraftMessage,
    FlagSet,
    FolderMessageSummary,
    FolderQuickStatus,
    FolderRef,
    FolderSyncState,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    PendingPush,
    SearchQuery,
    SlowPathRow,
)


class MirrorRepository(Protocol):
    """Interface for local mirror backends.

    Methods key off a ``storage_key`` — the backend's own identifier for
    one stored message (maildir filename, mbox integer, etc.).  The mirror
    layer is intentionally unaware of RFC 5322 ``Message-ID`` headers;
    that identity is an index-side concern represented by
    :class:`MessageRef`.

    Callers that have an :class:`IndexedMessage` pass its
    ``storage_key`` attribute; callers that have just created a message
    via :meth:`store_message` already hold the returned storage_key.
    """

    def list_folders(self, *, account_name: str) -> Sequence[FolderRef]:
        """Return all folders for one account."""
        ...

    def store_message(self, *, folder: FolderRef, raw_message: bytes) -> str:
        """Store one RFC 5322 message and return its storage_key."""
        ...

    def list_messages(self, *, folder: FolderRef) -> Sequence[str]:
        """Return the storage_keys of every message in the folder."""
        ...

    def get_message_bytes(
        self, *, folder: FolderRef, storage_key: str,
    ) -> bytes:
        """Return raw RFC 5322 message bytes."""
        ...

    def set_flags(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
        flags: frozenset[MessageFlag],
    ) -> None:
        """Update local flag state."""
        ...

    def delete_message(
        self, *, folder: FolderRef, storage_key: str,
    ) -> None:
        """Delete a message from local mirror storage."""
        ...

    def move_message_to_folder(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
        target_folder: str,
    ) -> str:
        """Relocate a stored message to *target_folder*.

        Returns the new storage_key — it may change for backends whose
        keys are folder-scoped (e.g. mbox), and stays the same for
        backends whose keys are globally unique (e.g. maildir).  The raw
        bytes are preserved; flag state remains whatever the original
        file carried.
        """
        ...

    def create_folder(self, *, account_name: str, folder_name: str) -> None:
        """Create an empty folder in the mirror (idempotent).

        A folder that exists only in the local mirror signals intent: the
        sync engine detects the mirror/server mismatch and issues an IMAP
        ``CREATE`` on the next pass.
        """
        ...

    def folder_mtime_ns(self, *, folder: FolderRef) -> int:
        """Return the folder's most recent modification time, in ns.

        The value only needs to be monotonic with real filesystem
        changes inside that folder — callers compare it against a
        previous value to decide whether to re-scan.  Returns ``0``
        when the folder is missing or unreadable; ``-1`` is reserved
        for "unsupported" so callers fall back to a full scan.
        """
        ...


class IndexRepository(Protocol):
    """Interface for metadata index implementations."""

    @contextlib.contextmanager
    def connection(self) -> Generator[None]:
        """Hold a single connection for a batch of operations.

        Implementations that support connection reuse should override this
        to keep one connection open for the duration of the block and
        commit on clean exit.  The default is a no-op (each call uses its
        own connection as before).
        """
        yield

    def initialize(self) -> None:
        """Create required schema if it does not exist."""
        ...

    def insert_message(self, *, message: IndexedMessage) -> IndexedMessage:
        """Insert a new row and return it with its assigned ``id``.

        Use for rows that have no row id yet (``message_ref.id == 0``):
        first ingestion from the server, local compose, or pending
        moves not yet persisted.
        """
        ...

    def update_message(self, *, message: IndexedMessage) -> None:
        """Persist changes to an existing row keyed by ``message_ref.id``."""
        ...

    def upsert_message(self, *, message: IndexedMessage) -> IndexedMessage:
        """Insert when ``message_ref.id <= 0``, update otherwise.

        Convenience for call sites that don't care which path applies.
        Returns the row with its (possibly newly assigned) ``id``.
        """
        ...

    def delete_message(self, *, message_ref: MessageRef) -> None:
        """Remove one message row by ``message_ref.id``."""
        ...

    def purge_expired_trash(
        self, *, account_name: str, retention_days: int
    ) -> list[tuple[FolderRef, str]]:
        """Delete trashed messages older than *retention_days*.

        Returns ``[(folder_ref, storage_key), ...]`` so the caller can
        remove the corresponding mirror files.
        """
        ...

    def list_indexed_accounts(self) -> list[str]:
        """Return all account names that have data in the index."""
        ...

    def purge_account(self, *, account_name: str) -> None:
        """Remove all data for one account from every table."""
        ...

    def purge_stale_folders(
        self,
        *,
        account_name: str,
        active_folders: frozenset[str],
    ) -> list[str]:
        """Remove sync/server state for folders not in *active_folders*.

        Returns the list of folder names that were purged.
        """
        ...

    def unread_counts_by_folder(
        self, *, account_name: str
    ) -> dict[str, int]:
        """Return ``{folder_name: unread_count}`` for one account.

        Folders with zero unread are omitted from the mapping; callers
        treat a missing entry as ``0``.  One GROUP BY query, no Python
        row materialisation.
        """
        ...

    def get_message(self, *, message_ref: MessageRef) -> IndexedMessage | None:
        """Return one indexed message by ``message_ref.id``, or None."""
        ...

    def find_messages_by_message_id(
        self,
        *,
        account_name: str,
        message_id: str,
        folder_name: str | None = None,
    ) -> Sequence[IndexedMessage]:
        """Return every row with the given RFC 5322 ``Message-ID:`` value.

        Identity is row-keyed, so a single Message-ID may legitimately
        match multiple rows: an alias delivered the same body twice, a
        message stored both in INBOX and an aggregate folder, etc.
        Callers (CLI, MCP) disambiguate by row id, folder, or
        timestamp.  ``folder_name`` narrows the search; ``None``
        searches the whole account.  Empty Message-IDs never match.
        """
        ...

    def list_folder_messages(self, *, folder: FolderRef) -> Sequence[IndexedMessage]:
        """Return indexed messages from one folder."""
        ...

    def list_folder_message_summaries(
        self, *, folder: FolderRef, active_only: bool = True
    ) -> Sequence[FolderMessageSummary]:
        """Return a narrow projection of messages in one folder for list display.

        Loads only the columns the folder list renders — no recipients,
        cc, body_preview, base_flags, server_flags, extra_imap_flags,
        trashed_at or synced_at — and skips their parsing cost.
        ``active_only`` filters to ``local_status='active'`` in SQL.
        Ordered by ``received_at`` descending.
        """
        ...

    def mark_folder_read(self, *, folder: FolderRef) -> int:
        """Add the SEEN flag to every active message in *folder* that lacks it.

        Returns the number of rows updated.  Implemented as a single SQL
        UPDATE so the caller doesn't have to materialise every message.
        """
        ...

    def search(
        self, *, query: SearchQuery, account_name: str | None
    ) -> Sequence[IndexedMessage]:
        """Run a metadata search query."""
        ...

    # ------------------------------------------------------------------
    # Folder sync state
    # ------------------------------------------------------------------

    def record_folder_sync_state(self, *, state: FolderSyncState) -> None:
        """Persist the sync watermark for one folder."""
        ...

    def get_folder_sync_state(
        self,
        *,
        account_name: str,
        folder_name: str,
    ) -> FolderSyncState | None:
        """Load the sync watermark for one folder, or None if never synced."""
        ...

    def list_folder_sync_states(
        self, *, account_name: str
    ) -> Sequence[FolderSyncState]:
        """Return all sync watermarks for one account."""
        ...

    # ------------------------------------------------------------------
    # UID / server-state queries (unified in the messages table)
    # ------------------------------------------------------------------

    def count_uids_for_folder(
        self, *, account_name: str, folder_name: str
    ) -> int:
        """Return the number of rows with ``uid IS NOT NULL`` for one folder.

        Used by the sync fast-path to compare against the server's
        ``STATUS MESSAGES`` count.  Includes trashed rows whose delete has
        not yet been pushed — those still occupy a slot server-side.
        """
        ...

    def list_folder_uids(
        self, *, account_name: str, folder_name: str
    ) -> set[int]:
        """Return the set of UIDs known locally for one folder.

        Used by the planner's fast-path UID-set check (compare against
        ``STATUS MESSAGES`` count) and by the slow-path UID diff.  Only
        rows with a non-NULL ``uid`` are included.
        """
        ...

    def list_folder_push_candidates(
        self, *, account_name: str, folder_name: str
    ) -> Sequence[PendingPush]:
        """Return rows needing a server-side push, SQL-filtered.

        A row qualifies when any of:

        - ``local_status='trashed'`` (pending delete / restore), or
        - ``local_status='active' AND uid IS NULL`` (pending append), or
        - ``local_status='pending_move'`` (move source recorded), or
        - ``local_status='active' AND uid IS NOT NULL AND
          local_flags != base_flags`` (flag drift).

        Quiescent folders return zero rows so the planner avoids
        hydrating 17k-row ``IndexedMessage``s to find no work.
        """
        ...

    def list_folder_slow_path_rows(
        self, *, account_name: str, folder_name: str
    ) -> Sequence[SlowPathRow]:
        """Return the narrow per-row projection the slow-path planner uses.

        ``_plan_folder`` only reads seven of nineteen columns, so
        hydrating full ``IndexedMessage`` rows for every message in the
        folder burns datetime parsing and flag-set construction on
        columns the planner never looks at.  This projection keeps the
        slow path linear in rows but drops the per-row constant.
        """
        ...

    def list_folder_base_flags(
        self, *, account_name: str, folder_name: str
    ) -> dict[int, tuple[frozenset[MessageFlag], frozenset[str]]]:
        """Return ``{uid: (base_flags, extra_imap_flags)}`` for UID-bearing rows.

        Used by the medium path to seed baseline flags for UIDs whose
        ``MODSEQ`` has not advanced — the server response only carries
        the changed UIDs, so the planner needs the cached baseline for
        the rest.  A two-column projection replaces a full-row hydration
        that was measured at 4-5s on a 100k-row mirror.
        """
        ...

    def clear_uids_for_folder(
        self, *, account_name: str, folder_name: str
    ) -> None:
        """NULL out uid, server_flags, extra_imap_flags, synced_at for a folder.

        Called when UIDVALIDITY changes and the UID epoch is invalid.
        """
        ...

class ImapClientSession(Protocol):
    """Interface for one authenticated IMAP session.

    Abstracts the wire protocol so the sync engine can be tested against
    a fake session without a real IMAP server.
    """

    def list_folders(self) -> Sequence[str]:
        """Return all mailbox names visible to this account."""
        ...

    def get_uid_validity(self, folder_name: str) -> int:
        """SELECT the folder and return its UIDVALIDITY.

        Always issues a SELECT so callers get a fresh value.  Subsequent
        fetch/store calls for the same folder reuse the selection without an
        extra round-trip; a different folder triggers a new SELECT.
        """
        ...

    def folder_quick_status(self, folder_name: str) -> FolderQuickStatus:
        """Return UIDVALIDITY / UIDNEXT / MESSAGES [/ HIGHESTMODSEQ] via STATUS.

        One cheap roundtrip that does **not** require a SELECT.  The sync
        planner uses this to short-circuit the full metadata scan on
        folders whose watermark and message count still match the local
        snapshot.  ``highest_modseq`` is ``None`` when the server does not
        advertise ``CONDSTORE``.
        """
        ...

    def fetch_uid_to_message_id(
        self, folder_name: str
    ) -> dict[int, tuple[str, FlagSet]]:
        """Return a mapping of UID → (Message-ID, flags) for all messages.

        An empty string is returned for messages that have no Message-ID
        header; the sync engine synthesises an ID in that case.
        """
        ...

    def fetch_flags(
        self, folder_name: str, uids: Sequence[int]
    ) -> dict[int, FlagSet]:
        """Return a mapping of UID → (known_flags, extra_imap_flags)."""
        ...

    def fetch_flags_changed_since(
        self, folder_name: str, modseq: int,
    ) -> dict[int, FlagSet]:
        """Fetch flags for messages whose state changed since ``modseq``.

        Issues ``UID FETCH 1:* (FLAGS) (CHANGEDSINCE modseq)`` (RFC 7162
        CONDSTORE).  Returns only messages whose ``MODSEQ`` has advanced
        past the given value — typically a tiny subset of the folder.
        Callers must check for CONDSTORE support first (via
        :meth:`folder_quick_status` returning a non-None
        ``highest_modseq``) before using this method.
        """
        ...

    def fetch_message_bytes(self, folder_name: str, uid: int) -> bytes:
        """Fetch the full RFC 5322 message for one UID."""
        ...

    def fetch_messages_batch(
        self, folder_name: str, uids: Sequence[int],
    ) -> dict[int, bytes]:
        """Fetch full RFC 5322 messages for multiple UIDs."""
        ...

    def store_flags(
        self,
        folder_name: str,
        uid: int,
        flags: frozenset[MessageFlag],
        extra_imap_flags: frozenset[str] = frozenset(),
    ) -> None:
        """Replace the flag set for one message on the server.

        Uses an absolute STORE (not +FLAGS / -FLAGS) so replay is safe.
        *extra_imap_flags* are custom server flags that must be preserved.
        """
        ...

    def append_message(
        self,
        folder_name: str,
        raw_message: bytes,
        flags: frozenset[MessageFlag],
        extra_imap_flags: frozenset[str] = frozenset(),
    ) -> int | None:
        """Upload a message via IMAP APPEND.

        Returns the new UID assigned by the server when the response
        carries ``APPENDUID`` (RFC 4315 / UIDPLUS), otherwise ``None``.
        Callers without UIDPLUS support fall back to a UID SEARCH on
        the inserted Message-ID, or wait for the next sync to discover
        the UID.
        """
        ...

    def mark_deleted(self, folder_name: str, uid: int) -> None:
        """Set the \\Deleted flag on one message."""
        ...

    def expunge(self, folder_name: str) -> None:
        """Expunge all \\Deleted messages in the given folder."""
        ...

    def move_message(
        self, source_folder: str, uid: int, target_folder: str,
    ) -> int | None:
        """Move one message from *source_folder* to *target_folder*.

        Uses IMAP ``UID MOVE`` (RFC 6851) when the server advertises
        the ``MOVE`` capability, otherwise falls back to ``UID COPY``
        + ``STORE +FLAGS \\Deleted`` + ``EXPUNGE`` on the source.

        Returns the new UID in *target_folder* when the server's
        response carries ``COPYUID``, otherwise ``None``.  Callers
        record the new UID on the local row to avoid re-emitting the
        move on the next sync.
        """
        ...

    def create_folder(self, folder_name: str) -> None:
        """Create a folder on the server (idempotent).

        Returns immediately when the folder already exists.  Used by the
        sync engine to propagate locally-created folders upstream.
        """
        ...

    def logout(self) -> None:
        """Close the session cleanly."""
        ...


class CredentialsProvider(Protocol):
    """Interface for resolving account passwords at runtime."""

    def get_password(self, *, account_name: str) -> str:
        """Return the password for the named account."""
        ...


class SyncService(Protocol):
    """Interface for synchronization workflows."""

    def sync(self, *, account_name: str | None = None) -> None:
        """Synchronize one account or all accounts."""
        ...


class SendService(Protocol):
    """Interface for SMTP sending workflows."""

    def save_draft(self, *, account_name: str, draft: DraftMessage) -> str:
        """Persist a draft and return its identifier."""
        ...

    def send(self, *, account_name: str, draft: DraftMessage) -> str:
        """Send a draft and return a provider message identifier."""
        ...


class ContactRepository(Protocol):
    """Interface for the person-centric contacts store."""

    def upsert_contact(self, *, contact: Contact) -> Contact:
        """Insert or update a contact record.  Returns the saved contact."""
        ...

    def find_contact_by_email(self, *, email_address: str) -> Contact | None:
        """Look up a contact by one of its email addresses."""
        ...

    def search_contacts(self, *, prefix: str, limit: int = 10) -> list[Contact]:
        """Search contacts by name, alias, or email address."""
        ...

    def list_all_contacts(self) -> list[Contact]:
        """Return every contact record (for export)."""
        ...

    def delete_contact(self, *, contact_id: int) -> None:
        """Delete a contact and its emails/aliases."""
        ...

    def merge_contacts(self, *, target_id: int, source_ids: list[int]) -> Contact:
        """Merge *source_ids* into *target_id*.

        Emails and aliases from the sources are added to the target.
        Message counts are summed.  Source records are deleted.
        Returns the merged contact.
        """
        ...

    def harvest_contacts(self, messages: Iterable[IndexedMessage]) -> None:
        """Extract addresses from *messages* and upsert them into the store."""
        ...


type ImapSessionFactory = Callable[[AccountConfig, str], ImapClientSession]
