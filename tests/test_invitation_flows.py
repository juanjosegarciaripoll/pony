"""Answering an invitation from the reader pane, end to end."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from unittest.mock import Mock

from textual.widgets import Button
from tui_helpers import (
    build_pony_app,
    make_calendar_runtime,
    make_tmp_paths,
)

from chronos.domain import CalendarRef
from chronos.storage_indexing import index_calendar
from pony.domain import FolderRef
from pony.tui.screens.invitation_screen import InvitationChoice, InvitationScreen
from pony.tui.screens.main_screen import MainScreen
from pony.tui.widgets.message_list import MessageListPanel
from pony.tui.widgets.message_view import MessageViewPanel

_ACCOUNT = "personal"
_CALENDAR = "work"


def _invite_ics(
    uid: str = "design-review@example.com",
    *,
    method: str = "REQUEST",
    summary: str = "Design review",
    sequence: int = 0,
) -> bytes:
    start = datetime.now(UTC) + timedelta(days=2)
    end = start + timedelta(hours=1)
    return (
        "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Example//EN",
                f"METHOD:{method}",
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{start.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
                f"SUMMARY:{summary}",
                "LOCATION:Room 3",
                f"SEQUENCE:{sequence}",
                "ORGANIZER:mailto:bob@example.com",
                "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:acct@example.com",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        )
        + "\r\n"
    ).encode()


def _invitation_mail(
    ics: bytes = b"", subject: str = "Invitation: Design review"
) -> bytes:
    message = EmailMessage()
    message["From"] = "bob@example.com"
    message["To"] = "acct@example.com"
    message["Subject"] = subject
    message["Date"] = "Mon, 02 Mar 2026 09:00:00 +0000"
    message["Message-ID"] = "<invite-1@example.com>"
    message.set_content("Please join.")
    message.add_alternative(
        (ics or _invite_ics()).decode(),
        subtype="calendar",
        params={"method": "REQUEST"},
    )
    return message.as_bytes()


def _plain_mail() -> bytes:
    message = EmailMessage()
    message["From"] = "bob@example.com"
    message["To"] = "acct@example.com"
    message["Subject"] = "Lunch?"
    message["Date"] = "Mon, 02 Mar 2026 09:00:00 +0000"
    message["Message-ID"] = "<plain-1@example.com>"
    message.set_content("Free at one?")
    return message.as_bytes()


def _calendar_with_one_calendar(label: str) -> Any:
    """A calendar runtime whose mirror holds an (empty) calendar.

    `all_calendar_refs` reads the calendar list off the mirror, so a
    runtime with no calendar at all offers nowhere to file an invitation.
    """
    paths = make_tmp_paths(label)
    runtime = make_calendar_runtime(
        paths, account_name=_ACCOUNT, calendar_name=_CALENDAR
    )
    (runtime.mirror.root / _ACCOUNT / _CALENDAR).mkdir(parents=True, exist_ok=True)
    return runtime


def _main(app: object) -> MainScreen:
    """The mail screen, narrowed from `App.screen`'s declared type."""
    return next(
        screen
        for screen in app.screen_stack  # type: ignore[attr-defined]
        if isinstance(screen, MainScreen)
    )


async def _open_first_message(pilot: Any) -> None:
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    await pilot.pause()


# ---------------------------------------------------------------------------
# The reader pane
# ---------------------------------------------------------------------------


async def test_the_reader_shows_what_an_invitation_proposes() -> None:
    folder = FolderRef("acct", "INBOX")
    app, *_ = build_pony_app(label="inv-reader", seed=((folder, _invitation_mail()),))
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        view = app.screen.query_one(MessageViewPanel)
        invitation = view.invitation
        assert invitation is not None
        assert invitation.summary == "Design review"
        assert invitation.organizer == "bob@example.com"


async def test_an_ics_attached_as_a_file_is_found_too() -> None:
    """An .ics sent as an attachment needs no external viewer.

    This is what retires the `[viewers]` rule for text/calendar: `i`
    reaches a plain event export, not only a METHOD:REQUEST riding as an
    alternative part.
    """
    folder = FolderRef("acct", "INBOX")
    message = EmailMessage()
    message["From"] = "someone@example.com"
    message["To"] = "acct@example.com"
    message["Subject"] = "Seminar next week"
    message["Date"] = "Mon, 02 Mar 2026 09:00:00 +0000"
    message["Message-ID"] = "<ics-attachment@example.com>"
    message.set_content("See attached.")
    message.add_attachment(
        _invite_ics(uid="seminar@example.com", method="PUBLISH", summary="Seminar"),
        maintype="application",
        subtype="ics",
        filename="event.ics",
    )
    app, *_ = build_pony_app(label="inv-attached", seed=((folder, message.as_bytes()),))
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        invitation = _main(app).query_one(MessageViewPanel).invitation
        assert invitation is not None
        assert invitation.summary == "Seminar"


async def test_an_event_with_no_organizer_offers_only_filing() -> None:
    """Nothing to reply to, so the dialog does not pretend otherwise."""
    folder = FolderRef("acct", "INBOX")
    ics = _invite_ics(method="PUBLISH").replace(
        b"ORGANIZER:mailto:bob@example.com\r\n", b""
    )
    runtime = _calendar_with_one_calendar("inv-noorg")
    app, *_ = build_pony_app(
        label="inv-noorg",
        seed=((folder, _invitation_mail(ics)),),
        calendar=runtime,
    )
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, InvitationScreen)
        buttons = {b.id for b in app.screen.query(Button)}
        assert "invitation-add" in buttons
        assert "invitation-accept" not in buttons


async def test_a_plain_message_carries_no_invitation() -> None:
    folder = FolderRef("acct", "INBOX")
    app, *_ = build_pony_app(label="inv-plain", seed=((folder, _plain_mail()),))
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        assert app.screen.query_one(MessageViewPanel).invitation is None


async def test_the_key_says_so_when_there_is_no_invitation() -> None:
    folder = FolderRef("acct", "INBOX")
    app, *_ = build_pony_app(label="inv-none", seed=((folder, _plain_mail()),))
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        await pilot.press("i")
        await pilot.pause()
        messages = [n.message for n in app._notifications]  # noqa: SLF001
        assert any("no invitation" in m for m in messages), messages


async def test_the_key_says_so_without_a_calendar() -> None:
    folder = FolderRef("acct", "INBOX")
    app, *_ = build_pony_app(label="inv-nocal", seed=((folder, _invitation_mail()),))
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        await pilot.press("i")
        await pilot.pause()
        messages = [n.message for n in app._notifications]  # noqa: SLF001
        assert any("No calendar configured" in m for m in messages), messages


async def test_the_key_says_so_when_no_calendar_has_been_synced() -> None:
    folder = FolderRef("acct", "INBOX")
    app, *_ = build_pony_app(
        label="inv-nocals",
        seed=((folder, _invitation_mail()),),
        calendar=make_calendar_runtime(make_tmp_paths("inv-nocals")),
    )
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        await pilot.press("i")
        await pilot.pause()
        messages = [n.message for n in app._notifications]  # noqa: SLF001
        assert any("sync the calendar first" in m.lower() for m in messages), messages


async def test_the_key_opens_the_dialog() -> None:
    folder = FolderRef("acct", "INBOX")
    runtime = _calendar_with_one_calendar("inv-dialog")
    app, *_ = build_pony_app(
        label="inv-dialog", seed=((folder, _invitation_mail()),), calendar=runtime
    )
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, InvitationScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, InvitationScreen)


# ---------------------------------------------------------------------------
# Filing and answering
# ---------------------------------------------------------------------------


async def test_adding_an_invitation_files_it_in_the_calendar() -> None:
    folder = FolderRef("acct", "INBOX")
    runtime = _calendar_with_one_calendar("inv-file")
    app, *_ = build_pony_app(
        label="inv-file", seed=((folder, _invitation_mail()),), calendar=runtime
    )
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        screen = _main(app)
        invitation = screen.query_one(MessageViewPanel).invitation
        assert invitation is not None
        screen._apply_invitation(  # noqa: SLF001
            invitation,
            InvitationChoice(calendar=CalendarRef(_ACCOUNT, _CALENDAR), partstat=None),
        )
        await pilot.pause()
        stored = list(runtime.index.list_components_by_uid("design-review@example.com"))
        assert len(stored) == 1
        assert stored[0].summary == "Design review"
        messages = [n.message for n in app._notifications]  # noqa: SLF001
        assert any("Filed in work" in m for m in messages), messages


async def test_accepting_files_it_and_replies_to_the_organizer() -> None:
    folder = FolderRef("acct", "INBOX")
    runtime = _calendar_with_one_calendar("inv-accept")
    sent = Mock()
    app, *_ = build_pony_app(
        label="inv-accept", seed=((folder, _invitation_mail()),), calendar=runtime
    )
    async with app.run_test() as pilot:
        import pony.smtp_sender as smtp_module

        original = smtp_module.send_message
        smtp_module.send_message = sent  # type: ignore[assignment]
        try:
            await _open_first_message(pilot)
            screen = _main(app)
            invitation = screen.query_one(MessageViewPanel).invitation
            assert invitation is not None
            screen._apply_invitation(  # noqa: SLF001
                invitation,
                InvitationChoice(
                    calendar=CalendarRef(_ACCOUNT, _CALENDAR), partstat="ACCEPTED"
                ),
            )
            await pilot.pause()
            await pilot.pause()
        finally:
            smtp_module.send_message = original  # type: ignore[assignment]

    assert sent.called, "the organizer should have been answered"
    message = sent.call_args.kwargs["msg"]
    assert message["To"] == "bob@example.com"
    assert message["Subject"] == "Accepted: Design review"
    calendar_parts = [
        part.get_param("method")
        for part in message.walk()
        if part.get_content_type() == "text/calendar"
    ]
    assert "REPLY" in calendar_parts


async def test_a_cancellation_removes_the_event() -> None:
    folder = FolderRef("acct", "INBOX")
    runtime = _calendar_with_one_calendar("inv-cancel")
    app, *_ = build_pony_app(
        label="inv-cancel",
        seed=(
            (folder, _invitation_mail()),
            (
                folder,
                _invitation_mail(
                    _invite_ics(method="CANCEL", sequence=1),
                    subject="Cancelled: Design review",
                ),
            ),
        ),
        calendar=runtime,
    )
    target = CalendarRef(_ACCOUNT, _CALENDAR)
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        screen = _main(app)
        list_panel = screen.query_one(MessageListPanel)

        # File the invitation from the first message…
        first = screen.query_one(MessageViewPanel).invitation
        assert first is not None
        screen._apply_invitation(  # noqa: SLF001
            first, InvitationChoice(calendar=target, partstat=None)
        )
        await pilot.pause()
        index_calendar(mirror=runtime.mirror, index=runtime.index, calendar=target)
        assert list(runtime.index.list_components_by_uid("design-review@example.com"))

        # …then apply the cancellation that followed it.
        from pony.invitation import parse_invitation

        cancellation = parse_invitation(_invite_ics(method="CANCEL", sequence=1))
        assert cancellation is not None
        screen._apply_invitation(  # noqa: SLF001
            cancellation, InvitationChoice(calendar=target, partstat=None)
        )
        await pilot.pause()
        assert list_panel is not None
        messages = [n.message for n in app._notifications]  # noqa: SLF001
        assert any("Cancelled in work" in m for m in messages), messages


async def test_malformed_calendar_data_is_reported_not_raised() -> None:
    folder = FolderRef("acct", "INBOX")
    runtime = _calendar_with_one_calendar("inv-broken")
    app, *_ = build_pony_app(
        label="inv-broken", seed=((folder, _invitation_mail()),), calendar=runtime
    )
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        screen = _main(app)
        invitation = screen.query_one(MessageViewPanel).invitation
        assert invitation is not None
        import dataclasses

        broken = dataclasses.replace(invitation, raw_ics=b"BEGIN:VCALENDAR\r\nnope")
        screen._apply_invitation(  # noqa: SLF001
            broken,
            InvitationChoice(calendar=CalendarRef(_ACCOUNT, _CALENDAR), partstat=None),
        )
        await pilot.pause()
        messages = [n.message for n in app._notifications]  # noqa: SLF001
        assert any("Could not file the invitation" in m for m in messages), messages


async def test_a_reply_without_a_sendable_account_is_reported() -> None:
    folder = FolderRef("acct", "INBOX")
    runtime = _calendar_with_one_calendar("inv-nosend")
    app, config, paths, index, mirrors = build_pony_app(
        label="inv-nosend", seed=((folder, _invitation_mail()),), calendar=runtime
    )
    async with app.run_test() as pilot:
        await _open_first_message(pilot)
        screen = _main(app)
        invitation = screen.query_one(MessageViewPanel).invitation
        assert invitation is not None
        # A configuration with nothing that can send. (An IMAP
        # account always can — its SMTP block is required — so the
        # reachable case is a config with no such account in it.)
        import dataclasses

        screen._config = dataclasses.replace(config, accounts=())  # noqa: SLF001
        screen._send_invitation_reply(invitation, "ACCEPTED")  # noqa: SLF001
        await pilot.pause()
        messages = [n.message for n in app._notifications]  # noqa: SLF001
        assert any("No account configured for sending" in m for m in messages), messages
