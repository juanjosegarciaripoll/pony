"""Core domain models for Pony Express."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

MirrorFormat = Literal["maildir", "mbox"]
CredentialsSource = Literal["plaintext", "env", "command", "encrypted"]
AccountType = Literal["imap", "local"]


@dataclass(frozen=True, slots=True)
class MirrorConfig:
    """Local mirror configuration for one account."""

    path: Path
    format: MirrorFormat
    trash_retention_days: int = 30


@dataclass(frozen=True, slots=True)
class FolderConfig:
    """Per-account folder sync policy.

    ``include``: if non-empty, only these folders are synchronised.  Empty
    means sync all folders the server exposes.

    ``exclude``: folders that are never synchronised, even if listed in
    ``include``.  Takes precedence over both ``include`` and ``read_only``.

    ``read_only``: folders synchronised server-to-local only.  Local flag
    changes and deletions are never pushed back to the server.  A folder in
    ``read_only`` is automatically included in sync even when ``include`` is
    non-empty, unless it also appears in ``exclude``.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    read_only: tuple[str, ...] = ()

    def should_sync(self, folder_name: str) -> bool:
        """Return True if this folder should be synchronised at all.

        ``include`` and ``read_only`` override ``exclude``: a folder
        matched by any of them is synced even if also excluded.  When
        ``include`` is non-empty, folders not matched by ``include``
        or ``read_only`` are excluded by default.
        """
        if self._matches(self.include, folder_name):
            return True
        if self._matches(self.read_only, folder_name):
            return True
        if self._matches(self.exclude, folder_name):
            return False
        return not self.include

    def is_read_only(self, folder_name: str) -> bool:
        """Return True if this folder is server-to-local only."""
        return self._matches(self.read_only, folder_name)

    @staticmethod
    def _matches(patterns: tuple[str, ...], folder_name: str) -> bool:
        for pat in patterns:
            # Accept glob-style * as a convenience for .* in regex.
            if pat == "*":
                return True
            if re.fullmatch(pat, folder_name):
                return True
        return False


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """SMTP connection settings for outgoing mail.

    Credentials (``username`` / password source) stay at the account level
    because they are shared with IMAP auth for ``AccountConfig`` and are
    set independently for ``LocalAccountConfig``; this dataclass captures
    only the wire-level SMTP bits.
    """

    host: str
    port: int = 465
    ssl: bool = True


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """Account configuration used by sync and send services."""

    name: str
    email_address: str
    imap_host: str
    smtp: SmtpConfig
    username: str
    credentials_source: CredentialsSource
    mirror: MirrorConfig
    imap_port: int = 993
    imap_ssl: bool = True
    password: str | None = None
    password_command: tuple[str, ...] | None = None
    folders: FolderConfig = field(default_factory=FolderConfig)
    # Composer: folder name overrides (None = auto-discover by name matching)
    sent_folder: str | None = None
    drafts_folder: str | None = None
    # Archive target for the `A` key in the TUI.  When set, archiving a
    # message locally moves it into this folder; the next sync propagates
    # the move to the server.  None disables the archive action.
    archive_folder: str | None = None
    # Composer: default Markdown composition mode for this account
    markdown_compose: bool = False
    # Composer: signature text appended after quoted content (None = no signature)
    signature: str | None = None

    @property
    def can_send(self) -> bool:
        """True when the account has enough config to send via SMTP.

        Always True for ``AccountConfig`` — the ``smtp`` block is
        required.  Defined symmetrically with ``LocalAccountConfig`` so
        callers can filter with ``a.can_send`` regardless of type.
        """
        return True


@dataclass(frozen=True, slots=True)
class LocalAccountConfig:
    """Local account backed by a mirror directory, no IMAP sync.

    Use this when you want Pony to read from a local Maildir or mbox tree
    managed by another tool (offlineimap, getmail, procmail, Emacs/Gnus).
    The sync command skips local accounts.

    SMTP fields are optional.  When ``smtp`` is configured (together with
    ``username`` and a credential source), the account can send outgoing
    mail without an IMAP configuration.
    """

    name: str
    email_address: str
    mirror: MirrorConfig
    # Composer overrides — same semantics as AccountConfig
    sent_folder: str | None = None
    drafts_folder: str | None = None
    markdown_compose: bool = False
    signature: str | None = None
    # Optional SMTP block + credentials for sending.  ``smtp`` is the
    # wire-level connection; ``username`` / ``credentials_source`` /
    # ``password`` / ``password_command`` provide authentication (same
    # shape as AccountConfig).  The parser enforces "all or nothing":
    # if ``smtp`` is set, ``username`` and ``credentials_source`` are
    # also required.
    smtp: SmtpConfig | None = None
    username: str | None = None
    credentials_source: CredentialsSource | None = None
    password: str | None = None
    password_command: tuple[str, ...] | None = None

    @property
    def can_send(self) -> bool:
        """True when SMTP and credentials are configured."""
        return self.smtp is not None and self.username is not None


type AnyAccount = AccountConfig | LocalAccountConfig


# The TOML format version Pony Express expects on disk.  Bumped when
# the schema changes in a way that requires user intervention.  The
# parser rejects configs that do not declare exactly this value — a
# missing or mismatched version is a loud error, not a silent migration.
CONFIG_VERSION: int = 2


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Top-level Pony Express configuration."""

    accounts: tuple[AnyAccount, ...]
    use_utf8: bool = False
    # Composer: path to external editor executable (None = use inline editor)
    editor: str | None = None
    # Composer: global default for Markdown composition mode.
    # Overridden per-account by AccountConfig.markdown_compose.
    markdown_compose: bool = False
    # Path to a BBDB v3 file.  When set, the contacts database is
    # exported to this file after every sync.
    bbdb_path: Path | None = None
    # Target directory for attachments opened/saved from the TUI.  When
    # None, resolves to ``~/Downloads`` at use time.
    downloads_path: Path | None = None
    # Textual theme name.  None means use Textual's built-in default.
    theme: str | None = None


@dataclass(frozen=True, slots=True)
class FolderRef:
    """A logical mail folder reference."""

    account_name: str
    folder_name: str


@dataclass(frozen=True, slots=True)
class MessageRef:
    """A message's row identity in the local index.

    Identity is per-row: ``id`` is the SQLite autoincrement primary key
    of the ``messages`` table, scoped by ``account_name`` and
    ``folder_name`` for clarity at call sites (the id alone is unique
    globally).  The RFC 5322 ``Message-ID:`` header is *not* part of
    identity — it lives as a display attribute on
    :class:`IndexedMessage` and may be empty or shared between rows.
    """

    account_name: str
    folder_name: str
    id: int


class MessageFlag(StrEnum):
    """Supported local message flags."""

    SEEN = "seen"
    ANSWERED = "answered"
    FLAGGED = "flagged"
    DELETED = "deleted"
    DRAFT = "draft"


class MessageStatus(StrEnum):
    """Local lifecycle status of a message."""

    ACTIVE = "active"
    TRASHED = "trashed"
    DELETED = "deleted"
    PENDING_MOVE = "pending_move"


# A set of known flags paired with opaque server-side flag strings that
# Pony doesn't model but must preserve through STORE operations.
type FlagSet = tuple[frozenset[MessageFlag], frozenset[str]]


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """Attachment metadata and addressing information."""

    part_id: str
    filename: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Contact:
    """A person-centric contact record.

    One contact can have multiple email addresses and aliases.  The
    ``id`` is an internal autoincrement key (``None`` for unsaved
    records).  Fields map to BBDB v3 for Emacs interop.
    """

    id: int | None
    first_name: str
    last_name: str
    emails: tuple[str, ...]  # all email addresses (lowercase)
    affix: tuple[str, ...] = ()  # titles/suffixes: "Dr.", "Jr."
    aliases: tuple[str, ...] = ()  # alternate names / nicknames
    organization: str = ""
    notes: str = ""
    message_count: int = 0
    last_seen: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def display_name(self) -> str:
        """Formatted full name, or empty string if both parts are blank."""
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts)

    @property
    def primary_email(self) -> str:
        """First email address, or empty string if none."""
        return self.emails[0] if self.emails else ""


@dataclass(frozen=True, slots=True)
class DraftMessage:
    """Draft message information for compose and send workflows."""

    from_address: str
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    subject: str
    body_text: str
    attachments: tuple[AttachmentRef, ...] = ()


@dataclass(frozen=True, slots=True)
class FolderSyncState:
    """Sync watermark for one IMAP folder.

    ``uid_validity`` must always be known — a value of zero indicates the
    folder has never been successfully selected.  ``uidnext`` is the
    server's ``UIDNEXT`` at the last sync's STATUS call; it may be
    greater than ``highest_uid + 1`` when UIDs have been *burned*
    (delivered then expunged or moved away), so the fast-path gate must
    compare server ``UIDNEXT`` directly against this stored value
    rather than deriving it from ``highest_uid``.  ``highest_modseq``
    is the CONDSTORE (RFC 7162) watermark; zero means either the server
    does not advertise ``CONDSTORE`` or no sync has populated it yet.
    """

    account_name: str
    folder_name: str
    uid_validity: int
    highest_uid: int
    uidnext: int = 0
    highest_modseq: int = 0
    synced_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(frozen=True, slots=True)
class FolderQuickStatus:
    """Cheap IMAP ``STATUS`` snapshot used as the sync fast-path gate.

    One ``STATUS folder (UIDVALIDITY UIDNEXT MESSAGES [HIGHESTMODSEQ])``
    roundtrip per folder, compared against the stored ``FolderSyncState``
    and local row count to decide whether the planner can skip the full
    ``FETCH 1:*`` metadata scan.  ``highest_modseq`` is ``None`` when the
    server does not advertise ``CONDSTORE``.
    """

    uid_validity: int
    uidnext: int
    messages: int
    highest_modseq: int | None = None


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """A structured search request."""

    text: str = ""
    from_address: str = ""
    to_address: str = ""
    cc_address: str = ""
    subject: str = ""
    body: str = ""
    case_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class IndexedMessage:
    """Indexed metadata stored in SQLite.

    ``message_ref.id`` is the row's primary key.  A row that has not
    yet been persisted carries ``id=0``.

    ``message_id`` is the RFC 5322 ``Message-ID:`` header, used for
    display, reply threading, and import-time duplicate detection.  It
    is not a key — multiple rows in one folder may share the same
    value.  Empty when the header was missing on import.

    ``uid`` is the IMAP UID for this message in its folder.  It is
    ``None`` for local-only rows (compose drafts, pending moves not yet
    pushed) and for messages whose UIDVALIDITY has been reset.
    ``uid_validity`` is the server's UIDVALIDITY at the moment ``uid``
    was assigned; ``0`` means UID has never been recorded.

    ``local_flags`` is what the user intends the flags to be.
    ``base_flags`` is the server's flag state at the last successful sync;
    it is used as the common ancestor in a three-way merge when both sides
    have changed flags independently.
    ``server_flags`` records the flags as last reported by the IMAP server.
    ``extra_imap_flags`` preserves opaque server-side flags (keywords,
    ``$Important``, etc.) that Pony does not model but must round-trip
    through STORE operations.
    ``local_status`` tracks the message through its local lifecycle.
    ``source_folder`` / ``source_uid`` are populated on
    ``PENDING_MOVE`` rows: the row currently lives in
    ``message_ref.folder_name`` but the server still has the message at
    ``(source_folder, source_uid)``.  Sync executes the server-side
    move and clears these fields once the new ``uid`` is known.
    ``synced_at`` records when this message was last reconciled with the
    IMAP server.
    """

    message_ref: MessageRef
    sender: str
    recipients: str
    cc: str
    subject: str
    body_preview: str
    storage_key: str  # mirror's internal key (Maildir filename / mbox integer)
    local_flags: frozenset[MessageFlag]
    base_flags: frozenset[MessageFlag]
    local_status: MessageStatus
    received_at: datetime
    message_id: str = ""
    uid: int | None = None
    uid_validity: int = 0
    server_flags: frozenset[MessageFlag] = frozenset()
    extra_imap_flags: frozenset[str] = frozenset()
    has_attachments: bool = False
    trashed_at: datetime | None = None
    synced_at: datetime | None = None
    source_folder: str | None = None
    source_uid: int | None = None


@dataclass(frozen=True, slots=True)
class FolderMessageSummary:
    """Narrow projection of ``IndexedMessage`` for the folder list view.

    The TUI message-list panel only reads a handful of fields per row
    (sender, subject, received_at, has_attachments, local_flags,
    local_status, plus identity fields).  Loading a full
    ``IndexedMessage`` for 10k+ row folders was the bottleneck on open:
    every row paid datetime parsing for three timestamp columns and
    frozenset construction for three flag columns it never displayed.
    This type carries only what the list renders.  Actions re-fetch
    the full ``IndexedMessage`` by ``message_ref`` on demand.
    """

    message_ref: MessageRef
    message_id: str
    storage_key: str
    sender: str
    subject: str
    received_at: datetime
    has_attachments: bool
    local_flags: frozenset[MessageFlag]
    local_status: MessageStatus


@dataclass(frozen=True, slots=True)
class PendingPush:
    """Narrow projection of ``messages`` for the sync planner.

    Filters to rows that need a server-side push: pending appends,
    pending moves, trashed rows, and rows with flag drift.  A SQL
    ``WHERE`` that already filters returns zero rows for a quiescent
    folder, so the cost scales with the number of *local changes*,
    not the folder size.
    """

    message_ref: MessageRef
    message_id: str
    local_status: MessageStatus
    uid: int | None
    storage_key: str
    local_flags: frozenset[MessageFlag]
    extra_imap_flags: frozenset[str]
    source_folder: str | None = None
    source_uid: int | None = None


@dataclass(frozen=True, slots=True)
class SlowPathRow:
    """Narrow projection of ``messages`` for the sync planner.

    The planner reads only the message ref, uid, local/base flag sets,
    ``extra_imap_flags``, ``storage_key``, ``local_status`` and the
    source-fields.  Hydrating a full ``IndexedMessage`` would pay a
    datetime parse and frozenset construction per row for columns the
    planner never touches.
    """

    message_ref: MessageRef
    message_id: str
    local_status: MessageStatus
    uid: int | None
    storage_key: str
    local_flags: frozenset[MessageFlag]
    base_flags: frozenset[MessageFlag]
    extra_imap_flags: frozenset[str]
    source_folder: str | None = None
    source_uid: int | None = None
