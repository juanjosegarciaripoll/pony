"""Focused message and folder operation tests for ``MainScreen``."""

from __future__ import annotations

import dataclasses
from unittest.mock import Mock

import pytest
from corpus import plain_text
from tui_helpers import build_pony_app, make_test_account, make_tmp_paths

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
