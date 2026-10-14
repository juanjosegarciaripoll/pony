"""Focused message and folder operation tests for ``MainScreen``."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any
from unittest.mock import Mock

import pytest
from corpus import plain_text
from tui_helpers import (
    build_pony_app,
    make_test_account,
    make_tmp_paths,
    seed_message,
)

from pony.mail.domain import (
    AnyAccount,
    FolderConfig,
    FolderRef,
    MessageFlag,
    MessageStatus,
    MirrorConfig,
)
from pony.mail.index_store import SqliteIndexRepository
from pony.mail.protocols import MirrorRepository
from pony.mail.storage import MaildirMirrorRepository, MboxMirrorRepository
from pony.tui.mail.screens.compose_screen import ComposeScreen
from pony.tui.mail.screens.main_screen import MainScreen
from pony.tui.mail.widgets.folder_panel import FolderPanel
from pony.tui.mail.widgets.message_list import MessageListPanel
from pony.tui.mail.widgets.message_view import MessageViewPanel


def _main(app: object) -> MainScreen:
    assert isinstance(app.screen, MainScreen)  # type: ignore[attr-defined]
    return app.screen  # type: ignore[no-any-return,attr-defined]


def _notifications(app: object) -> list[str]:
    messages: list[str] = []
    original = app.notify  # type: ignore[attr-defined]

    def capture(message: str, **kwargs: object) -> None:
        messages.append(message)
        original(message, **kwargs)

    app.notify = capture  # type: ignore[attr-defined]
    return messages


async def _select_inbox(pilot: object) -> None:
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


async def test_empty_message_actions_are_safe_noops() -> None:
    app, *_ = build_pony_app(label="main-message-empty")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        assert screen.get_current_message() is None
        screen.set_flag(MessageFlag.SEEN, present=False)
        screen.toggle_flag(MessageFlag.FLAGGED)
        screen.trash_current_message()
        screen.archive_current_message()
        screen.action_mark_all_read()
        screen._prompt_folder_picker(verb="Copy", on_chosen=Mock())


async def test_mark_seen_handles_seen_and_missing_rows() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-message-seen",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        panel = screen.query_one(MessageListPanel)
        summary = panel.get_selected_summary()
        assert summary is not None

        screen._mark_seen(summary)
        updated = index.get_message(message_ref=summary.message_ref)
        assert updated is not None
        assert MessageFlag.SEEN in updated.local_flags
        screen._mark_seen(dataclasses.replace(summary, local_flags=updated.local_flags))

        # A summary from before someone else marked the row read: the
        # index says there is nothing to write, so there is nothing to
        # repaint either.
        stale = dataclasses.replace(summary, local_flags=frozenset())
        screen._mark_seen(stale)

        index.delete_message(message_ref=summary.message_ref)
        screen._mark_seen(stale)


@pytest.mark.parametrize(
    ("folders", "expected"),
    [
        (FolderConfig(read_only=("INBOX",)), "Cannot archive from read-only"),
        (FolderConfig(exclude=("Archive",)), "excluded from sync"),
        (FolderConfig(read_only=("Archive",)), "Archive folder 'Archive' is read-only"),
    ],
)
async def test_archive_policy_guards(folders: FolderConfig, expected: str) -> None:
    paths = make_tmp_paths("main-archive-policy")
    account = make_test_account(paths, archive_folder="Archive")
    account = dataclasses.replace(account, folders=folders)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-archive-policy-app",
        accounts=(account,),
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        _main(app).archive_current_message()
        await pilot.pause()

    assert any(expected in message for message in messages)


async def test_archive_reports_mirror_failures() -> None:
    """A mirror that refuses the rename is reported, not swallowed.

    The move itself is :mod:`pony.mail.mailbox_actions`' job and is tested
    there; what this covers is that the screen passes its notices on.
    """
    paths = make_tmp_paths("main-mirror-failures")
    account = make_test_account(paths, archive_folder="Archive")
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _app_paths, index, mirrors = build_pony_app(
        label="main-mirror-failures-app",
        accounts=(account,),
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        panel = screen.query_one(MessageListPanel)
        summary = panel.get_selected_summary()
        assert summary is not None
        assert index.get_message(message_ref=summary.message_ref) is not None
        mirrors["acct"].move_message_to_folder = Mock(
            side_effect=OSError("fictional mirror failure")
        )

        screen.archive_current_message()

    assert messages.count("Failed to move message in local mirror.") == 1


async def test_copy_move_and_folder_creation_error_guards() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-operation-errors",
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        current = screen.get_current_message()
        assert current is not None
        missing = FolderRef(account_name="missing", folder_name="Archive")

        screen._copy_to_folder([current], missing)
        screen._move_to_folder([current], folder)
        screen._move_to_folder([current], missing)
        screen._create_folder("missing", "Fictional")

        mirror = screen._mirrors["acct"]
        mirror.create_folder = Mock(side_effect=OSError("fictional create failure"))
        screen._create_folder("acct", "Fictional")

        assert index.get_message(message_ref=current.message_ref) is not None

    assert messages == [
        "Missing mirror for source or target account.",
        "Missing mirror for source or target account.",
        "Unknown account 'missing'.",
        "Failed to create folder 'Fictional'.",
    ]


def _local_account(paths: object, name: str = "local"):  # type: ignore[no-untyped-def]
    """A local (non-IMAP) account backed by a maildir mirror."""
    from pony.mail.domain import LocalAccountConfig, MirrorConfig

    mirror_dir = paths.data_dir / "mirrors" / name  # type: ignore[attr-defined]
    mirror_dir.mkdir(parents=True, exist_ok=True)
    return LocalAccountConfig(
        name=name,
        email_address=f"{name}@example.test",
        mirror=MirrorConfig(path=mirror_dir, format="maildir"),
    )


async def test_archive_requires_an_imap_account() -> None:
    """A local account has no server to archive to."""
    paths = make_tmp_paths("main-archive-local")
    folder = FolderRef(account_name="local", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-archive-local-app",
        accounts=(_local_account(paths),),
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        _main(app).archive_current_message()
        await pilot.pause()

    assert any("requires an IMAP account" in message for message in messages)


async def test_archiving_from_the_archive_folder_is_a_noop() -> None:
    """Source and target being the same folder means there is nothing to do."""
    paths = make_tmp_paths("main-archive-same")
    account = make_test_account(paths, archive_folder="INBOX")
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-archive-same-app",
        accounts=(account,),
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        _main(app).archive_current_message()
        await pilot.pause()

    # No warning, and the row stays exactly where it was.
    assert messages == []
    assert len(list(index.list_folder_messages(folder=folder))) == 1


async def test_cross_account_move_from_a_local_source_deletes_the_original() -> None:
    """Local sources have no sync to relay a deletion, so the row goes away.

    The IMAP path instead marks the source TRASHED and lets the next sync
    EXPUNGE it; a local mirror would keep that tombstone forever.
    """
    paths = make_tmp_paths("main-move-local-src")
    local = _local_account(paths, name="local")
    remote = make_test_account(paths, name="remote")
    source = FolderRef(account_name="local", folder_name="INBOX")
    target = FolderRef(account_name="remote", folder_name="INBOX")

    app, _cfg, _paths, index, mirrors = build_pony_app(
        label="main-move-local-src-app",
        accounts=(local, remote),
        seed=[(source, plain_text())],
    )
    mirrors["remote"].create_folder(account_name="remote", folder_name="INBOX")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        msg = list(index.list_folder_messages(folder=source))[0]

        screen._move_to_folder([msg], target)
        await pilot.pause()

    # Target gained the message; the local source row is gone outright
    # rather than left behind as a TRASHED tombstone.
    assert len(list(index.list_folder_messages(folder=target))) == 1
    assert index.get_message(message_ref=msg.message_ref) is None


async def test_new_folder_falls_back_to_the_only_configured_account() -> None:
    """With exactly one account there is no ambiguity to resolve."""
    from pony.tui.mail.screens.new_folder_screen import NewFolderScreen

    app, *_ = build_pony_app(label="main-new-folder-single")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._current_folder_ref = None

        assert screen._account_for_new_folder() is not None
        screen.action_new_folder()
        await pilot.pause()

        assert isinstance(app.screen, NewFolderScreen)
        app.screen.dismiss(None)
        await pilot.pause()


async def _select_and_orphan(pilot: object, app: object):  # type: ignore[no-untyped-def]
    """Select the first message, then delete its index row behind the screen.

    Models the race where a sync (or a second Pony process) removes the
    row after the list rendered it: the summaries are still on screen,
    but resolving them against the index yields nothing.
    """
    await _select_inbox(pilot)
    screen = _main(app)
    panel = screen.query_one(MessageListPanel)
    summary = panel.get_selected_summary()
    assert summary is not None
    screen._index.delete_message(message_ref=summary.message_ref)
    return screen


async def test_message_actions_tolerate_rows_deleted_underneath_them() -> None:
    """Every bulk action re-resolves its summaries and must cope with none."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-orphaned-rows",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        screen = await _select_and_orphan(pilot, app)

        screen.set_flag(MessageFlag.SEEN, present=True)
        screen.toggle_flag(MessageFlag.FLAGGED)
        screen.trash_current_message()
        screen.archive_current_message()
        screen.action_copy()
        await pilot.pause()

        # Nothing raised and no row was resurrected.
        assert list(screen._index.list_folder_messages(folder=folder)) == []


async def test_setting_a_flag_that_is_already_present_changes_nothing() -> None:
    """The per-message no-op check avoids a pointless index write."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-flag-noop",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        screen.set_flag(MessageFlag.FLAGGED, present=True)
        await pilot.pause()
        first = index.list_folder_messages(folder=folder)[0]

        screen.set_flag(MessageFlag.FLAGGED, present=True)
        await pilot.pause()
        second = index.list_folder_messages(folder=folder)[0]

    assert MessageFlag.FLAGGED in second.local_flags
    assert first.local_flags == second.local_flags


def test_marking_an_already_answered_message_is_a_noop() -> None:
    """``add_flag`` says whether the row changed, and what it did."""
    from tui_helpers import make_index, make_test_account, make_tmp_paths

    from pony.mail import mailbox_actions
    from pony.mail.domain import AppConfig
    from pony.mail.storage import MaildirMirrorRepository

    paths = make_tmp_paths("answered-noop")
    account = make_test_account(paths)
    index = make_index(paths)
    mailboxes = mailbox_actions.Mailboxes(
        config=AppConfig(accounts=(account,)),
        index=index,
        mirrors={
            account.name: MaildirMirrorRepository(
                account_name=account.name,
                root_dir=account.mirror.path,
            ),
        },
    )
    folder = FolderRef(account_name=account.name, folder_name="INBOX")
    stored = mailboxes.mirrors[account.name].store_message(
        folder=folder,
        raw_message=plain_text(),
    )
    from pony.mail.domain import MessageRef
    from pony.mail.message_projection import project_rfc822_message

    row = index.insert_message(
        message=project_rfc822_message(
            message_ref=MessageRef(
                account_name=account.name,
                folder_name="INBOX",
                id=0,
            ),
            raw_message=plain_text(),
            storage_key=stored,
        ),
    )

    flagged = mailbox_actions.add_flag(mailboxes, row, MessageFlag.ANSWERED)
    assert flagged.message is not None
    assert MessageFlag.ANSWERED in flagged.message.local_flags
    assert flagged.outcome.changed == 1

    # Already set: nothing to write, nothing to repaint, and an outcome
    # that says which of the two it was.
    again = mailbox_actions.add_flag(mailboxes, flagged.message, MessageFlag.ANSWERED)
    assert again.message is None
    assert again.outcome.changed == 0
    still = index.get_message(message_ref=row.message_ref)
    assert still is not None
    assert MessageFlag.ANSWERED in still.local_flags


async def test_mark_all_read_without_an_open_folder_is_a_noop() -> None:
    app, *_ = build_pony_app(label="main-mark-all-no-folder")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._current_folder_ref = None
        screen.action_mark_all_read()
        await pilot.pause()


async def test_the_configured_drafts_folder_is_recognised() -> None:
    """Draft detection prefers the account's configured folder name.

    The question is asked about the message under the cursor, not about
    the open folder, so the row has to be in the configured folder for
    ``e`` to be live.
    """
    paths = make_tmp_paths("main-drafts-folder")
    account = dataclasses.replace(make_test_account(paths), drafts_folder="Entwuerfe")
    app, _cfg, _paths, index, mirrors = build_pony_app(
        label="main-drafts-folder-app",
        accounts=(account,),
    )
    for name in ("Entwuerfe", "Drafts"):
        mirrors["acct"].create_folder(account_name="acct", folder_name=name)
        seed_message(
            index=index,
            mirror=mirrors["acct"],
            folder=FolderRef(account_name="acct", folder_name=name),
            raw=plain_text(),
            message_id=f"<{name}@example.com>",
        )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        panel = screen.query_one(MessageListPanel)

        for name, expected in (("Entwuerfe", True), ("Drafts", False)):
            panel.load_folder(FolderRef(account_name="acct", folder_name=name))
            await panel.wait_for_load_complete()
            await pilot.pause()
            assert screen.check_action("edit_draft", ()) is expected

        # Nothing under the cursor is not a draft either.
        panel.load_folder(FolderRef(account_name="acct", folder_name="Sent"))
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert screen.check_action("edit_draft", ()) is False


async def test_edit_draft_is_dead_on_a_search_hit_from_another_folder() -> None:
    """`e` retires what it opens, so it must ask the row, not the folder.

    Opening Drafts, moving the tree cursor to the account node and
    searching leaves ``_current_folder_ref`` pointing at Drafts while the
    hits span folders.  Asking the folder made ``e`` live on an INBOX
    hit, and the composer then retired that received mail — index row
    TRASHED, expunged server-side on the next sync.
    """
    inbox = FolderRef(account_name="acct", folder_name="INBOX")
    drafts = FolderRef(account_name="acct", folder_name="Drafts")
    app, _cfg, _paths, index, mirrors = build_pony_app(
        label="main-edit-draft-search",
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")
    for folder in (inbox, drafts):
        seed_message(
            index=index,
            mirror=mirrors["acct"],
            folder=folder,
            raw=plain_text(),
            message_id=f"<{folder.folder_name}-hit@example.com>",
        )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        panel = screen.query_one(MessageListPanel)
        # The tree cursor on the account node is what makes the search
        # account-wide; the open folder is still Drafts, and `_run_search`
        # never touches it.
        tree = screen.query_one(FolderPanel)
        tree.move_cursor(tree.root.children[0])
        await pilot.pause()
        screen._current_folder_ref = drafts
        screen._run_search("Tuesday")
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert screen._current_folder_ref == drafts

        by_folder = {
            row: panel._summaries[row].message_ref.folder_name
            for row in range(len(panel._summaries))
        }
        assert set(by_folder.values()) == {"INBOX", "Drafts"}

        for row, folder_name in by_folder.items():
            panel.move_cursor(row=row)
            await pilot.pause()
            assert screen.check_action("edit_draft", ()) is (folder_name == "Drafts")

        # And the refusal holds even when the action is called directly,
        # which is the half a stale footer cannot protect.
        inbox_row = next(row for row, name in by_folder.items() if name == "INBOX")
        panel.move_cursor(row=inbox_row)
        await pilot.pause()
        screen.compose_from_draft()
        await pilot.pause()
        assert not isinstance(app.screen, ComposeScreen)


async def test_a_key_pressed_while_the_list_loads_says_it_is_loading() -> None:
    """An action with no row to act on, because the rows are in flight.

    The list fetches its rows off the event loop, so there is a moment
    with nothing selected. An action then has nothing to act on, and
    the honest answer is to say the list is still loading rather than
    to do nothing quietly -- which reads as a key that was dropped.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-action-while-loading",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        panel = screen.query_one(MessageListPanel)
        messages = _notifications(app)

        panel.load_folder(folder)
        assert panel.awaiting_rows is True
        screen.trash_current_message()

        assert "The list is still loading." in messages
        # And nothing was trashed on the way past.
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert [
            row.local_status for row in index.list_folder_messages(folder=folder)
        ] == [MessageStatus.ACTIVE]


async def test_a_stale_rows_ready_does_not_spend_the_pending_sync() -> None:
    """The reader syncs from the load that is current, not the one before.

    ``RowsReady`` is a message, so one can be in the queue from a load
    that a newer load has already replaced. Acting on it synced the
    reader to a panel that had just been emptied -- clearing the pane --
    and consumed the request, so the rows that did arrive never synced
    anything.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-stale-rows-ready",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        panel = screen.query_one(MessageListPanel)

        panel.load_folder(folder)
        assert panel.awaiting_rows is True
        screen._sync_view_with_rows = True
        screen.on_message_list_panel_rows_ready(MessageListPanel.RowsReady())

        # Still owed a sync: the rows this is waiting for have not
        # arrived yet.
        assert screen._sync_view_with_rows is True

        await panel.wait_for_load_complete()
        await pilot.pause()
        assert screen._sync_view_with_rows is False


async def test_a_repaint_with_nothing_to_reload_leaves_no_sync_owing() -> None:
    """A request to sync the reader belongs to a load that happened.

    With no folder open and no search to re-run there is nothing to
    reload, and the request used to stay set -- so the *next* load,
    which is somebody opening a folder, opened its first message in the
    reader without being asked.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-repaint-nothing",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        panel = screen.query_one(MessageListPanel)
        view = screen.query_one(MessageViewPanel)
        screen._close_reader()
        screen._current_folder_ref = None

        screen._refresh_after_list_action()

        assert screen._sync_view_with_rows is False

        # Opening a folder now leaves the reader closed.
        screen._current_folder_ref = folder
        panel.load_folder(folder)
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert view.display is False


async def test_a_message_key_while_the_list_loads_says_it_is_loading() -> None:
    """The keys that act on one message, not on the marks.

    ``_act_on_targets`` says so; reply, forward, save, print and edit
    resolve their message through ``get_current_message``, which was
    simply ``None`` while the rows were in flight -- five keys that
    looked dropped.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-message-key-loading",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        panel = screen.query_one(MessageListPanel)
        messages = _notifications(app)

        panel.load_folder(folder)
        assert panel.awaiting_rows is True
        screen.compose_reply()

        assert "The list is still loading." in messages
        assert not isinstance(app.screen, ComposeScreen)


async def test_search_without_a_folder_or_account_selected_warns() -> None:
    """With the tree cursor on the root there is nothing to scope to."""
    from pony.tui.mail.widgets.folder_panel import FolderPanel

    app, *_ = build_pony_app(label="main-search-unscoped")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        panel = screen.query_one(FolderPanel)
        panel.move_cursor(panel.root)
        await pilot.pause()

        screen._run_search("anything")
        await pilot.pause()

    assert any("Select a folder or account first." in m for m in messages)


async def test_a_folder_scoped_search_excludes_other_folders() -> None:
    """The cursor on a folder node narrows the account-wide hit list."""
    from email.message import EmailMessage

    def _msg(subject: str, folder: str) -> bytes:
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = "acct@example.com"
        message["Subject"] = subject
        message["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
        message["Message-ID"] = f"<{folder}-{subject}@example.com>"
        message.set_content("needle in the body")
        return message.as_bytes()

    inbox = FolderRef(account_name="acct", folder_name="INBOX")
    archive = FolderRef(account_name="acct", folder_name="Archive")
    app, _cfg, _paths, _index, mirrors = build_pony_app(label="main-search-scoped")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Archive")
    for folder in (inbox, archive):
        seed_message(
            index=_index,
            mirror=mirrors["acct"],
            folder=folder,
            raw=_msg("needle", folder.folder_name),
            message_id=f"<{folder.folder_name}-needle@example.com>",
        )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        panel = screen.query_one(MessageListPanel)

        screen._run_search("needle")
        await panel.wait_for_load_complete()
        await pilot.pause()

        # Both folders hold a match, but only INBOX's is listed.
        assert panel.row_count == 1


async def test_navigating_past_the_last_message_stops() -> None:
    """The reader's next-message step has nowhere to go at the end."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-navigate-end",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        screen = _main(app)

        screen._navigate_from_view(delta=1)
        await pilot.pause()

        assert screen.query_one(MessageListPanel).cursor_row == 0


async def test_the_terminal_title_falls_back_without_an_open_folder() -> None:
    app, *_ = build_pony_app(label="main-title-no-folder")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._current_folder_ref = None

        screen._set_reader_terminal_title()
        await pilot.pause()


async def test_the_periodic_sync_is_the_apps_not_the_screens() -> None:
    """One scheduler, reached through the app, however often it is asked for.

    The screen used to own a Textual timer, so a second arming could stack
    a second interval and the test had to prove it did not. There is
    nothing left on the screen to stack: it only looks the scheduler up.
    """
    app, *_ = build_pony_app(label="main-bg-timer")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)

        first = screen._scheduler()
        assert first is not None
        assert screen._scheduler() is first
        assert first is app.mail_sync
        # Nothing on the screen schedules anything any more.
        assert not hasattr(screen, "_background_sync_timer")


def _tree_folder_names(panel: object) -> set[str]:
    """Every folder the panel's tree is currently showing.

    ``panel._inbox_nodes`` holds the INBOX rows only, which is why
    asserting against it could never see a newly created folder.
    """
    names: set[str] = set()
    pending = [panel.root]  # type: ignore[attr-defined]
    while pending:
        node = pending.pop()
        if isinstance(node.data, FolderRef):
            names.add(node.data.folder_name)
        pending.extend(node.children)
    return names


async def test_creating_a_folder_refreshes_the_tree() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="main-folder-created",
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)

        from pony.tui.mail.widgets.folder_panel import FolderPanel

        panel = screen.query_one(FolderPanel)
        before = _tree_folder_names(panel)

        screen._create_folder("acct", "Fresh")
        await pilot.pause()

        after = _tree_folder_names(panel)

    # The new folder has to be in the mirror *and* on the tree: the test
    # used to assert ``"INBOX" in names``, which held with the refresh
    # deleted.
    assert "Fresh" not in before
    assert "Fresh" in after
    assert "Fresh" in {
        ref.folder_name for ref in mirrors["acct"].list_folders(account_name="acct")
    }
    assert any("Folder 'Fresh' created locally" in m for m in messages)


async def test_goto_folder_moves_the_tree_cursor() -> None:
    """The goto dialog's callback selects the chosen folder."""
    from pony.tui.mail.screens.goto_folder_screen import GotoFolderScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="main-goto-folder",
        seed=[(folder, plain_text())],
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Archive")

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)

        screen.action_goto_folder()
        await pilot.pause()
        assert isinstance(app.screen, GotoFolderScreen)

        target = FolderRef(account_name="acct", folder_name="Archive")
        app.screen.dismiss(target)
        await pilot.pause()

        assert screen._current_folder_ref == target


async def test_goto_offers_a_folder_that_has_no_directory_yet() -> None:
    """The list came from the mirrors, and misses two kinds of folder.

    A folder the server has but this host has not materialised, and one
    that has messages with no directory on disk. The tree beside it
    shows both, because the tree asks `folder_unread_counts`; the goto
    dialog asked the mirror and offered neither.
    """
    from pony.mail.domain import FolderSyncState
    from pony.tui.mail.screens.goto_folder_screen import GotoFolderScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-goto-unmaterialised",
        seed=[(folder, plain_text())],
    )
    index.record_folder_sync_state(
        state=FolderSyncState(
            account_name="acct",
            folder_name="Synced/Elsewhere",
            uid_validity=1,
            highest_uid=0,
        ),
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        _main(app).action_goto_folder()
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, GotoFolderScreen)
        offered = {ref.folder_name for ref in dialog._folders}

    assert "Synced/Elsewhere" in offered
    assert "INBOX" in offered


async def test_trashing_records_when_it_happened() -> None:
    """``trashed_at`` drives both retention and the sync planner.

    A trashed row with no server UID and no ``trashed_at`` is given a
    PurgeLocalOp by the planner — dropped outright instead of kept
    recoverable — and ``purge_expired_trash`` selects on
    ``trashed_at IS NOT NULL``, so such a row is invisible to retention
    as well.  ``pony folder dedup`` always set it; the TUI did not.
    """
    from datetime import UTC, datetime

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-trash-timestamp",
        seed=[(folder, plain_text())],
    )

    before = datetime.now(tz=UTC)
    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        current = screen.get_current_message()
        assert current is not None
        seeded = current.message_ref
        screen.trash_current_message()
        await pilot.pause()
    after = datetime.now(tz=UTC)

    trashed = index.get_message(message_ref=seeded)
    assert trashed is not None
    assert trashed.local_status == MessageStatus.TRASHED
    assert trashed.trashed_at is not None
    assert before <= trashed.trashed_at <= after


async def test_trashing_an_already_trashed_row_keeps_the_original_time() -> None:
    """Re-trashing must not restart the retention clock."""
    import dataclasses as _dc
    from datetime import UTC, datetime, timedelta

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-trash-idempotent",
        seed=[(folder, plain_text())],
    )
    original = datetime.now(tz=UTC) - timedelta(days=5)

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        msg = screen.get_current_message()
        assert msg is not None
        index.update_message(
            message=_dc.replace(
                msg, local_status=MessageStatus.TRASHED, trashed_at=original
            )
        )
        screen.trash_current_message()
        await pilot.pause()

    reloaded = index.get_message(message_ref=msg.message_ref)
    assert reloaded is not None
    assert reloaded.trashed_at == original


async def test_creating_a_folder_the_sync_would_never_push_is_refused() -> None:
    """The mirror directory is the request, but the planner filters it.

    ``sync._plan_folders`` only issues CREATE for folder names satisfying
    ``should_sync``.  Making one under an ``exclude`` pattern left it
    local forever and stranded anything moved into it, silently.
    """
    paths = make_tmp_paths("main-create-excluded")
    account = dataclasses.replace(
        make_test_account(paths),
        folders=FolderConfig(exclude=("Scratch",)),
    )
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="main-create-excluded-app",
        accounts=(account,),
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        _main(app)._create_folder("acct", "Scratch")
        await pilot.pause()

        names = {
            ref.folder_name for ref in mirrors["acct"].list_folders(account_name="acct")
        }

    assert any("excluded from sync" in message for message in messages)
    assert "Scratch" not in names


async def test_a_synced_folder_name_is_still_created() -> None:
    paths = make_tmp_paths("main-create-allowed")
    account = dataclasses.replace(
        make_test_account(paths),
        folders=FolderConfig(exclude=("Scratch",)),
    )
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="main-create-allowed-app",
        accounts=(account,),
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        _main(app)._create_folder("acct", "Projects")
        await pilot.pause()

        names = {
            ref.folder_name for ref in mirrors["acct"].list_folders(account_name="acct")
        }

    assert "Projects" in names
    assert any("run sync to propagate" in message for message in messages)


async def test_a_local_account_folder_is_created_without_a_sync_hint() -> None:
    paths = make_tmp_paths("main-create-local")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="main-create-local-app",
        accounts=(_local_account(paths),),
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        _main(app)._create_folder("local", "Kept")
        await pilot.pause()

        names = {
            ref.folder_name
            for ref in mirrors["local"].list_folders(account_name="local")
        }

    assert "Kept" in names
    assert any("created locally." in message for message in messages)


def _maildir_names(
    mirrors: Mapping[str, MirrorRepository], account: str = "acct"
) -> list[str]:
    """Filenames in the account's INBOX cur/ directory."""
    mirror = mirrors[account]
    assert isinstance(mirror, MaildirMirrorRepository)
    return sorted(p.name for p in (mirror._root_dir / "cur").iterdir())  # noqa: SLF001


def _mbox_account(label: str) -> AnyAccount:
    """An account whose mirror is mbox rather than Maildir."""
    paths = make_tmp_paths(label)
    return dataclasses.replace(
        make_test_account(paths),
        mirror=MirrorConfig(path=paths.data_dir / "mbox", format="mbox"),
    )


def _mbox_rows(mirrors: Mapping[str, MirrorRepository]) -> list[tuple[str, str]]:
    """(subject, Status) for every message physically in the mbox file.

    Read with a fresh ``mailbox.mbox`` rather than through the
    repository, because the point is what another mail client — or this
    one after a crash — would find on disk.
    """
    import mailbox

    mirror = mirrors["acct"]
    assert isinstance(mirror, MboxMirrorRepository)
    path = next(
        p
        for p in mirror._root_dir.rglob("*")  # noqa: SLF001
        if p.is_file() and p.suffix == ".mbox"
    )
    box = mailbox.mbox(str(path))
    try:
        return [(str(m.get("Subject")), str(m.get("Status"))) for m in box]
    finally:
        box.close()


def _numbered_message(index: int) -> bytes:
    """A plain message whose subject identifies it."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "user@example.com"
    message["Subject"] = f"numbered {index}"
    message["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
    message["Message-ID"] = f"<numbered-{index}@example.com>"
    message.set_content("body")
    return message.as_bytes()


def _searchable(subject: str, folder: str) -> bytes:
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "acct@example.com"
    message["Subject"] = subject
    message["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
    message["Message-ID"] = f"<{folder}-{subject}@example.com>"
    message.set_content("zzqneedle in the body")
    return message.as_bytes()


def _search_across_two_folders(
    label: str,
) -> tuple[Any, SqliteIndexRepository]:
    """An account with one hit in INBOX and one in Work, both on screen."""
    inbox = FolderRef(account_name="acct", folder_name="INBOX")
    work = FolderRef(account_name="acct", folder_name="Work")
    account = make_test_account(make_tmp_paths(label), archive_folder="Archive")
    app, _cfg, _paths, index, mirrors = build_pony_app(
        label=f"{label}-app",
        accounts=(account,),
    )
    for name in ("Work", "Archive"):
        mirrors["acct"].create_folder(account_name="acct", folder_name=name)
    for folder in (inbox, work):
        seed_message(
            index=index,
            mirror=mirrors["acct"],
            folder=folder,
            raw=_searchable("zzqhit", folder.folder_name),
            message_id=f"<{folder.folder_name}-zzqhit@example.com>",
        )
    return app, index


async def test_marks_spanning_folders_are_each_archived_from_their_own() -> None:
    """One source folder for all of them left half the marks behind.

    Reproduced before the fix: the INBOX row became PENDING_MOVE in
    Archive, the Work row stayed ACTIVE in Work, and the user got
    "Failed to move message in local mirror." beside "Archived to
    Archive." — with the marks cleared, so there was nothing to retry.
    """
    app, index = _search_across_two_folders("main-archive-spanning")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        tree = screen.query_one(FolderPanel)
        tree.move_cursor(tree.root.children[0])
        await pilot.pause()
        panel = screen.query_one(MessageListPanel)
        screen._run_search("zzqneedle")
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert panel.row_count == 2

        # Mark both rows, from two different folders.
        panel.move_cursor(row=0)
        panel.action_mark_down()
        panel.action_mark_down()
        await pilot.pause()
        assert len(panel.marked_summaries()) == 2

        screen.action_archive()
        await pilot.pause()

    archived = list(
        index.list_folder_messages(
            folder=FolderRef(account_name="acct", folder_name="Archive"),
        ),
    )
    assert len(archived) == 2
    assert {row.local_status for row in archived} == {MessageStatus.PENDING_MOVE}
    for name in ("INBOX", "Work"):
        left = list(
            index.list_folder_messages(
                folder=FolderRef(account_name="acct", folder_name=name),
            ),
        )
        # The row moves to Archive rather than staying behind; what
        # matters is that *neither* source folder kept its message.
        assert left == []
    # One report, not two contradicting each other.
    assert not any("Failed to move" in m for m in messages)
    assert sum("Archived to Archive" in m for m in messages) == 1


async def test_an_action_on_a_search_hit_keeps_the_search_on_screen() -> None:
    """Triage from a search used to mean one search per message."""
    app, _index = _search_across_two_folders("main-search-survives")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        tree = screen.query_one(FolderPanel)
        tree.move_cursor(tree.root.children[0])
        await pilot.pause()
        panel = screen.query_one(MessageListPanel)
        screen._run_search("zzqneedle")
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert panel.in_search
        assert panel.row_count == 2

        # Flagging a hit is the cheapest action that repaints the list.
        screen.action_toggle_flagged()
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert panel.in_search
        assert str(panel.border_title).startswith("Search: zzqneedle")
        assert panel.row_count == 2

        # And an archive, which removes a row from its folder, leaves the
        # remaining hit listed rather than dropping back to the folder.
        screen.action_archive()
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert panel.in_search


async def test_leaving_the_search_stops_an_action_from_re_running_it() -> None:
    app, _index = _search_across_two_folders("main-search-exit")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        tree = screen.query_one(FolderPanel)
        tree.move_cursor(tree.root.children[0])
        await pilot.pause()
        panel = screen.query_one(MessageListPanel)
        screen._run_search("zzqneedle")
        await panel.wait_for_load_complete()
        await pilot.pause()

        screen.on_message_list_panel_search_exit_requested(
            MessageListPanel.SearchExitRequested(),
        )
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert screen._search is None
        assert not panel.in_search
        screen.action_toggle_flagged()
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert not panel.in_search
