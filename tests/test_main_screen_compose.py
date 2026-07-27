"""Focused compose, contact, and draft callbacks for ``MainScreen``."""

from __future__ import annotations

import dataclasses
from email.message import EmailMessage
from unittest.mock import Mock

from corpus import plain_text
from tui_helpers import build_pony_app

from pony.domain import Contact, FolderRef, MessageStatus
from pony.tui.screens.contact_edit_screen import ContactEditScreen
from pony.tui.screens.main_screen import MainScreen
from pony.tui.widgets.message_list import MessageListPanel
from pony.tui.widgets.message_view import MessageViewPanel


def _main(app: object) -> MainScreen:
    return next(
        screen
        for screen in app.screen_stack  # type: ignore[attr-defined]
        if isinstance(screen, MainScreen)
    )


def _notifications(app: object) -> list[str]:
    messages: list[str] = []
    original = app.notify  # type: ignore[attr-defined]

    def capture(message: str, **kwargs: object) -> None:
        messages.append(message)
        original(message, **kwargs)

    app.notify = capture  # type: ignore[attr-defined]
    return messages


async def _select_message(pilot: object) -> None:
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


def _draft_bytes() -> bytes:
    message = EmailMessage()
    message["From"] = "acct@example.test"
    message["To"] = "marina@example.test"
    message["Cc"] = "river@example.test"
    message["Subject"] = "Fictional draft"
    message.set_content("Draft body")
    return message.as_bytes()


async def test_compose_entry_points_ignore_empty_selection() -> None:
    app, *_ = build_pony_app(label="main-compose-empty")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen.compose_reply()
        screen.compose_reply_all()
        screen.compose_forward()
        screen.compose_from_draft()


async def test_reply_and_forward_report_mirror_read_failures() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="main-compose-read-failure",
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        mirrors["acct"].get_message_bytes = Mock(
            side_effect=OSError("fictional read failure")
        )
        screen.compose_reply()
        screen.compose_reply_all()
        screen.compose_forward()

    assert messages == [
        "Could not load message for reply.",
        "Could not load message for reply.",
        "Could not load message for forward.",
    ]


async def test_harvest_contact_ignores_invalid_and_opens_new_contact() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-contact-harvest",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        panel = screen.query_one(MessageViewPanel)
        screen._contacts = index
        app.push_screen = Mock()

        panel.header_address = Mock(return_value=None)
        screen.action_harvest_contact("1")
        app.push_screen.assert_not_called()

        panel.header_address = Mock(
            return_value=("Marina Rivera", " Marina.Rivera@Example.Test ")
        )
        screen.action_harvest_contact("1")
        edit_screen = app.push_screen.call_args.args[0]
        assert isinstance(edit_screen, ContactEditScreen)
        contact = edit_screen._contact
        assert contact.first_name == "Marina"
        assert contact.last_name == "Rivera"
        assert contact.emails == ("marina.rivera@example.test",)


async def test_harvest_existing_nameless_contact_uses_display_name() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-contact-existing",
        seed=[(folder, plain_text())],
    )
    index.upsert_contact(
        contact=Contact(
            id=None,
            first_name="",
            last_name="",
            emails=("marina@example.test",),
        )
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        screen._contacts = index
        panel = screen.query_one(MessageViewPanel)
        panel.header_address = Mock(
            return_value=("Marina del Río", "marina@example.test")
        )
        app.push_screen = Mock()
        screen.action_harvest_contact("1")

        edit_screen = app.push_screen.call_args.args[0]
        contact = edit_screen._contact
        assert contact.first_name == "Marina del"
        assert contact.last_name == "Río"


async def test_local_draft_completion_deletes_original() -> None:
    folder = FolderRef(account_name="acct", folder_name="Drafts")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-draft-local",
        seed=[(folder, _draft_bytes())],
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._current_folder_ref = folder
        panel = screen.query_one(MessageListPanel)
        panel.load_folder(folder)
        await panel.wait_for_load_complete()
        current = screen.get_current_message()
        assert current is not None and current.uid is None

        app.push_screen = Mock()
        screen.compose_from_draft()
        callback = app.push_screen.call_args.args[1]
        callback(None)
        assert index.get_message(message_ref=current.message_ref) is not None
        callback(False)
        assert index.get_message(message_ref=current.message_ref) is None


async def test_remote_draft_completion_marks_original_trashed() -> None:
    folder = FolderRef(account_name="acct", folder_name="Drafts")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-draft-remote",
        seed=[(folder, _draft_bytes())],
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._current_folder_ref = folder
        panel = screen.query_one(MessageListPanel)
        panel.load_folder(folder)
        await panel.wait_for_load_complete()
        current = screen.get_current_message()
        assert current is not None
        remote = dataclasses.replace(current, uid=42)
        index.update_message(message=remote)

        app.push_screen = Mock()
        screen.compose_from_draft()
        callback = app.push_screen.call_args.args[1]
        callback(True)
        updated = index.get_message(message_ref=current.message_ref)
        assert updated is not None
        assert updated.local_status == MessageStatus.TRASHED
