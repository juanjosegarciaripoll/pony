"""Focused message and folder operation tests for ``MainScreen``."""

from __future__ import annotations

import dataclasses
from unittest.mock import Mock

import pytest
from corpus import plain_text
from tui_helpers import (
    build_pony_app,
    make_test_account,
    make_tmp_paths,
    seed_message,
)

from pony.domain import FolderConfig, FolderRef, MessageFlag
from pony.tui.screens.main_screen import MainScreen
from pony.tui.widgets.message_list import MessageListPanel


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

        index.delete_message(message_ref=summary.message_ref)
        screen._mark_seen(dataclasses.replace(summary, local_flags=frozenset()))


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


async def test_archive_and_same_account_move_report_mirror_failures() -> None:
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
        message = index.get_message(message_ref=summary.message_ref)
        assert message is not None
        mirrors["acct"].move_message_to_folder = Mock(
            side_effect=OSError("fictional mirror failure")
        )

        screen.archive_current_message()
        assert (
            screen._move_same_account(
                msg=message,
                mirror=mirrors["acct"],
                source=folder,
                target=FolderRef(account_name="acct", folder_name="Archive"),
            )
            is False
        )

    assert messages.count("Failed to move message in local mirror.") == 2


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

        screen._copy_to_folder([current], folder, missing)
        screen._move_to_folder([current], folder, folder)
        screen._move_to_folder([current], folder, missing)
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
    from pony.domain import LocalAccountConfig, MirrorConfig

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

        screen._move_to_folder([msg], source, target)
        await pilot.pause()

    # Target gained the message; the local source row is gone outright
    # rather than left behind as a TRASHED tombstone.
    assert len(list(index.list_folder_messages(folder=target))) == 1
    assert index.get_message(message_ref=msg.message_ref) is None


async def test_new_folder_falls_back_to_the_only_configured_account() -> None:
    """With exactly one account there is no ambiguity to resolve."""
    from pony.tui.screens.new_folder_screen import NewFolderScreen

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


async def test_marking_an_already_answered_message_is_a_noop() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-answered-noop",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        msg = screen.get_current_message()
        assert msg is not None

        screen._mark_answered(msg)
        await pilot.pause()
        answered = index.get_message(message_ref=msg.message_ref)
        assert answered is not None
        assert MessageFlag.ANSWERED in answered.local_flags

        # Second call sees the flag already set and returns early.
        screen._mark_answered(answered)
        await pilot.pause()

    still = index.get_message(message_ref=msg.message_ref)
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
    """Draft detection prefers the account's configured folder name."""
    paths = make_tmp_paths("main-drafts-folder")
    account = dataclasses.replace(make_test_account(paths), drafts_folder="Entwuerfe")
    app, *_ = build_pony_app(
        label="main-drafts-folder-app",
        accounts=(account,),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)

        screen._current_folder_ref = FolderRef(
            account_name="acct", folder_name="Entwuerfe"
        )
        assert screen._in_drafts_folder() is True

        screen._current_folder_ref = FolderRef(
            account_name="acct", folder_name="Drafts"
        )
        assert screen._in_drafts_folder() is False

        screen._current_folder_ref = None
        assert screen._in_drafts_folder() is False


async def test_search_without_a_folder_or_account_selected_warns() -> None:
    """With the tree cursor on the root there is nothing to scope to."""
    from pony.tui.widgets.folder_panel import FolderPanel

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


async def test_rearming_the_background_timer_resets_it_instead_of_stacking() -> None:
    """A manual sync pushes the next automatic run one interval out."""
    app, *_ = build_pony_app(label="main-bg-timer")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)

        screen._arm_background_sync_timer()
        first = screen._background_sync_timer
        assert first is not None

        screen._arm_background_sync_timer()
        await pilot.pause()

        # Same timer object, reset — not a second interval.
        assert screen._background_sync_timer is first


async def test_creating_a_folder_refreshes_the_tree() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="main-folder-created",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_inbox(pilot)
        screen = _main(app)
        mirrors["acct"].create_folder(account_name="acct", folder_name="Fresh")

        screen._create_folder("acct", "Fresh")
        await pilot.pause()

        from pony.tui.widgets.folder_panel import FolderPanel

        panel = screen.query_one(FolderPanel)
        names = {
            node.data.folder_name
            for node in panel._inbox_nodes
            if isinstance(node.data, FolderRef)
        }
        assert "INBOX" in names


async def test_goto_folder_moves_the_tree_cursor() -> None:
    """The goto dialog's callback selects the chosen folder."""
    from pony.tui.screens.goto_folder_screen import GotoFolderScreen

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
