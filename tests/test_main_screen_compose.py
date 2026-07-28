"""Focused compose, contact, and draft callbacks for ``MainScreen``."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from email.message import EmailMessage
from unittest.mock import Mock

from corpus import mid_thread_message, plain_text, unthreaded_message
from tui_helpers import build_pony_app

from pony.domain import Contact, FolderRef, MessageFlag, MessageStatus
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
    """A three-word name keeps its compound family name.

    This path used to split on the last word alone, giving
    ("Marina del", "Río"); the sync path already split it correctly.
    Both now share one implementation.
    """
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
        assert contact.first_name == "Marina"
        assert contact.last_name == "del Río"


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


def _local_account(paths: object, name: str = "local"):  # type: ignore[no-untyped-def]
    """A local (non-IMAP) account with no SMTP block — it cannot send."""
    from pony.domain import LocalAccountConfig, MirrorConfig

    mirror_dir = paths.data_dir / "mirrors" / name  # type: ignore[attr-defined]
    mirror_dir.mkdir(parents=True, exist_ok=True)
    return LocalAccountConfig(
        name=name,
        email_address=f"{name}@example.test",
        mirror=MirrorConfig(path=mirror_dir, format="maildir"),
    )


async def test_harvest_contact_without_a_contacts_index_does_nothing() -> None:
    """Contact harvesting is inert when the screen has no contacts store."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="main-harvest-no-store",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        screen._contacts = None
        app.push_screen = Mock()

        screen.action_harvest_contact("1")

        app.push_screen.assert_not_called()


async def test_harvest_contact_with_no_display_name_leaves_both_names_empty() -> None:
    """A bare address yields a contact with only the email filled in."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-harvest-bare",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        screen._contacts = index
        panel = screen.query_one(MessageViewPanel)
        panel.header_address = Mock(return_value=("   ", "bare@example.test"))
        app.push_screen = Mock()

        screen.action_harvest_contact("1")

        contact = app.push_screen.call_args.args[0]._contact
        assert contact.first_name == ""
        assert contact.last_name == ""
        assert contact.emails == ("bare@example.test",)


async def test_harvest_contact_with_a_single_word_name_sets_only_the_first() -> None:
    """A mononym is a first name — inventing a surname would be wrong."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-harvest-mononym",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        screen._contacts = index
        panel = screen.query_one(MessageViewPanel)
        panel.header_address = Mock(return_value=("Prince", "prince@example.test"))
        app.push_screen = Mock()

        screen.action_harvest_contact("1")

        contact = app.push_screen.call_args.args[0]._contact
        assert contact.first_name == "Prince"
        assert contact.last_name == ""


async def test_composing_without_a_sendable_account_warns() -> None:
    """With no SMTP anywhere, compose explains instead of opening blank."""
    from tui_helpers import make_tmp_paths

    paths = make_tmp_paths("main-no-smtp")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="main-no-smtp-app",
        accounts=(_local_account(paths),),
    )
    notifications = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        assert screen._sendable_or_notify("Composing") is None

    assert any("requires an IMAP account" in n for n in notifications)


async def test_editing_a_draft_reports_a_mirror_read_failure() -> None:
    """A draft whose bytes cannot be read explains rather than opening empty."""
    folder = FolderRef(account_name="acct", folder_name="Drafts")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="main-draft-unreadable",
        seed=[(folder, _draft_bytes())],
    )
    notifications = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen.query_one(MessageListPanel).load_folder(folder)
        await pilot.pause()

        def _boom(*, folder: FolderRef, storage_key: str) -> bytes:  # noqa: ARG001
            raise OSError("draft unreadable")

        mirrors["acct"].get_message_bytes = _boom  # type: ignore[method-assign]
        screen.compose_from_draft()
        await pilot.pause()

    assert any("Could not load draft." in n for n in notifications)


async def test_editing_a_draft_without_a_mirror_is_a_noop() -> None:
    """An account whose mirror is missing cannot open its drafts."""
    folder = FolderRef(account_name="acct", folder_name="Drafts")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="main-draft-no-mirror",
        seed=[(folder, _draft_bytes())],
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen.query_one(MessageListPanel).load_folder(folder)
        await pilot.pause()
        screen._mirrors = {}
        app.push_screen = Mock()

        screen.compose_from_draft()
        await pilot.pause()

        app.push_screen.assert_not_called()


async def test_a_sent_reply_all_marks_the_original_answered() -> None:
    """Completing the reply-all composer flags the source message."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-reply-all-answered",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        captured: list[Callable[[bool | None], None]] = []

        def _capture(
            _screen: object,
            callback: Callable[[bool | None], None] | None = None,
            **_kw: object,
        ) -> None:
            if callback is not None:
                captured.append(callback)

        app.push_screen = Mock(side_effect=_capture)

        screen.compose_reply_all()
        on_sent = captured[0]

        # Dismissing without sending leaves the flags alone.
        on_sent(None)
        msg = screen.get_current_message()
        assert msg is not None
        assert MessageFlag.ANSWERED not in msg.local_flags

        on_sent(True)
        await pilot.pause()

    reloaded = list(index.list_folder_messages(folder=folder))[0]
    assert MessageFlag.ANSWERED in reloaded.local_flags


async def test_every_compose_entry_point_refuses_without_a_sendable_account() -> None:
    """Compose, reply, reply-all, forward and edit-draft share one guard."""
    from tui_helpers import make_tmp_paths

    paths = make_tmp_paths("main-entry-no-smtp")
    folder = FolderRef(account_name="local", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="main-entry-no-smtp-app",
        accounts=(_local_account(paths),),
        seed=[(folder, plain_text())],
    )
    notifications = _notifications(app)

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        app.push_screen = Mock()

        screen.compose_new()
        screen.compose_reply()
        screen.compose_reply_all()
        screen.compose_forward()
        screen.compose_from_draft()
        await pilot.pause()

        app.push_screen.assert_not_called()

    # One notice per entry point, each naming what it was trying to do.
    assert sum("requires an IMAP account" in n for n in notifications) == 5
    assert any("Composing requires" in n for n in notifications)
    assert any("Replying requires" in n for n in notifications)
    assert any("Forwarding requires" in n for n in notifications)


async def test_a_sent_reply_marks_the_original_answered() -> None:
    """The single-recipient reply shares reply-all's completion callback."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="main-reply-answered",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        captured: list[Callable[[bool | None], None]] = []

        def _capture(
            _screen: object,
            callback: Callable[[bool | None], None] | None = None,
            **_kw: object,
        ) -> None:
            if callback is not None:
                captured.append(callback)

        app.push_screen = Mock(side_effect=_capture)

        screen.compose_reply()
        on_sent = captured[0]

        on_sent(None)
        msg = screen.get_current_message()
        assert msg is not None
        assert MessageFlag.ANSWERED not in msg.local_flags

        on_sent(True)
        await pilot.pause()

    reloaded = list(index.list_folder_messages(folder=folder))[0]
    assert MessageFlag.ANSWERED in reloaded.local_flags


async def test_link_actions_ignore_the_wrong_link_kind() -> None:
    """A mailto: link is not a web link, and vice versa."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="main-link-kinds",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        panel = screen.query_one(MessageViewPanel)
        app.push_screen = Mock()

        # activate_link only opens web links.
        panel.body_link = Mock(return_value=("mail", "someone@example.test"))
        screen.action_activate_link("1")
        app.push_screen.assert_not_called()

        # compose_link only handles mail links.
        panel.body_link = Mock(return_value=("web", "https://example.test"))
        screen.action_compose_link("1")
        app.push_screen.assert_not_called()

        # Nothing at that index at all.
        panel.body_link = Mock(return_value=None)
        screen.action_activate_link("1")
        screen.action_compose_link("1")
        await pilot.pause()

        app.push_screen.assert_not_called()


async def test_a_reply_threads_under_the_message_it_answers() -> None:
    """Reply and reply-all both chain onto the parent's References.

    Pony used to send neither header, so every reply landed as a new
    thread in the recipient's client.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-reply-threading",
        seed=[(folder, mid_thread_message())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        captured: list[object] = []

        def _capture(compose_screen: object, *_a: object, **_kw: object) -> None:
            captured.append(compose_screen)

        app.push_screen = Mock(side_effect=_capture)

        for entry in (screen.compose_reply, screen.compose_reply_all):
            captured.clear()
            entry()
            initial = captured[0]._initial  # type: ignore[attr-defined]
            assert initial.in_reply_to == "<thread-third@example.com>"
            # Folded chain is unfolded, and the parent is appended.
            assert initial.references == (
                "<thread-root@example.com> <thread-second@example.com> "
                "<thread-third@example.com>"
            )


async def test_a_forward_starts_its_own_thread() -> None:
    """A forward is not a reply — it must not claim the original's thread."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-forward-threading",
        seed=[(folder, mid_thread_message())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        captured: list[object] = []
        app.push_screen = Mock(side_effect=lambda s, *_a, **_k: captured.append(s))

        screen.compose_forward()
        initial = captured[0]._initial  # type: ignore[attr-defined]
        assert initial.in_reply_to == ""
        assert initial.references == ""


async def test_replying_to_a_message_without_a_message_id_adds_no_headers() -> None:
    """Nothing legitimate to point at, so no threading headers are invented."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-reply-unthreaded",
        seed=[(folder, unthreaded_message())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        captured: list[object] = []
        app.push_screen = Mock(side_effect=lambda s, *_a, **_k: captured.append(s))

        screen.compose_reply()
        initial = captured[0]._initial  # type: ignore[attr-defined]
        assert initial.in_reply_to == ""
        assert initial.references == ""


async def test_a_forward_names_its_attachment_after_the_subject() -> None:
    """The recipient sees the attachment name, so it must be meaningful.

    The forwarded copy used to be a bare ``mkstemp`` name, arriving as
    ``forwarded-message-dn1nwh3p.eml``.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-forward-naming",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        captured: list[object] = []
        app.push_screen = Mock(side_effect=lambda s, *_a, **_k: captured.append(s))

        screen.compose_forward()
        initial = captured[0]._initial  # type: ignore[attr-defined]
        attached = list(initial.attachment_paths)
        assert [p.name for p in attached] == ["Tuesday meeting confirmed.eml"]
        assert attached[0].read_bytes() == plain_text()
        # The composer is told it owns the copy so it can clean up.
        assert tuple(initial.owned_paths) == tuple(attached)
        for path in attached:
            path.unlink()
            path.parent.rmdir()


async def test_a_cancelled_forward_leaves_no_temporary_file_behind() -> None:
    """The forwarded copy is scratch space tied to the composer's lifetime."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-forward-cleanup",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await _select_message(pilot)
        screen = _main(app)
        screen.compose_forward()
        await pilot.pause()

        compose = app.screen
        attached = list(compose._initial.attachment_paths)  # type: ignore[attr-defined]
        assert attached and attached[0].exists()

        app.pop_screen()
        await pilot.pause()

        assert not attached[0].exists(), "forwarded copy outlived the composer"
        assert not attached[0].parent.exists(), "scratch directory was left behind"
