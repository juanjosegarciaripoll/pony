"""The mailbox actions, exercised without a user interface.

Archive, move, copy, trash, flag changes and folder creation used to be
written out inside ``MainScreen``, so the only way to reach them was to
start a Textual app, drive a Pilot and read the toasts back.  They live
in :mod:`pony.mailbox_actions` now, over the stores, and these tests are
what that buys: the policy that refuses a destructive move and the
bookkeeping that keeps sync honest are checked directly, against real
SQLite and real maildirs.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock

from corpus import plain_text, unthreaded_message
from tui_helpers import (
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
    seed_message,
)

from pony import mailbox_actions
from pony.domain import (
    AnyAccount,
    AppConfig,
    FolderConfig,
    FolderRef,
    IndexedMessage,
    LocalAccountConfig,
    MessageFlag,
    MessageStatus,
    MirrorConfig,
)
from pony.mailbox_actions import Mailboxes

if TYPE_CHECKING:
    from pony.paths import AppPaths

INBOX = FolderRef(account_name="acct", folder_name="INBOX")
ARCHIVE = FolderRef(account_name="acct", folder_name="Archive")


def _local_account(paths: AppPaths, name: str = "local") -> LocalAccountConfig:
    mirror_dir = paths.data_dir / "mirrors" / name
    mirror_dir.mkdir(parents=True, exist_ok=True)
    return LocalAccountConfig(
        name=name,
        email_address=f"{name}@example.com",
        mirror=MirrorConfig(path=mirror_dir, format="maildir"),
    )


def _build(
    label: str,
    *,
    accounts: tuple[AnyAccount, ...] | None = None,
    seed: tuple[tuple[FolderRef, bytes], ...] = ((INBOX, plain_text()),),
) -> tuple[Mailboxes, AppConfig]:
    """Real stores, no app: one index, one mirror per account."""
    paths = make_tmp_paths(label)
    if accounts is None:
        accounts = (make_test_account(paths, archive_folder="Archive"),)
    config = make_test_config(accounts=accounts)
    index = make_index(paths)
    mirrors = make_mirrors(config)
    for folder, raw in seed:
        seed_message(
            index=index,
            mirror=mirrors[folder.account_name],
            folder=folder,
            raw=raw,
        )
    return Mailboxes(config=config, index=index, mirrors=mirrors), config


def _rows(mailboxes: Mailboxes, folder: FolderRef) -> list[IndexedMessage]:
    return list(mailboxes.index.list_folder_messages(folder=folder))


def _texts(outcome: object) -> list[str]:
    return [notice.text for notice in outcome.notices]  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# Flags
# ----------------------------------------------------------------------


def test_add_flag_returns_the_new_row_and_then_nothing() -> None:
    mailboxes, _config = _build("actions-add-flag")
    (message,) = _rows(mailboxes, INBOX)

    updated = mailbox_actions.add_flag(mailboxes, message, MessageFlag.SEEN)

    assert updated is not None
    assert MessageFlag.SEEN in updated.local_flags
    stored = mailboxes.index.get_message(message_ref=message.message_ref)
    assert stored is not None
    assert MessageFlag.SEEN in stored.local_flags
    # Already set: nothing to write and nothing for a caller to repaint.
    assert mailbox_actions.add_flag(mailboxes, updated, MessageFlag.SEEN) is None


def test_set_flag_counts_only_the_rows_it_changed() -> None:
    mailboxes, _config = _build("actions-set-flag")
    messages = _rows(mailboxes, INBOX)

    added = mailbox_actions.set_flag(
        mailboxes,
        messages,
        MessageFlag.FLAGGED,
        present=True,
    )
    assert added.changed == 1
    assert added.acted

    # The rows handed in are now stale, which is exactly the case a
    # second press produces: the flag is already set, so nothing moves.
    again = mailbox_actions.set_flag(
        mailboxes,
        _rows(mailboxes, INBOX),
        MessageFlag.FLAGGED,
        present=True,
    )
    assert again.changed == 0
    assert not again.acted

    removed = mailbox_actions.set_flag(
        mailboxes,
        _rows(mailboxes, INBOX),
        MessageFlag.FLAGGED,
        present=False,
    )
    assert removed.changed == 1
    (stored,) = _rows(mailboxes, INBOX)
    assert MessageFlag.FLAGGED not in stored.local_flags


def test_toggle_flag_flips_each_row_on_its_own() -> None:
    mailboxes, _config = _build("actions-toggle-flag")
    (message,) = _rows(mailboxes, INBOX)
    mailbox_actions.add_flag(mailboxes, message, MessageFlag.FLAGGED)

    outcome = mailbox_actions.toggle_flag(
        mailboxes,
        _rows(mailboxes, INBOX),
        MessageFlag.FLAGGED,
    )

    assert outcome.changed == 1
    (stored,) = _rows(mailboxes, INBOX)
    assert MessageFlag.FLAGGED not in stored.local_flags


def test_mark_folder_read_reports_a_count_or_says_nothing() -> None:
    mailboxes, _config = _build("actions-mark-read")

    outcome = mailbox_actions.mark_folder_read(mailboxes, INBOX)
    assert outcome.changed == 1
    assert _texts(outcome) == ["Marked 1 message as read."]

    # Nothing left unread: no count, and nothing to say about it.
    quiet = mailbox_actions.mark_folder_read(mailboxes, INBOX)
    assert quiet.changed == 0
    assert quiet.notices == ()


# ----------------------------------------------------------------------
# Trash
# ----------------------------------------------------------------------


def test_trash_records_when_it_happened_and_keeps_the_first_answer() -> None:
    """``trashed_at`` is what retention and the planner key off."""
    mailboxes, _config = _build("actions-trash")
    first = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    second = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

    outcome = mailbox_actions.trash_messages(
        mailboxes, _rows(mailboxes, INBOX), now=first
    )

    assert outcome.changed == 1
    (stored,) = _rows(mailboxes, INBOX)
    assert stored.local_status is MessageStatus.TRASHED
    assert stored.trashed_at == first

    mailbox_actions.trash_messages(mailboxes, _rows(mailboxes, INBOX), now=second)
    (stored,) = _rows(mailboxes, INBOX)
    assert stored.trashed_at == first


# ----------------------------------------------------------------------
# Archive
# ----------------------------------------------------------------------


def test_archive_moves_to_the_configured_folder() -> None:
    mailboxes, _config = _build("actions-archive")

    outcome = mailbox_actions.archive_messages(
        mailboxes, _rows(mailboxes, INBOX), INBOX
    )

    assert _texts(outcome) == ["Archived to Archive."]
    assert _rows(mailboxes, INBOX) == []
    (stored,) = _rows(mailboxes, ARCHIVE)
    # The row carries where it came from, which is what makes the next
    # sync a UID MOVE rather than an append plus a stray delete.
    assert stored.uid is None
    assert stored.source_folder == "INBOX"


def test_archive_needs_an_imap_account_and_a_configured_folder() -> None:
    paths = make_tmp_paths("actions-archive-local")
    local_folder = FolderRef(account_name="local", folder_name="INBOX")
    mailboxes, _config = _build(
        "actions-archive-local-stores",
        accounts=(_local_account(paths),),
        seed=((local_folder, plain_text()),),
    )

    refusal = mailbox_actions.archive_messages(
        mailboxes,
        _rows(mailboxes, local_folder),
        local_folder,
    )
    assert _texts(refusal) == ["Archive requires an IMAP account."]

    unset = make_test_account(paths, archive_folder=None)
    bare, _config = _build("actions-archive-unset", accounts=(unset,))
    silent = mailbox_actions.archive_messages(bare, _rows(bare, INBOX), INBOX)
    assert _texts(silent) == ["No archive_folder configured for this account."]


def test_archiving_the_archive_folder_does_nothing_quietly() -> None:
    paths = make_tmp_paths("actions-archive-same")
    account = make_test_account(paths, archive_folder="INBOX")
    mailboxes, _config = _build("actions-archive-same-stores", accounts=(account,))

    outcome = mailbox_actions.archive_messages(
        mailboxes, _rows(mailboxes, INBOX), INBOX
    )

    assert outcome.notices == ()
    assert not outcome.acted
    assert len(_rows(mailboxes, INBOX)) == 1


def test_archive_refusals_name_the_archive_folder() -> None:
    """Archiving says "archive", not "move": it is its own verb."""
    paths = make_tmp_paths("actions-archive-policy")
    cases = (
        (
            FolderConfig(read_only=("INBOX",)),
            "Cannot archive from read-only folder 'INBOX'.",
        ),
        (
            FolderConfig(exclude=("Archive",)),
            "Archive folder 'Archive' is excluded from sync.",
        ),
        (
            FolderConfig(read_only=("Archive",)),
            "Archive folder 'Archive' is read-only.",
        ),
    )
    for index, (folders, expected) in enumerate(cases):
        account = dataclasses.replace(
            make_test_account(paths, archive_folder="Archive"),
            folders=folders,
        )
        mailboxes, _config = _build(
            f"actions-archive-policy-{index}",
            accounts=(account,),
        )
        outcome = mailbox_actions.archive_messages(
            mailboxes,
            _rows(mailboxes, INBOX),
            INBOX,
        )
        assert _texts(outcome) == [expected]
        assert len(_rows(mailboxes, INBOX)) == 1


# ----------------------------------------------------------------------
# Move
# ----------------------------------------------------------------------


def test_move_within_an_account_keeps_the_row_and_its_message_id() -> None:
    mailboxes, _config = _build("actions-move-same")
    (before,) = _rows(mailboxes, INBOX)

    outcome = mailbox_actions.move_messages(
        mailboxes,
        [before],
        INBOX,
        ARCHIVE,
    )

    assert _texts(outcome) == ["Moved to Archive."]
    (after,) = _rows(mailboxes, ARCHIVE)
    assert after.message_id == before.message_id
    assert after.uid is None


def test_move_to_the_same_folder_is_a_noop() -> None:
    mailboxes, _config = _build("actions-move-noop")

    outcome = mailbox_actions.move_messages(
        mailboxes,
        _rows(mailboxes, INBOX),
        INBOX,
        INBOX,
    )

    assert outcome.notices == ()
    assert len(_rows(mailboxes, INBOX)) == 1


def test_move_across_accounts_copies_then_retires_the_source() -> None:
    paths = make_tmp_paths("actions-move-cross")
    imap = make_test_account(paths, archive_folder="Archive")
    local = _local_account(paths)
    target = FolderRef(account_name="local", folder_name="INBOX")
    mailboxes, _config = _build(
        "actions-move-cross-stores",
        accounts=(imap, local),
        seed=((INBOX, plain_text()),),
    )
    (before,) = _rows(mailboxes, INBOX)

    outcome = mailbox_actions.move_messages(mailboxes, [before], INBOX, target)

    assert _texts(outcome) == ["Moved to local/INBOX."]
    (landed,) = _rows(mailboxes, target)
    # Distinct accounts are independent identity namespaces, so the
    # Message-ID survives the crossing.
    assert landed.message_id == before.message_id
    # The IMAP source is kept as TRASHED so the next sync expunges it.
    (source_row,) = _rows(mailboxes, INBOX)
    assert source_row.local_status is MessageStatus.TRASHED


def test_move_off_a_local_account_deletes_the_source_outright() -> None:
    paths = make_tmp_paths("actions-move-from-local")
    imap = make_test_account(paths, archive_folder="Archive")
    local = _local_account(paths)
    source = FolderRef(account_name="local", folder_name="INBOX")
    mailboxes, _config = _build(
        "actions-move-from-local-stores",
        accounts=(imap, local),
        seed=((source, plain_text()),),
    )

    outcome = mailbox_actions.move_messages(
        mailboxes,
        _rows(mailboxes, source),
        source,
        INBOX,
    )

    assert outcome.changed == 1
    # No sync will ever relay a deletion for a local account, so the row
    # goes rather than waiting as TRASHED forever.
    assert _rows(mailboxes, source) == []
    assert len(_rows(mailboxes, INBOX)) == 1


def test_move_refuses_a_missing_mirror_and_a_read_only_source() -> None:
    paths = make_tmp_paths("actions-move-refusals")
    account = dataclasses.replace(
        make_test_account(paths, archive_folder="Archive"),
        folders=FolderConfig(read_only=("INBOX",)),
    )
    mailboxes, _config = _build("actions-move-refusals-stores", accounts=(account,))
    nowhere = FolderRef(account_name="missing", folder_name="Archive")

    missing = mailbox_actions.move_messages(
        mailboxes,
        _rows(mailboxes, INBOX),
        INBOX,
        nowhere,
    )
    assert _texts(missing) == ["Missing mirror for source or target account."]

    read_only = mailbox_actions.move_messages(
        mailboxes,
        _rows(mailboxes, INBOX),
        INBOX,
        ARCHIVE,
    )
    assert _texts(read_only) == ["Cannot move from read-only folder 'INBOX'."]
    assert len(_rows(mailboxes, INBOX)) == 1


def test_move_reports_a_mirror_that_refuses_the_rename() -> None:
    mailboxes, _config = _build("actions-move-mirror-failure")
    mailboxes.mirrors["acct"].move_message_to_folder = Mock(  # type: ignore[union-attr]
        side_effect=OSError("fictional mirror failure"),
    )

    outcome = mailbox_actions.move_messages(
        mailboxes,
        _rows(mailboxes, INBOX),
        INBOX,
        ARCHIVE,
    )

    assert _texts(outcome) == ["Failed to move message in local mirror."]
    assert not outcome.acted
    # The row stays where it is: a failed mirror write is not a move.
    assert len(_rows(mailboxes, INBOX)) == 1


def test_a_message_something_else_moved_first_says_so() -> None:
    """Two browser tabs, or a window and a sync, racing for one message.

    The mirror answers `KeyError` -- the file is not where the index
    says it is -- and that is not an internal failure: nothing is lost
    and nothing is Pony's fault. It used to read "Failed to move
    message in local mirror", which is also what a full disk says, and
    the two need different answers from the reader.
    """
    mailboxes, _config = _build("actions-move-already-moved")
    (row,) = _rows(mailboxes, INBOX)
    mailboxes.mirrors["acct"].move_message_to_folder = Mock(  # type: ignore[union-attr]
        side_effect=KeyError("message not found: 1234"),
    )

    outcome = mailbox_actions.move_messages(mailboxes, [row], INBOX, ARCHIVE)

    (text,) = _texts(outcome)
    assert "has already moved" in text
    assert row.subject in text
    assert outcome.notices[0].severity == "warning"
    assert not outcome.acted
    assert len(_rows(mailboxes, INBOX)) == 1


# ----------------------------------------------------------------------
# Copy
# ----------------------------------------------------------------------


def test_copy_within_an_account_rewrites_the_message_id() -> None:
    """Otherwise the planner reads the duplicate as a move."""
    mailboxes, _config = _build("actions-copy-same")
    (before,) = _rows(mailboxes, INBOX)

    outcome = mailbox_actions.copy_messages(mailboxes, [before], INBOX, ARCHIVE)

    assert _texts(outcome) == ["Copied to Archive."]
    (copy,) = _rows(mailboxes, ARCHIVE)
    assert copy.message_id != before.message_id
    assert copy.uid is None
    # The original is untouched — that is what makes it a copy.
    assert len(_rows(mailboxes, INBOX)) == 1


def test_copy_across_accounts_keeps_the_message_id() -> None:
    """Distinct accounts are independent identity namespaces."""
    paths = make_tmp_paths("actions-copy-cross")
    imap = make_test_account(paths, archive_folder="Archive")
    local = _local_account(paths)
    target = FolderRef(account_name="local", folder_name="INBOX")
    mailboxes, _config = _build(
        "actions-copy-cross-stores",
        accounts=(imap, local),
        seed=((INBOX, plain_text()),),
    )
    (before,) = _rows(mailboxes, INBOX)

    outcome = mailbox_actions.copy_messages(mailboxes, [before], INBOX, target)

    assert _texts(outcome) == ["Copied to local/INBOX."]
    (landed,) = _rows(mailboxes, target)
    assert landed.message_id == before.message_id


def test_copy_counts_the_messages_it_copied() -> None:
    mailboxes, _config = _build("actions-copy-count")
    seed_message(
        index=mailboxes.index,
        mirror=mailboxes.mirrors["acct"],
        folder=INBOX,
        raw=unthreaded_message(),
    )

    outcome = mailbox_actions.copy_messages(
        mailboxes,
        _rows(mailboxes, INBOX),
        INBOX,
        ARCHIVE,
    )

    assert _texts(outcome) == ["Copied to Archive (2 messages)."]
    assert len(_rows(mailboxes, ARCHIVE)) == 2


def test_copy_reports_unreadable_sources_and_failed_writes() -> None:
    mailboxes, _config = _build("actions-copy-failures")
    messages = _rows(mailboxes, INBOX)
    mirror = mailboxes.mirrors["acct"]
    original_get = mirror.get_message_bytes
    mirror.get_message_bytes = Mock(side_effect=OSError("unreadable"))  # type: ignore[union-attr]

    unreadable = mailbox_actions.copy_messages(mailboxes, messages, INBOX, ARCHIVE)
    assert _texts(unreadable) == [
        f"Could not read source message {messages[0].subject!r}."
    ]

    mirror.get_message_bytes = original_get  # type: ignore[union-attr]
    mirror.store_message = Mock(side_effect=OSError("unwritable"))  # type: ignore[union-attr]
    unwritable = mailbox_actions.copy_messages(mailboxes, messages, INBOX, ARCHIVE)
    assert _texts(unwritable) == ["Failed to write copy to Archive."]

    missing = FolderRef(account_name="missing", folder_name="INBOX")
    assert _texts(
        mailbox_actions.copy_messages(mailboxes, messages, INBOX, missing)
    ) == [
        "Missing mirror for source or target account.",
    ]


# ----------------------------------------------------------------------
# Retiring a message
# ----------------------------------------------------------------------


def test_retire_trashes_a_synced_row_and_deletes_an_unsynced_one() -> None:
    mailboxes, _config = _build("actions-retire")
    (message,) = _rows(mailboxes, INBOX)

    mailbox_actions.retire_message(mailboxes, message, INBOX, expunge_on_server=True)
    (trashed,) = _rows(mailboxes, INBOX)
    assert trashed.local_status is MessageStatus.TRASHED

    mailbox_actions.retire_message(mailboxes, trashed, INBOX, expunge_on_server=False)
    assert _rows(mailboxes, INBOX) == []


def test_retire_survives_a_mirror_that_cannot_delete() -> None:
    """The index row is authoritative; a stray file is the scan's problem."""
    mailboxes, _config = _build("actions-retire-failure")
    (message,) = _rows(mailboxes, INBOX)
    mailboxes.mirrors["acct"].delete_message = Mock(  # type: ignore[union-attr]
        side_effect=OSError("fictional delete failure"),
    )

    mailbox_actions.retire_message(mailboxes, message, INBOX, expunge_on_server=False)

    assert _rows(mailboxes, INBOX) == []


# ----------------------------------------------------------------------
# Folders
# ----------------------------------------------------------------------


def test_create_folder_says_whether_a_sync_will_follow() -> None:
    paths = make_tmp_paths("actions-create-folder")
    imap = make_test_account(paths, archive_folder="Archive")
    local = _local_account(paths)
    mailboxes, _config = _build(
        "actions-create-folder-stores",
        accounts=(imap, local),
        seed=((INBOX, plain_text()),),
    )

    on_imap = mailbox_actions.create_folder(mailboxes, "acct", "Receipts")
    assert _texts(on_imap) == [
        "Folder 'Receipts' created locally; run sync to propagate.",
    ]

    # A local account has no server side: the creation is terminal.
    on_local = mailbox_actions.create_folder(mailboxes, "local", "Receipts")
    assert _texts(on_local) == ["Folder 'Receipts' created locally."]


def test_create_folder_refuses_what_sync_would_never_push() -> None:
    paths = make_tmp_paths("actions-create-folder-refusals")
    account = dataclasses.replace(
        make_test_account(paths, archive_folder="Archive"),
        folders=FolderConfig(exclude=("Receipts",)),
    )
    mailboxes, _config = _build(
        "actions-create-folder-refusals-stores",
        accounts=(account,),
    )

    excluded = mailbox_actions.create_folder(mailboxes, "acct", "Receipts")
    assert _texts(excluded) == [
        "Folder 'Receipts' is excluded from sync; "
        "it would never be created on the server.",
    ]

    unknown = mailbox_actions.create_folder(mailboxes, "nobody", "Receipts")
    assert _texts(unknown) == ["Unknown account 'nobody'."]

    mailboxes.mirrors["acct"].create_folder = Mock(  # type: ignore[union-attr]
        side_effect=OSError("fictional create failure"),
    )
    broken = mailbox_actions.create_folder(mailboxes, "acct", "Keep")
    assert _texts(broken) == ["Failed to create folder 'Keep'."]


# ----------------------------------------------------------------------
# Policy on its own
# ----------------------------------------------------------------------


def test_resolve_messages_drops_rows_that_have_vanished() -> None:
    """A row raced away by a sync delete is skipped, not an error."""
    mailboxes, _config = _build("actions-resolve")
    (message,) = _rows(mailboxes, INBOX)
    refs = [message.message_ref]

    assert mailbox_actions.resolve_messages(mailboxes, refs) == [message]

    mailboxes.index.delete_message(message_ref=message.message_ref)
    assert mailbox_actions.resolve_messages(mailboxes, refs) == []


def test_check_move_policy_ignores_accounts_with_no_server() -> None:
    paths = make_tmp_paths("actions-policy-local")
    local = _local_account(paths)
    mailboxes, config = _build(
        "actions-policy-local-stores",
        accounts=(local,),
        seed=((FolderRef(account_name="local", folder_name="INBOX"), plain_text()),),
    )
    source = FolderRef(account_name="local", folder_name="INBOX")
    target = FolderRef(account_name="local", folder_name="Keep")

    assert mailbox_actions.check_move_policy(config, source, target) is None
    assert mailboxes.mirror("local") is not None
