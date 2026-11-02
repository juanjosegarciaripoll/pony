"""Finding duplicate copies of one message inside a folder.

Two rows are duplicates only when they share a ``Message-ID`` *and*
their stored copies are byte-identical.  The header alone cannot settle
it: `Message-ID` is display-only in this index and duplicates are
legitimate — resends and mailing-list copies produce them routinely
(see ``docs/synchronization.md``) — while both callers of this module go
on to expunge mail from the server.  A group whose copies differ, or
whose mirror file cannot be read, is reported and left alone.

Planning is separated from applying on purpose: the CLI prints the plan
and reads a typed confirmation, the TUI puts it in a dialog, and only
then does either write rows.  ``trashed_duplicate`` is the row
projection they share.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import TYPE_CHECKING

from .domain import IndexedMessage, MessageFlag, MessageStatus

if TYPE_CHECKING:
    from datetime import datetime

    from .domain import FolderRef
    from .protocols import IndexRepository, MirrorRepository


@dataclasses.dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """One set of byte-identical copies: the keeper and the rest."""

    label: str
    keep: IndexedMessage
    trash: tuple[IndexedMessage, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class DuplicatePlan:
    """What a dedupe run would do to a folder, and what it would not."""

    groups: tuple[DuplicateGroup, ...] = ()
    #: Message-IDs that repeat with copies that are *not* identical.
    unmatched_ids: tuple[str, ...] = ()
    #: Rows skipped because their mirror file could not be read.
    unreadable: int = 0

    @property
    def message_count(self) -> int:
        """How many rows the plan would mark TRASHED."""
        return sum(len(group.trash) for group in self.groups)

    @property
    def group_count(self) -> int:
        return len(self.groups)


def _keep_first(message: IndexedMessage) -> tuple[int, int, int, datetime, int]:
    """Sort key putting the most informative copy first."""
    useful = message.local_flags - {MessageFlag.DELETED}
    return (
        -len(useful),
        -int(MessageFlag.ANSWERED in useful),
        -int(MessageFlag.FLAGGED in useful),
        message.received_at,
        message.message_ref.id,
    )


def plan_duplicates(
    *,
    index: IndexRepository,
    mirror: MirrorRepository,
    folder: FolderRef,
) -> DuplicatePlan:
    """Work out which copies in *folder* are redundant.

    Only ACTIVE rows carrying a UID and a non-empty ``Message-ID`` are
    considered: a row without a UID is a local-only draft the server has
    never seen, and TRASHED / PENDING_MOVE rows are already in flux.
    """
    candidates: dict[str, list[IndexedMessage]] = {}
    for message in index.list_folder_messages(folder=folder):
        if not message.message_id:
            continue
        if message.local_status != MessageStatus.ACTIVE:
            continue
        if message.uid is None:
            continue
        candidates.setdefault(message.message_id, []).append(message)

    groups: list[DuplicateGroup] = []
    unmatched: list[str] = []
    unreadable = 0

    for message_id, rows in sorted(candidates.items()):
        if len(rows) < 2:
            continue
        by_digest: dict[str, list[IndexedMessage]] = {}
        for message in rows:
            digest = _content_digest(mirror=mirror, folder=folder, message=message)
            if digest is None:
                unreadable += 1
                continue
            by_digest.setdefault(digest, []).append(message)

        identical = [rows for rows in by_digest.values() if len(rows) > 1]
        if not identical:
            unmatched.append(message_id)
            continue
        for n, subgroup in enumerate(identical, start=1):
            ordered = sorted(subgroup, key=_keep_first)
            # Several distinct messages can share one Message-ID, each
            # with duplicates of its own; number them so the plan names
            # which set is which.
            label = message_id if len(identical) == 1 else f"{message_id} [{n}]"
            groups.append(
                DuplicateGroup(
                    label=label,
                    keep=ordered[0],
                    trash=tuple(ordered[1:]),
                )
            )

    return DuplicatePlan(
        groups=tuple(groups),
        unmatched_ids=tuple(unmatched),
        unreadable=unreadable,
    )


def _content_digest(
    *,
    mirror: MirrorRepository,
    folder: FolderRef,
    message: IndexedMessage,
) -> str | None:
    """SHA-256 of the message as stored, or ``None`` when unreadable."""
    try:
        raw = mirror.get_message_bytes(
            folder=folder,
            storage_key=message.storage_key,
            message_id=message.message_id,
        )
    except Exception:  # noqa: BLE001
        return None
    return hashlib.sha256(raw).hexdigest()


def trashed_duplicate(
    message: IndexedMessage,
    *,
    now: datetime,
) -> IndexedMessage:
    """The row for a redundant copy, marked for deletion.

    The row keeps its UID, which is what lets the sync planner emit
    ``PushDeleteOp`` and expunge the copy server-side; retention reaps
    the local row later.
    """
    return dataclasses.replace(
        message,
        local_status=MessageStatus.TRASHED,
        trashed_at=now,
    )
