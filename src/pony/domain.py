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
class AccountConfig:
    """Account configuration used by sync and send services."""

    name: str
    email_address: str
    imap_host: str
    smtp_host: str
    username: str
    credentials_source: CredentialsSource
    mirror: MirrorConfig
    imap_port: int = 993
    imap_ssl: bool = True
    smtp_port: int = 465
    smtp_ssl: bool = True
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


@dataclass(frozen=True, slots=True)
class LocalAccountConfig:
    """Local-only account backed by a mirror directory (no IMAP/SMTP).

    Use this when you want Pony to read from a local Maildir or mbox tree
    that is managed by another tool (e.g. offlineimap, getmail, procmail).
    The sync command skips local accounts; composing is still available but
    sending requires an SMTP-capable account.
    """

    name: str
    email_address: str
    mirror: MirrorConfig
    # Composer overrides — same semantics as AccountConfig
    sent_folder: str | None = None
    drafts_folder: str | None = None
    markdown_compose: bool = False
    signature: str | None = None


type AnyAccount = AccountConfig | LocalAccountConfig


@dataclass(frozen=True, slots=True)
class McpConfig:
    """Configuration for the embedded MCP HTTP server."""

    host: str = "127.0.0.1"
    port: int = 8765


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
    # When set, start the MCP HTTP server automatically with `pony tui`.
    mcp: McpConfig | None = None


@dataclass(frozen=True, slots=True)
class FolderRef:
    """A logical mail folder reference."""

    account_name: str
    folder_name: str


@dataclass(frozen=True, slots=True)
class MessageRef:
    """A message identity scoped to an account and folder."""

    account_name: str
    folder_name: str
    message_id: str


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
    emails: tuple[str, ...]        # all email addresses (lowercase)
    affix: tuple[str, ...] = ()    # titles/suffixes: "Dr.", "Jr."
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
    folder has never been successfully selected.
    """

    account_name: str
    folder_name: str
    uid_validity: int
    highest_uid: int
    synced_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))



class OperationType(StrEnum):
    """Mutation operations queued for remote reconciliation."""

    DELETE = "delete"
    MARK_READ = "mark-read"
    MARK_UNREAD = "mark-unread"
    FLAG = "flag"
    UNFLAG = "unflag"


@dataclass(frozen=True, slots=True)
class PendingOperation:
    """A deferred remote mutation operation."""

    operation_id: str
    account_name: str
    message_ref: MessageRef
    operation_type: OperationType
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


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

    ``uid`` is the IMAP UID for this message in its folder.  It is
    ``None`` for local-only messages (drafts, local accounts) and for
    messages whose UIDVALIDITY has been reset (UIDs cleared).

    ``local_flags`` is what the user intends the flags to be.
    ``base_flags`` is the server's flag state at the last successful sync;
    it is used as the common ancestor in a three-way merge when both sides
    have changed flags independently.
    ``server_flags`` records the flags as last reported by the IMAP server.
    ``extra_imap_flags`` preserves opaque server-side flags (keywords,
    ``$Important``, etc.) that Pony does not model but must round-trip
    through STORE operations.
    ``local_status`` tracks the message through its local lifecycle.
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
    uid: int | None = None
    server_flags: frozenset[MessageFlag] = frozenset()
    extra_imap_flags: frozenset[str] = frozenset()
    has_attachments: bool = False
    trashed_at: datetime | None = None
    synced_at: datetime | None = None
