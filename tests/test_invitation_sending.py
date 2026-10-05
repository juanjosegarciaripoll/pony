"""Sending invitations out of the calendar, and completing their attendees."""

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
from pony.domain import Contact
from pony.tui.calendar_host import (
    _with_method,
    contact_completer,
    send_event_invitations,
)

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


class AttendeeSuggesterTest(unittest.IsolatedAsyncioTestCase):
    """The inline completion offered in the event editor's Invitees field."""

    def setUp(self) -> None:
        from chronos.tui.screens.event_edit_screen import _AttendeeSuggester

        def _completer(prefix: str, *, limit: int = 10) -> tuple[str, ...]:
            known = ("Ana Lopez <ana@example.com>", "Bob <bob@example.com>")
            matches = tuple(a for a in known if prefix.lower() in a.lower())
            return matches[:limit]

        self.suggester = _AttendeeSuggester(_completer)

    async def test_the_first_match_is_offered(self) -> None:
        self.assertEqual(
            "Ana Lopez <ana@example.com>",
            await self.suggester.get_suggestion("ana"),
        )

    async def test_addresses_already_entered_are_left_alone(self) -> None:
        self.assertEqual(
            "bob@example.com, Ana Lopez <ana@example.com>",
            await self.suggester.get_suggestion("bob@example.com, ana"),
        )

    async def test_one_letter_offers_nothing(self) -> None:
        self.assertIsNone(await self.suggester.get_suggestion("a"))

    async def test_an_unknown_prefix_offers_nothing(self) -> None:
        self.assertIsNone(await self.suggester.get_suggestion("zzz"))


class CalendarServicesWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_calendar_is_given_a_sender_and_a_completer(self) -> None:
        paths = make_tmp_paths("wiring")
        runtime = make_calendar_runtime(paths)
        self.addCleanup(runtime.close)
        app, *_ = build_pony_app(label="wiring", calendar=runtime, with_contacts=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            services = app.calendar_services
            self.assertIsNotNone(services.invitation_sender)
            self.assertIsNotNone(services.attendee_completer)

    async def test_saving_an_event_with_attendees_posts_the_invitation(self) -> None:
        paths = make_tmp_paths("posting")
        runtime = make_calendar_runtime(paths)
        self.addCleanup(runtime.close)
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
        self.assertTrue(sent.called)
        self.assertEqual("ana@example.com", sent.call_args.kwargs["msg"]["To"])

    async def test_an_event_with_no_attendees_sends_nothing(self) -> None:
        paths = make_tmp_paths("no-attendees")
        runtime = make_calendar_runtime(paths)
        self.addCleanup(runtime.close)
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
        self.assertFalse(sent.called)


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
