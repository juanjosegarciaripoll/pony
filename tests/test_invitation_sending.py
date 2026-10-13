"""Sending invitations out of the calendar, and completing their attendees.

Sending lives in :mod:`pony.invitation_actions`, beside the inbound
half: it was in ``pony.tui.calendar_host``, which imports Textual, so
the browser could not reach it and inviting attendees from a browser
invited nobody.  Completing an attendee's address still belongs to the
host, because it is the host that has a contact store.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

from tui_helpers import (
    build_pony_app,
    make_calendar_runtime,
    make_test_account,
    make_tmp_paths,
)

from chronos.domain import CalendarRef
from chronos.outcome import Notice
from pony.domain import Contact
from pony.invitation_actions import _with_method, send_event_invitations
from pony.tui.calendar_host import contact_completer

_START = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)

_EVENT_ICS = (
    "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//chronos//EN",
            "BEGIN:VEVENT",
            "UID:review@example.com",
            "DTSTAMP:20260302T090000Z",
            "DTSTART:20260305T140000Z",
            "DTEND:20260305T150000Z",
            "SUMMARY:Design review",
            "LOCATION:Room 3",
            "ORGANIZER:mailto:acct@example.com",
            "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:ana@example.com",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    + "\r\n"
).encode()


class WithMethodTest(unittest.TestCase):
    """A stored event is not a scheduling message until it is posted."""

    def test_a_method_is_added_when_absent(self) -> None:
        out = _with_method(_EVENT_ICS, "REQUEST").decode()
        self.assertIn("METHOD:REQUEST", out)
        self.assertLess(out.index("METHOD:REQUEST"), out.index("BEGIN:VEVENT"))

    def test_an_existing_method_is_left_alone(self) -> None:
        already = _EVENT_ICS.replace(b"BEGIN:VEVENT", b"METHOD:CANCEL\r\nBEGIN:VEVENT")
        self.assertEqual(already, _with_method(already, "REQUEST"))

    def test_bytes_without_an_event_are_returned_unchanged(self) -> None:
        self.assertEqual(b"not ics", _with_method(b"not ics", "REQUEST"))

    def test_an_empty_calendar_is_returned_unchanged(self) -> None:
        bare = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        self.assertEqual(bare, _with_method(bare, "REQUEST"))

    def test_the_method_precedes_a_timezone_component(self) -> None:
        # RFC 5545 puts the calendar's own properties ahead of its
        # components, so a VTIMEZONE must not end up before the METHOD.
        with_tz = _EVENT_ICS.replace(
            b"BEGIN:VEVENT",
            b"BEGIN:VTIMEZONE\r\nTZID:Europe/Madrid\r\nEND:VTIMEZONE\r\nBEGIN:VEVENT",
        )
        out = _with_method(with_tz, "REQUEST").decode()
        self.assertLess(out.index("METHOD:REQUEST"), out.index("BEGIN:VTIMEZONE"))


class SendEventInvitationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = make_tmp_paths("invite-send")
        self.account = make_test_account(self.paths)
        self.sent = Mock()
        import pony.smtp_sender as smtp_module

        self._original = smtp_module.send_message
        smtp_module.send_message = self.sent  # type: ignore[assignment]

        def _restore() -> None:
            smtp_module.send_message = self._original  # type: ignore[assignment]

        self.addCleanup(_restore)

    def _send(self, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "ics": _EVENT_ICS,
            "attendees": ("ana@example.com",),
            "organizer": "acct@example.com",
            "summary": "Design review",
            "is_update": False,
            "account": self.account,
            "password": "secret",
            "connect_timeout": 10,
        }
        kwargs.update(overrides)
        return send_event_invitations(**kwargs)

    def test_the_attendees_are_mailed(self) -> None:
        delivery = self._send()
        self.assertTrue(delivery.ok)
        self.assertEqual(("ana@example.com",), delivery.sent_to)
        message = self.sent.call_args.kwargs["msg"]
        self.assertEqual("ana@example.com", message["To"])
        self.assertEqual("Invitation: Design review", message["Subject"])

    def test_the_calendar_part_is_a_request(self) -> None:
        self._send()
        message = self.sent.call_args.kwargs["msg"]
        methods = [
            part.get_param("method")
            for part in message.walk()
            if part.get_content_type() == "text/calendar"
        ]
        self.assertIn("REQUEST", methods)

    def test_an_update_says_so_in_the_subject(self) -> None:
        self._send(is_update=True)
        message = self.sent.call_args.kwargs["msg"]
        self.assertEqual("Updated invitation: Design review", message["Subject"])

    def test_the_organizer_is_not_invited_to_their_own_event(self) -> None:
        delivery = self._send(
            attendees=("acct@example.com", "ana@example.com"),
            organizer="acct@example.com",
        )
        self.assertEqual(("ana@example.com",), delivery.sent_to)

    def test_the_comparison_ignores_case(self) -> None:
        delivery = self._send(
            attendees=("ACCT@Example.com", "ana@example.com"),
            organizer="acct@example.com",
        )
        self.assertEqual(("ana@example.com",), delivery.sent_to)

    def test_an_event_with_only_the_organizer_sends_nothing(self) -> None:
        delivery = self._send(attendees=("acct@example.com",))
        self.assertTrue(delivery.ok)
        self.assertEqual((), delivery.sent_to)
        self.assertFalse(self.sent.called)

    def test_the_body_describes_the_event(self) -> None:
        self._send()
        message = self.sent.call_args.kwargs["msg"]
        body = message.get_body(preferencelist=("plain",))
        assert body is not None
        text = body.get_content()
        self.assertIn("Design review", text)
        self.assertIn("Room 3", text)

    def test_an_smtp_failure_is_reported_not_raised(self) -> None:
        from pony.smtp_sender import SMTPError

        self.sent.side_effect = SMTPError("smtp.example.com refused the message")
        delivery = self._send()
        self.assertFalse(delivery.ok)
        assert delivery.error is not None
        self.assertIn("refused", delivery.error)


class ContactCompleterTest(unittest.TestCase):
    def setUp(self) -> None:
        _app, _config, _paths, self.index, _mirrors = build_pony_app(label="completer")
        self.index.upsert_contact(
            contact=Contact(
                id=None,
                first_name="Ana",
                last_name="Lopez",
                emails=("ana@example.com", "ana.lopez@work.example.com"),
            )
        )
        self.complete = contact_completer(self.index)

    def test_a_prefix_finds_the_contact(self) -> None:
        self.assertIn("Ana Lopez <ana@example.com>", list(self.complete("ana")))

    def test_every_address_of_a_contact_is_offered(self) -> None:
        suggestions = list(self.complete("lopez"))
        self.assertEqual(2, len(suggestions))

    def test_one_letter_is_not_enough(self) -> None:
        self.assertEqual((), self.complete("a"))
        self.assertEqual((), self.complete(" "))

    def test_the_limit_is_respected(self) -> None:
        self.assertEqual(1, len(self.complete("lopez", limit=1)))

    def test_an_unknown_prefix_finds_nothing(self) -> None:
        self.assertEqual((), self.complete("zzzz"))


def _suggester() -> object:
    """The editor's inline completer, over a fixed address book."""
    from chronos.tui.screens.event_edit_screen import _AttendeeSuggester

    def _completer(prefix: str, *, limit: int = 10) -> tuple[str, ...]:
        known = ("Ana Lopez <ana@example.com>", "Bob <bob@example.com>")
        matches = tuple(a for a in known if prefix.lower() in a.lower())
        return matches[:limit]

    return _AttendeeSuggester(_completer)


async def test_the_first_match_is_offered() -> None:
    suggester = _suggester()
    assert (
        await suggester.get_suggestion("ana")  # type: ignore[attr-defined]
        == "Ana Lopez <ana@example.com>"
    )


async def test_addresses_already_entered_are_left_alone() -> None:
    suggester = _suggester()
    assert (
        await suggester.get_suggestion("bob@example.com, ana")  # type: ignore[attr-defined]
        == "bob@example.com, Ana Lopez <ana@example.com>"
    )


async def test_one_letter_offers_nothing() -> None:
    suggester = _suggester()
    assert await suggester.get_suggestion("a") is None  # type: ignore[attr-defined]


async def test_an_unknown_prefix_offers_nothing() -> None:
    suggester = _suggester()
    assert await suggester.get_suggestion("zzz") is None  # type: ignore[attr-defined]


async def test_the_calendar_is_given_a_sender_and_a_completer() -> None:
    runtime = make_calendar_runtime(make_tmp_paths("wiring"))
    app, *_ = build_pony_app(label="wiring", calendar=runtime, with_contacts=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        services = app.calendar_services
        assert services.invitation_sender is not None
        assert services.attendee_completer is not None
    runtime.close()


async def test_saving_an_event_with_attendees_posts_the_invitation() -> None:
    runtime = make_calendar_runtime(make_tmp_paths("posting"))
    app, *_ = build_pony_app(label="posting", calendar=runtime)
    sent = Mock()
    async with app.run_test() as pilot:
        import pony.smtp_sender as smtp_module

        original = smtp_module.send_message
        smtp_module.send_message = sent  # type: ignore[assignment]
        try:
            await pilot.pause()
            sender = app.calendar_services.invitation_sender
            assert sender is not None
            sender(
                ics=_EVENT_ICS,
                attendees=("ana@example.com",),
                organizer="acct@example.com",
                summary="Design review",
                is_update=False,
            )
            await pilot.pause()
            await pilot.pause()
        finally:
            smtp_module.send_message = original  # type: ignore[assignment]
    runtime.close()
    assert sent.called
    assert sent.call_args.kwargs["msg"]["To"] == "ana@example.com"


async def test_an_event_with_no_attendees_sends_nothing() -> None:
    runtime = make_calendar_runtime(make_tmp_paths("no-attendees"))
    app, *_ = build_pony_app(label="no-attendees", calendar=runtime)
    sent = Mock()
    async with app.run_test() as pilot:
        import pony.smtp_sender as smtp_module

        original = smtp_module.send_message
        smtp_module.send_message = sent  # type: ignore[assignment]
        try:
            await pilot.pause()
            sender = app.calendar_services.invitation_sender
            assert sender is not None
            sender(
                ics=_EVENT_ICS,
                attendees=(),
                organizer="acct@example.com",
                summary="Design review",
                is_update=False,
            )
            await pilot.pause()
        finally:
            smtp_module.send_message = original  # type: ignore[assignment]
    runtime.close()
    assert not sent.called


def test_the_calendar_screen_passes_the_completer_to_its_editor() -> None:
    """The editor is given whatever completer the services carry."""
    from chronos.tui.screens.event_edit_screen import EventEditScreen

    def _completer(prefix: str, *, limit: int = 10) -> tuple[str, ...]:  # noqa: ARG001
        return ()

    screen = EventEditScreen(
        calendars=(CalendarRef("personal", "work"),),
        existing=None,
        default_calendar=CalendarRef("personal", "work"),
        on_save=lambda _draft: None,
        attendee_completer=_completer,
    )
    assert screen._attendee_completer is _completer  # noqa: SLF001


def test_an_editor_without_a_completer_is_still_valid() -> None:
    from chronos.tui.screens.event_edit_screen import EventEditScreen

    screen = EventEditScreen(
        calendars=(CalendarRef("personal", "work"),),
        existing=None,
        default_calendar=CalendarRef("personal", "work"),
        on_save=lambda _draft: None,
    )
    assert screen._attendee_completer is None  # noqa: SLF001


class SendingIsReachableWithoutATerminalTest(unittest.TestCase):
    """The whole point of the move, stated as a test.

    `send_event_invitations` sat in `pony.tui.calendar_host`, which
    imports `textual.app.App`, so `pony.web` could not call it: the
    browser must not import anything under a `tui` package, and
    `tests/test_layering.py` enforces that.  Saving an event with
    attendees from the browser therefore invited nobody, silently.
    """

    def test_it_is_importable_from_a_module_that_loads_no_textual(self) -> None:
        import subprocess
        import sys

        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-c",
                "import sys;"
                " from pony.invitation_actions import send_event_invitations;"
                " assert callable(send_event_invitations);"
                " raise SystemExit(1 if 'textual' in sys.modules else 0)",
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            0,
            result.returncode,
            result.stderr.decode(errors="replace") or "it dragged in textual",
        )

    def test_the_host_no_longer_carries_it(self) -> None:
        import pony.tui.calendar_host as host

        self.assertFalse(hasattr(host, "send_event_invitations"))
        self.assertFalse(hasattr(host, "_with_method"))
        self.assertFalse(hasattr(host, "InvitationDelivery"))

    def test_the_terminal_still_calls_the_one_implementation(self) -> None:
        """`tui/app.py` is a thin caller now, not a second copy."""
        import pony.invitation_actions as actions
        import pony.tui.app as app

        self.assertIs(app.send_event_invitations, actions.send_event_invitations)


def _sendable_account() -> Any:
    return make_test_account(make_tmp_paths("invite-sender"))


def _config_with(*accounts: Any) -> Any:
    from tui_helpers import make_test_config

    return make_test_config(accounts=accounts)


class SenderForInvitationTest(unittest.TestCase):
    """One answer to "who posts this invitation, and with what".

    Both front ends post the same iTIP REQUEST after the same save, and
    each asked the two questions and worded both refusals itself -- the
    terminal said "no account is configured for sending invitations",
    the browser "no invitation was sent: No account configured for
    sending".  Every refusal has to be about the invitation and not
    about the save: the event is already on the server, and a sentence
    that sounds like a failed save invites the user to write it twice.
    """

    def test_an_account_and_its_password_come_back_together(self) -> None:
        from pony.invitation_actions import sender_for_invitation

        account = _sendable_account()
        config = _config_with(account)
        ready = sender_for_invitation(config, _FixedCredentials("s3cret"))

        self.assertEqual(ready, (account, "s3cret"))

    def test_no_sendable_account_is_a_saved_event_with_no_invitation(self) -> None:
        from pony.invitation_actions import sender_for_invitation

        config = _config_with()
        ready = sender_for_invitation(config, _FixedCredentials("s3cret"))

        assert isinstance(ready, Notice)
        self.assertTrue(ready.text.startswith("Event saved, but no invitation"))
        self.assertIn("No account configured for sending", ready.text)

    def test_a_password_that_cannot_be_got_says_the_same_thing(self) -> None:
        from pony.invitation_actions import sender_for_invitation

        ready = sender_for_invitation(
            _config_with(_sendable_account()), _FailingCredentials()
        )

        assert isinstance(ready, Notice)
        self.assertTrue(ready.text.startswith("Event saved, but no invitation"))
        self.assertEqual(ready.severity, "error")


class _FixedCredentials:
    def __init__(self, password: str) -> None:
        self._password = password

    def get_password(self, *, account_name: str) -> str:  # noqa: ARG002
        return self._password


class _FailingCredentials:
    def get_password(self, *, account_name: str) -> str:
        raise RuntimeError(f"locked keyring for {account_name}")
