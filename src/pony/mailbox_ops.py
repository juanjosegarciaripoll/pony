"""Index-row projections for local mailbox mutations.

Pony records a local mutation by rewriting the message's index row and
letting the sync planner observe it — there is no pending-operations
table (see ``docs/synchronization.md``).  That makes the exact field set of
each rewrite part of the sync contract: forget ``uid=None`` and the
planner never notices the change; forget ``source_uid`` and a move is
pushed as an append plus a delete of the wrong message.

Both rewrites had been open-coded at each call site — the "arrived in a
folder" one in three places and the "moved to a folder" one in two — so
the contract was only as good as the least careful copy.

These functions are pure: they take a row and return the row it should
become.  Reading and writing bytes, opening index transactions, and
turning failure into a message for the user all stay with the caller,
because a CLI, a TUI and a sync engine each need to do those
differently.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from .domain import (
    FolderRef,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
)

if TYPE_CHECKING:
    from .protocols import MirrorRepository

_log = logging.getLogger(__name__)


def write_mirror_flags(
    mirror: MirrorRepository,
    *,
    folder: FolderRef,
    storage_key: str,
    flags: frozenset[MessageFlag],
) -> bool:
    """Write *flags* onto the message at *storage_key* in *mirror*.

    Takes the three things the write actually needs, so a caller holding
    a ``FolderMessageSummary`` — the narrow row the message list uses —
    does not have to re-fetch the full ``IndexedMessage`` just to record
    a flag.  See :func:`mirror_flags` for the whole-row convenience form
    and for why this is best-effort.
    """
    try:
        mirror.set_flags(folder=folder, storage_key=storage_key, flags=flags)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Could not write flags to mirror for %s: %s", storage_key, exc)
        return False
    return True


def mirror_flags(mirror: MirrorRepository, message: IndexedMessage) -> bool:
    """Write *message*'s local flags onto its file in *mirror*.

    The index is where Pony reads flags from, but it is not the only
    reader of the mirror: a Maildir or mbox tree is routinely shared with
    another MUA, and until this was called nothing ever wrote flags to
    disk, so anything Pony marked read still looked unread to everything
    else.

    Best-effort by design.  The index row is authoritative and has
    already been written by the time this runs; a mirror file that has
    been moved or removed underneath us (a sync racing the same message)
    must not turn a successful flag change into an error the user sees.
    Returns whether the write landed, for callers that want to count.

    Costs one file rename on Maildir but a full rewrite of the mailbox on
    mbox, so callers should apply it to messages the user actually
    selected rather than to a whole folder.
    """
    return write_mirror_flags(
        mirror,
        folder=FolderRef(
            account_name=message.message_ref.account_name,
            folder_name=message.message_ref.folder_name,
        ),
        storage_key=message.storage_key,
        flags=frozenset(message.local_flags),
    )


def landed_in_folder(
    message: IndexedMessage,
    *,
    target: FolderRef,
    storage_key: str,
    message_id: str | None = None,
) -> IndexedMessage:
    """The row for *message* newly arrived in *target* as local mail.

    Used when bytes have been written into *target* as a genuinely new
    message: a copy, a cross-account move, or ``pony folder mirror``.
    The result is a fresh ``ACTIVE`` row with ``id=0`` — the caller
    inserts it rather than updating in place.

    Every server-side identity is cleared, because the message does not
    exist upstream yet: no UID, no UID validity, and no server flags.
    ``uid=None`` is what makes the sync planner emit a ``PushAppendOp``.
    Nothing is carried over from a previous move either, so a row that
    was mid-move upstream does not arrive here still pointing at it.

    *message_id* replaces the row's Message-ID when the bytes were
    rewritten with a new one; pass ``None`` to keep the original.
    """
    return dataclasses.replace(
        message,
        message_ref=MessageRef(
            account_name=target.account_name,
            folder_name=target.folder_name,
            id=0,
        ),
        message_id=message.message_id if message_id is None else message_id,
        storage_key=storage_key,
        uid=None,
        uid_validity=0,
        base_flags=frozenset(),
        server_flags=frozenset(),
        extra_imap_flags=frozenset(),
        local_status=MessageStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
        source_folder=None,
        source_uid=None,
    )


def moved_to_folder(
    message: IndexedMessage,
    *,
    target: FolderRef,
    storage_key: str,
) -> IndexedMessage:
    """The row for *message* moved locally into *target* within one account.

    The same row is kept — same ``id``, same Message-ID — and marked
    ``PENDING_MOVE`` with the server-side origin recorded, so the sync
    planner can issue a ``UID MOVE`` (or append-plus-expunge) and then
    clear the source fields.  The caller updates in place.

    ``source_uid`` is the UID the message had *before* the move; without
    it the planner cannot tell the server which message to remove.
    """
    return dataclasses.replace(
        message,
        message_ref=MessageRef(
            account_name=target.account_name,
            folder_name=target.folder_name,
            id=message.message_ref.id,
        ),
        storage_key=storage_key,
        uid=None,
        server_flags=frozenset(),
        extra_imap_flags=frozenset(),
        synced_at=None,
        local_status=MessageStatus.PENDING_MOVE,
        source_folder=message.message_ref.folder_name,
        source_uid=message.uid,
    )
