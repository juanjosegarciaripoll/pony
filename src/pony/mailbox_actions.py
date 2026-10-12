"""What the mailbox actions do, with no user interface attached.

Trash, archive, copy, move, flag changes and folder creation are the
operations a reader offers on the messages in front of it.  Each one is
the same small recipe — check the account's folder policy, rewrite the
mirror, rewrite the index row, say what happened — and each one had been
written out again inside :class:`~pony.tui.screens.main_screen.MainScreen`,
interleaved with ``app.notify`` calls and table refreshes.  Archive was a
move with its own copy of the policy checks; a cross-account move was a
copy with its own copy of the byte handling.

They live here instead, over the stores rather than over widgets, and
they report through :class:`chronos.outcome.Outcome` instead of calling
into a UI.  That makes each one testable without a running app, lets a
second front end offer the same actions without reimplementing them, and
leaves exactly one place where the policy that guards a destructive move
is decided.

The row rewrites themselves stay in :mod:`pony.mailbox_ops`, which is
pure; this module is what adds the I/O and the account policy around
them.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chronos.outcome import Notice, NoticeLog, Outcome, failure, warn

from .accounts import find_imap_account
from .domain import (
    AppConfig,
    FolderRef,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
)
from .mailbox_ops import flush_mirror, landed_in_folder, moved_to_folder
from .message_copy import copy_message_bytes

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from .protocols import IndexRepository, MirrorRepository


@dataclass(frozen=True, slots=True)
class Mailboxes:
    """The stores every mailbox action works on.

    One argument instead of three, because no action needs a different
    combination of them and a front end holds all three anyway.
    """

    config: AppConfig
    index: IndexRepository
    mirrors: Mapping[str, MirrorRepository]

    def mirror(self, account_name: str) -> MirrorRepository | None:
        return self.mirrors.get(account_name)


# ----------------------------------------------------------------------
# Flags
# ----------------------------------------------------------------------


def add_flag(
    mailboxes: Mailboxes,
    message: IndexedMessage,
    flag: MessageFlag,
) -> IndexedMessage | None:
    """Add *flag* to one message, returning the row it became.

    Returns ``None`` when the flag was already set, so a caller can tell
    "nothing to repaint" from "repaint this row".  The index is the only
    store of flag state — nothing reads flags back out of the mirror, so
    writing them there would be data no one consumes.
    """
    if flag in message.local_flags:
        return None
    updated = dataclasses.replace(
        message,
        local_flags=message.local_flags | {flag},
    )
    with mailboxes.index.connection():
        mailboxes.index.upsert_message(message=updated)
    return updated


def set_flag(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    flag: MessageFlag,
    *,
    present: bool,
) -> Outcome:
    """Add or remove *flag* on every message in *messages*."""
    if present:
        return _rewrite_flags(mailboxes, messages, lambda flags: flags | {flag})
    return _rewrite_flags(mailboxes, messages, lambda flags: flags - {flag})


def toggle_flag(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    flag: MessageFlag,
) -> Outcome:
    """Flip *flag* on every message in *messages*, row by row."""
    return _rewrite_flags(
        mailboxes,
        messages,
        lambda flags: flags - {flag} if flag in flags else flags | {flag},
    )


def mark_folder_read(mailboxes: Mailboxes, folder: FolderRef) -> Outcome:
    """Clear the unread state of every message in *folder*."""
    count = mailboxes.index.mark_folder_read(folder=folder)
    if not count:
        return Outcome()
    suffix = "s" if count != 1 else ""
    return Outcome(
        changed=count,
        notices=(Notice(f"Marked {count} message{suffix} as read."),),
    )


def _rewrite_flags(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    decide: Callable[[frozenset[MessageFlag]], frozenset[MessageFlag]],
) -> Outcome:
    changed = 0
    with mailboxes.index.connection():
        for msg in messages:
            new_flags = decide(msg.local_flags)
            if new_flags == msg.local_flags:
                continue
            mailboxes.index.upsert_message(
                message=dataclasses.replace(msg, local_flags=new_flags),
            )
            changed += 1
    return Outcome(changed=changed)


# ----------------------------------------------------------------------
# Trash
# ----------------------------------------------------------------------


def trash_messages(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    *,
    now: datetime,
) -> Outcome:
    """Mark every message in *messages* as trashed in the index."""
    changed = 0
    with mailboxes.index.connection():
        for msg in messages:
            updated = dataclasses.replace(
                msg,
                local_status=MessageStatus.TRASHED,
                # Retention and the sync planner both key off this.
                # Without it a trashed row with no server UID is purged
                # outright instead of being recoverable.
                trashed_at=msg.trashed_at or now,
            )
            mailboxes.index.upsert_message(message=updated)
            changed += 1
    return Outcome(changed=changed)


# ----------------------------------------------------------------------
# Archive, move, copy
# ----------------------------------------------------------------------


def archive_messages(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    source: FolderRef,
) -> Outcome:
    """Move every message in *messages* to the account's archive folder.

    Archiving is a move whose destination comes from the account's
    ``archive_folder`` setting rather than from the user, so it answers
    that question and then runs the same move.  The moves are applied
    locally (index + mirror) immediately; the next sync pushes them to
    the server via ``UID MOVE``.
    """
    account = find_imap_account(mailboxes.config, source.account_name)
    if account is None:
        return Outcome.refused(warn("Archive requires an IMAP account."))
    target_name = account.archive_folder
    if not target_name:
        return Outcome.refused(
            failure("No archive_folder configured for this account."),
        )
    if target_name == source.folder_name:
        return Outcome()
    refusal = check_move_policy(
        mailboxes.config,
        source,
        FolderRef(account_name=source.account_name, folder_name=target_name),
        verb="archive",
        target_noun="Archive folder",
    )
    if refusal is not None:
        return Outcome.refused(refusal)
    return move_messages(
        mailboxes,
        messages,
        source,
        FolderRef(account_name=source.account_name, folder_name=target_name),
        past_verb="Archived",
        checked=True,
    )


def move_messages(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    source: FolderRef,
    target: FolderRef,
    *,
    past_verb: str = "Moved",
    checked: bool = False,
) -> Outcome:
    """Move every message in *messages* from *source* to *target*.

    Two execution paths, chosen by comparing source and target accounts:

    - **Same account** — the mirror file is renamed in place via
      ``move_message_to_folder`` and the index row is marked
      ``PENDING_MOVE``, Message-ID preserved, so the source folder's
      next sync emits ``PushMoveOp`` (``UID MOVE`` server-side).

    - **Different accounts** — no atomic cross-server IMAP move exists,
      so this decomposes into cross-account copy plus retiring the
      source.  Bytes are written to the target mirror with the original
      Message-ID preserved (distinct accounts are independent identity
      namespaces), a ``uid=NULL`` row is inserted on the target side, and
      the source is marked ``TRASHED`` for IMAP sources (so the next sync
      EXPUNGEs it) or deleted outright for local ones (which have no
      sync to relay the deletion).  Target-first ordering means an
      interruption leaves a duplicate, not a loss.

    *checked* says the caller has already run :func:`check_move_policy`,
    which is how archiving reports its refusals in its own words.
    """
    if source == target:
        return Outcome()
    mirrors = _mirror_pair(mailboxes, source, target)
    if mirrors is None:
        return Outcome.refused(failure("Missing mirror for source or target account."))
    source_mirror, target_mirror = mirrors
    if not checked:
        refusal = check_move_policy(mailboxes.config, source, target)
        if refusal is not None:
            return Outcome.refused(refusal)

    same_account = source.account_name == target.account_name
    source_account = find_imap_account(mailboxes.config, source.account_name)
    log = NoticeLog()
    moved = 0
    with mailboxes.index.connection():
        for msg in messages:
            if same_account:
                ok = _move_in_place(
                    mailboxes,
                    msg=msg,
                    mirror=source_mirror,
                    source=source,
                    target=target,
                    log=log,
                )
            else:
                ok = _move_across_accounts(
                    mailboxes,
                    msg=msg,
                    source_mirror=source_mirror,
                    target_mirror=target_mirror,
                    source=source,
                    target=target,
                    source_is_imap=source_account is not None,
                    log=log,
                )
            if ok:
                moved += 1
    if moved:
        log.info(f"{past_verb} to {_where(source, target)}{_count_suffix(moved)}.")
    return log.outcome(moved)


def copy_messages(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    source: FolderRef,
    target: FolderRef,
) -> Outcome:
    """Copy every message in *messages* from *source* into *target*.

    Copies are applied locally (index + mirror) immediately; the next
    sync pushes them to the server via ``APPEND``.  Within a single
    account the copy is given a synthetic Message-ID so the sync planner
    doesn't mistake the duplicate for a move (the planner keys
    cross-folder identity on Message-ID, and multi-folder identity is a
    deferred feature).  Across accounts the original Message-ID is
    preserved — accounts are independent identity namespaces and a true
    copy keeps IMAP thread integrity intact.
    """
    mirrors = _mirror_pair(mailboxes, source, target)
    if mirrors is None:
        return Outcome.refused(failure("Missing mirror for source or target account."))
    source_mirror, target_mirror = mirrors

    log = NoticeLog()
    copied = 0
    with mailboxes.index.connection():
        for msg in messages:
            landed = _duplicate_into(
                mailboxes,
                msg=msg,
                source_mirror=source_mirror,
                target_mirror=target_mirror,
                source=source,
                target=target,
                rewrite_message_id=target.account_name == source.account_name,
                log=log,
            )
            if landed:
                copied += 1
    if copied:
        log.info(f"Copied to {_where(source, target)}{_count_suffix(copied)}.")
    return log.outcome(copied)


def check_move_policy(
    config: AppConfig,
    source: FolderRef,
    target: FolderRef,
    *,
    verb: str = "move",
    target_noun: str = "Target folder",
) -> Notice | None:
    """Why *source* → *target* must not happen, or ``None`` if it may.

    A move is destructive on the source side, so it is refused when
    either end cannot reconcile server-side: a read-only source would
    have the message come straight back, and a target that is excluded
    from sync or read-only would strand it locally forever.  Folders on
    accounts with no IMAP side have no such policy and are never
    refused.
    """
    source_account = find_imap_account(config, source.account_name)
    if source_account is not None and source_account.folders.is_read_only(
        source.folder_name,
    ):
        return warn(f"Cannot {verb} from read-only folder {source.folder_name!r}.")
    target_account = find_imap_account(config, target.account_name)
    if target_account is None:
        return None
    if not target_account.folders.should_sync(target.folder_name):
        return warn(f"{target_noun} {target.folder_name!r} is excluded from sync.")
    if target_account.folders.is_read_only(target.folder_name):
        return warn(f"{target_noun} {target.folder_name!r} is read-only.")
    return None


def _move_in_place(
    mailboxes: Mailboxes,
    *,
    msg: IndexedMessage,
    mirror: MirrorRepository,
    source: FolderRef,
    target: FolderRef,
    log: NoticeLog,
) -> bool:
    """Rename the mirror file, keep the row, mark it ``PENDING_MOVE``.

    A ``KeyError`` from the mirror means the file is no longer where the
    index says it is, and in practice that means something else got
    there first: a second browser tab, another window, a sync between
    the two clicks.  Nothing is lost and nothing is Pony's fault, so it
    is reported as what happened rather than as an internal failure --
    "Failed to move message in local mirror" is true of a full disk too,
    and the two need different answers from the reader.
    """
    try:
        new_key = mirror.move_message_to_folder(
            folder=source,
            storage_key=msg.storage_key,
            target_folder=target.folder_name,
        )
    except KeyError:
        log.warn(
            f"{msg.subject or '(no subject)'!r} has already moved"
            " — nothing done. Sync, or reopen the folder, to see where.",
        )
        return False
    except Exception:  # noqa: BLE001
        log.failure("Failed to move message in local mirror.")
        return False
    # Same-account move: keep the row, with the original (folder, uid)
    # recorded as the server-side source, so sync executes UID MOVE (or
    # APPEND+EXPUNGE).  No delete-then-insert.
    mailboxes.index.update_message(
        message=moved_to_folder(msg, target=target, storage_key=new_key),
    )
    return True


def _move_across_accounts(
    mailboxes: Mailboxes,
    *,
    msg: IndexedMessage,
    source_mirror: MirrorRepository,
    target_mirror: MirrorRepository,
    source: FolderRef,
    target: FolderRef,
    source_is_imap: bool,
    log: NoticeLog,
) -> bool:
    """Copy the bytes to the target account, then retire the source."""
    landed = _duplicate_into(
        mailboxes,
        msg=msg,
        source_mirror=source_mirror,
        target_mirror=target_mirror,
        source=source,
        target=target,
        # Distinct accounts are independent identity namespaces, so a
        # true copy keeps IMAP thread integrity intact.
        rewrite_message_id=False,
        log=log,
    )
    if not landed:
        return False
    left_behind = retire_message(
        mailboxes,
        msg,
        source,
        expunge_on_server=source_is_imap,
    )
    if left_behind is not None:
        log.notices.append(left_behind)
    return True


def retire_message(
    mailboxes: Mailboxes,
    message: IndexedMessage,
    folder: FolderRef,
    *,
    expunge_on_server: bool,
) -> Notice | None:
    """Take *message* out of *folder* now that it lives somewhere else.

    A copy that has landed elsewhere, or a draft that has been sent,
    leaves an original behind.  When the server still holds it the row
    is marked ``TRASHED`` so the next sync EXPUNGEs it; when it never
    reached a server there is nothing to relay, so the row and its
    mirror file go outright.

    Which of those applies is the caller's to decide — a cross-account
    move asks whether the *account* has an IMAP side, while a sent draft
    asks whether that one row has a UID — so it arrives as
    *expunge_on_server* rather than being guessed here.

    Deleting the mirror file is best effort, because the index row is
    what the reader shows and `pony doctor`'s integrity scan finds a
    stray file.  "Best effort" used to mean a bare
    ``suppress(Exception)`` inside a function that returned ``None``,
    though, so no caller *could* report it: the notice comes back
    instead, for the caller to add to what it says.
    """
    if expunge_on_server:
        mailboxes.index.update_message(
            message=dataclasses.replace(message, local_status=MessageStatus.TRASHED),
        )
        return None
    left_behind: Notice | None = None
    mirror = mailboxes.mirror(message.message_ref.account_name)
    if mirror is not None:
        try:
            mirror.delete_message(folder=folder, storage_key=message.storage_key)
        except Exception:  # noqa: BLE001 — any backend may refuse
            left_behind = warn(
                f"{message.subject or '(no subject)'!r} is filed, but the old"
                f" copy in {folder.folder_name} is still on disk."
                " `pony doctor` lists it.",
            )
        # Without this an mbox deletion is only queued: the message is
        # still in the file and comes back on the next read.
        flush_mirror(mirror)
    mailboxes.index.delete_message(message_ref=message.message_ref)
    return left_behind


def _duplicate_into(
    mailboxes: Mailboxes,
    *,
    msg: IndexedMessage,
    source_mirror: MirrorRepository,
    target_mirror: MirrorRepository,
    source: FolderRef,
    target: FolderRef,
    rewrite_message_id: bool,
    log: NoticeLog,
) -> bool:
    """Write *msg*'s bytes into *target* and insert the index row.

    The shared half of copying and of a cross-account move: both read the
    source bytes, decide what happens to the Message-ID, store them on
    the target side and record a ``uid=NULL`` row for sync to push.
    """
    try:
        raw = source_mirror.get_message_bytes(
            folder=source,
            storage_key=msg.storage_key,
        )
    except Exception:  # noqa: BLE001
        log.failure(f"Could not read source message {msg.subject!r}.")
        return False
    new_raw, new_mid = copy_message_bytes(raw, rewrite_message_id=rewrite_message_id)
    try:
        new_key = target_mirror.store_message(folder=target, raw_message=new_raw)
    except Exception:  # noqa: BLE001
        log.failure(f"Failed to write copy to {target.folder_name}.")
        return False
    mailboxes.index.insert_message(
        message=landed_in_folder(
            msg,
            target=target,
            storage_key=new_key,
            message_id=new_mid,
        ),
    )
    return True


# ----------------------------------------------------------------------
# Folders
# ----------------------------------------------------------------------


def create_folder(
    mailboxes: Mailboxes,
    account_name: str,
    folder_name: str,
) -> Outcome:
    """Create *folder_name* in the account's local mirror."""
    mirror = mailboxes.mirror(account_name)
    if mirror is None:
        return Outcome.refused(failure(f"Unknown account {account_name!r}."))
    # The mirror directory *is* the request to create the folder, but the
    # planner only issues CREATE for names it would sync.  Making an
    # excluded folder would leave it local forever, and anything moved
    # into it stranded, with nothing said at any point.
    account = find_imap_account(mailboxes.config, account_name)
    if account is not None and not account.folders.should_sync(folder_name):
        return Outcome.refused(
            warn(
                f"Folder {folder_name!r} is excluded from sync; "
                "it would never be created on the server.",
            ),
        )
    try:
        mirror.create_folder(account_name=account_name, folder_name=folder_name)
    except Exception:  # noqa: BLE001
        return Outcome.refused(failure(f"Failed to create folder {folder_name!r}."))
    # Local accounts have no server side: the creation is terminal.
    # IMAP accounts get the folder pushed upstream on the next sync.
    suffix = "; run sync to propagate." if account is not None else "."
    return Outcome(
        changed=1,
        notices=(Notice(f"Folder {folder_name!r} created locally{suffix}"),),
    )


# ----------------------------------------------------------------------
# Shared odds and ends
# ----------------------------------------------------------------------


def folder_unread_counts(
    mailboxes: Mailboxes,
    account_name: str,
) -> dict[str, int]:
    """``{folder_name: unread}`` for one account's whole folder tree.

    Folders come from three places and the tree needs all of them:

    - the account's mirror, the only source that covers a local account,
      where the mirror *is* the account;
    - the index's folder sync state, which holds a folder the server has
      but this host has not materialised yet;
    - the index's unread counts, which hold a folder that has messages
      without a directory on disk.

    Each of those was a bug once, in the terminal, and a front end that
    asks the mirror alone reintroduces them.  Counts come from one
    ``GROUP BY`` rather than from materialising every row.
    """
    counts: dict[str, int] = {}
    mirror = mailboxes.mirror(account_name)
    if mirror is not None:
        for ref in mirror.list_folders(account_name=account_name):
            counts.setdefault(ref.folder_name, 0)
    for state in mailboxes.index.list_folder_sync_states(account_name=account_name):
        counts.setdefault(state.folder_name, 0)
    for folder_name, unread in mailboxes.index.unread_counts_by_folder(
        account_name=account_name,
    ).items():
        counts[folder_name] = unread
    return counts


def resolve_messages(
    mailboxes: Mailboxes,
    refs: Sequence[MessageRef],
) -> list[IndexedMessage]:
    """Fetch the full index row behind each of *refs*.

    Rows that have vanished from the index — raced with a sync delete,
    say — are dropped rather than reported: the action then acts on what
    is still there.
    """
    resolved: list[IndexedMessage] = []
    for ref in refs:
        msg = mailboxes.index.get_message(message_ref=ref)
        if msg is not None:
            resolved.append(msg)
    return resolved


def group_by_folder(
    messages: Sequence[IndexedMessage],
) -> dict[FolderRef, list[IndexedMessage]]:
    """*messages* split by the folder each one is actually in.

    Every folder-scoped operation here — move, archive, copy — takes a
    *source* and applies it to all the messages it is given.  A front
    end showing search results can have the user mark rows from several
    folders at once, and handing the first row's folder to all of them
    moved the ones that matched and reported a mirror failure for the
    rest, with the marks cleared so there was nothing to retry.  So the
    caller loops over this instead of guessing, and the first key is the
    folder the first message is in, which keeps the common single-folder
    case reporting in the order the rows were marked.
    """
    grouped: dict[FolderRef, list[IndexedMessage]] = {}
    for msg in messages:
        folder = FolderRef(
            account_name=msg.message_ref.account_name,
            folder_name=msg.message_ref.folder_name,
        )
        grouped.setdefault(folder, []).append(msg)
    return grouped


def merge_outcomes(outcomes: Sequence[Outcome]) -> Outcome:
    """One report for an operation that ran once per folder.

    The user asked for one thing and should hear about it once, so
    notices with the same text and severity collapse — two folders
    archived into the same Archive would otherwise say "Archived to
    Archive." twice.  Anything that differs is kept in the order it was
    produced, because a failure in one folder is exactly what must not
    be swallowed by a success in another.
    """
    changed = 0
    notices: list[Notice] = []
    seen: set[Notice] = set()
    for outcome in outcomes:
        changed += outcome.changed
        for notice in outcome.notices:
            if notice in seen:
                continue
            seen.add(notice)
            notices.append(notice)
    return Outcome(changed=changed, notices=tuple(notices))


def _mirror_pair(
    mailboxes: Mailboxes,
    source: FolderRef,
    target: FolderRef,
) -> tuple[MirrorRepository, MirrorRepository] | None:
    source_mirror = mailboxes.mirror(source.account_name)
    target_mirror = mailboxes.mirror(target.account_name)
    if source_mirror is None or target_mirror is None:
        return None
    return source_mirror, target_mirror


def _where(source: FolderRef, target: FolderRef) -> str:
    """How to name *target* to someone looking at *source*."""
    if target.account_name == source.account_name:
        return target.folder_name
    return f"{target.account_name}/{target.folder_name}"


def _count_suffix(count: int) -> str:
    return "" if count == 1 else f" ({count} messages)"


__all__ = [
    "Mailboxes",
    "add_flag",
    "archive_messages",
    "check_move_policy",
    "copy_messages",
    "create_folder",
    "group_by_folder",
    "mark_folder_read",
    "merge_outcomes",
    "move_messages",
    "resolve_messages",
    "retire_message",
    "set_flag",
    "toggle_flag",
    "trash_messages",
]
