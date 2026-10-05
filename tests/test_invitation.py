"""Reading invitations out of mail and writing the answers back."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy

from pony.invitation import (
    PARTSTAT_ACCEPTED,
    PARTSTAT_DECLINED,
    PARTSTAT_TENTATIVE,
    build_itip_message,
    extract_invitation,
    find_calendar_payload,
    parse_invitation,
    reply_body,
    reply_ics,
    reply_subject,
    request_body,
    request_subject,
)

_NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)

_REQUEST_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Example//EN\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
UID:design-review@example.com\r
DTSTAMP:20260302T090000Z\r
DTSTART:20260305T140000Z\r
DTEND:20260305T150000Z\r
SUMMARY:Design review\r
LOCATION:Room 3\r
SEQUENCE:2\r
ORGANIZER:mailto:bob@example.com\r
ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION:mailto:me@example.com\r
ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION:mailto:ana@example.com\r
END:VEVENT\r
END:VCALENDAR\r
"""

_CANCEL_ICS = _REQUEST_ICS.replace(b"METHOD:REQUEST", b"METHOD:CANCEL")


def _message_with_calendar(
    ics: bytes = _REQUEST_ICS,
    *,
    content_subtype: str = "calendar",
    maintype: str = "text",
) -> bytes:
    message = EmailMessage()
    message["From"] = "bob@example.com"
    message["To"] = "me@example.com"
    message["Subject"] = "Invitation: Design review"
    message.set_content("Please join.")
    if maintype == "text":
        message.add_alternative(
            ics.decode(), subtype=content_subtype, params={"method": "REQUEST"}
        )
    else:
        message.add_attachment(
            ics, maintype=maintype, subtype=content_subtype, filename="invite.ics"
        )
    return message.as_bytes()


def _plain_message() -> bytes:
    message = EmailMessage()
    message["From"] = "bob@example.com"
    message["To"] = "me@example.com"
    message["Subject"] = "Lunch?"
    message.set_content("Free at one?")
    return message.as_bytes()


class FindCalendarPayloadTest(unittest.TestCase):
    def test_a_plain_message_carries_none(self) -> None:
        self.assertIsNone(find_calendar_payload(_plain_message()))

    def test_a_text_calendar_alternative_is_found(self) -> None:
        payload = find_calendar_payload(_message_with_calendar())
        assert payload is not None
        self.assertIn(b"design-review@example.com", payload)

    def test_an_application_ics_attachment_is_found_too(self) -> None:
        payload = find_calendar_payload(
            _message_with_calendar(maintype="application", content_subtype="ics")
        )
        assert payload is not None
        self.assertIn(b"design-review@example.com", payload)

    def test_unparseable_bytes_carry_none(self) -> None:
        self.assertIsNone(find_calendar_payload(b"\xff\xfe not a message"))

    def test_an_empty_calendar_part_is_ignored(self) -> None:
        message = EmailMessage()
        message["From"] = "bob@example.com"
        message["Subject"] = "Empty"
        message.set_content("Body")
        message.add_alternative("   ", subtype="calendar")
        self.assertIsNone(find_calendar_payload(message.as_bytes()))


class ParseInvitationTest(unittest.TestCase):
    def test_a_request_is_projected_in_full(self) -> None:
        invitation = parse_invitation(_REQUEST_ICS)
        assert invitation is not None
        self.assertEqual("REQUEST", invitation.method)
        self.assertEqual("design-review@example.com", invitation.uid)
        self.assertEqual("Design review", invitation.summary)
        self.assertEqual("bob@example.com", invitation.organizer)
        self.assertEqual(("me@example.com", "ana@example.com"), invitation.attendees)
        self.assertEqual("Room 3", invitation.location)
        self.assertEqual(2, invitation.sequence)
        self.assertEqual(datetime(2026, 3, 5, 14, 0, tzinfo=UTC), invitation.starts_at)
        self.assertTrue(invitation.is_request)
        self.assertFalse(invitation.is_cancellation)

    def test_a_cancellation_is_recognised(self) -> None:
        invitation = parse_invitation(_CANCEL_ICS)
        assert invitation is not None
        self.assertTrue(invitation.is_cancellation)
        self.assertFalse(invitation.is_request)
        self.assertEqual("Cancelled: Design review", invitation.headline())

    def test_a_reply_is_recognised(self) -> None:
        invitation = parse_invitation(
            _REQUEST_ICS.replace(b"METHOD:REQUEST", b"METHOD:REPLY")
        )
        assert invitation is not None
        self.assertTrue(invitation.is_reply)
        self.assertEqual("Reply: Design review", invitation.headline())

    def test_a_calendar_without_a_method_counts_as_a_request(self) -> None:
        invitation = parse_invitation(_REQUEST_ICS.replace(b"METHOD:REQUEST\r\n", b""))
        assert invitation is not None
        self.assertEqual("", invitation.method)
        self.assertTrue(invitation.is_request)

    def test_malformed_calendar_data_is_not_an_invitation(self) -> None:
        self.assertIsNone(parse_invitation(b"BEGIN:VCALENDAR\r\nnonsense"))

    def test_a_calendar_without_a_uid_is_not_an_invitation(self) -> None:
        self.assertIsNone(
            parse_invitation(
                _REQUEST_ICS.replace(b"UID:design-review@example.com\r\n", b"")
            )
        )

    def test_a_recurrence_override_does_not_displace_the_master(self) -> None:
        with_override = _REQUEST_ICS.replace(
            b"END:VCALENDAR",
            b"BEGIN:VEVENT\r\nUID:design-review@example.com\r\n"
            b"RECURRENCE-ID:20260312T140000Z\r\nDTSTAMP:20260302T090000Z\r\n"
            b"DTSTART:20260312T150000Z\r\nSUMMARY:Moved\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR",
        )
        invitation = parse_invitation(with_override)
        assert invitation is not None
        self.assertEqual("Design review", invitation.summary)

    def test_describe_lists_what_is_proposed(self) -> None:
        invitation = parse_invitation(_REQUEST_ICS)
        assert invitation is not None
        lines = invitation.describe()
        self.assertEqual("Invitation: Design review", lines[0])
        joined = "\n".join(lines)
        self.assertIn("Where: Room 3", joined)
        self.assertIn("Organizer: bob@example.com", joined)
        self.assertIn("ana@example.com", joined)

    def test_describe_omits_what_is_missing(self) -> None:
        bare = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Example//EN\r
BEGIN:VEVENT\r
UID:bare@example.com\r
DTSTAMP:20260302T090000Z\r
SUMMARY:Bare\r
END:VEVENT\r
END:VCALENDAR\r
"""
        invitation = parse_invitation(bare)
        assert invitation is not None
        self.assertEqual(("Invitation: Bare",), invitation.describe())


class ExtractInvitationTest(unittest.TestCase):
    def test_a_message_with_an_invitation(self) -> None:
        invitation = extract_invitation(_message_with_calendar())
        assert invitation is not None
        self.assertEqual("Design review", invitation.summary)

    def test_a_plain_message_has_none(self) -> None:
        self.assertIsNone(extract_invitation(_plain_message()))

    def test_a_message_whose_calendar_part_is_rubbish_has_none(self) -> None:
        message = EmailMessage()
        message["From"] = "bob@example.com"
        message["Subject"] = "Broken"
        message.set_content("Body")
        message.add_alternative("BEGIN:VCALENDAR\nbroken", subtype="calendar")
        self.assertIsNone(extract_invitation(message.as_bytes()))


class ReplyTest(unittest.TestCase):
    def _invitation(self) -> object:
        invitation = parse_invitation(_REQUEST_ICS)
        assert invitation is not None
        return invitation

    def test_an_acceptance_carries_the_identity_of_the_invitation(self) -> None:
        ics = reply_ics(
            self._invitation(),  # type: ignore[arg-type]
            attendee="me@example.com",
            partstat=PARTSTAT_ACCEPTED,
            now=_NOW,
        )
        text = ics.decode()
        self.assertIn("METHOD:REPLY", text)
        self.assertIn("UID:design-review@example.com", text)
        # SEQUENCE is what lets the organizer ignore an answer to a
        # superseded invitation.
        self.assertIn("SEQUENCE:2", text)
        self.assertIn("ORGANIZER:mailto:bob@example.com", text)
        self.assertIn("ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@example.com", text)
        self.assertIn("DTSTAMP:20260302T090000Z", text)

    def test_a_decline_says_so(self) -> None:
        ics = reply_ics(
            self._invitation(),  # type: ignore[arg-type]
            attendee="me@example.com",
            partstat=PARTSTAT_DECLINED,
            now=_NOW,
        )
        self.assertIn("PARTSTAT=DECLINED", ics.decode())

    def test_a_display_name_is_reduced_to_the_address(self) -> None:
        ics = reply_ics(
            self._invitation(),  # type: ignore[arg-type]
            attendee="Me Myself <me@example.com>",
            partstat=PARTSTAT_TENTATIVE,
            now=_NOW,
        )
        self.assertIn("mailto:me@example.com", ics.decode())

    def test_an_unknown_status_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            reply_ics(
                self._invitation(),  # type: ignore[arg-type]
                attendee="me@example.com",
                partstat="MAYBE",
                now=_NOW,
            )

    def test_the_subject_names_the_answer_and_the_event(self) -> None:
        self.assertEqual(
            "Accepted: Design review",
            reply_subject(self._invitation(), PARTSTAT_ACCEPTED),  # type: ignore[arg-type]
        )
        self.assertEqual(
            "Declined: Design review",
            reply_subject(self._invitation(), PARTSTAT_DECLINED),  # type: ignore[arg-type]
        )

    def test_the_body_reads_as_a_sentence(self) -> None:
        body = reply_body(
            self._invitation(),  # type: ignore[arg-type]
            attendee="me@example.com",
            partstat=PARTSTAT_ACCEPTED,
        )
        self.assertIn("me@example.com has accepted", body)


class RequestTextTest(unittest.TestCase):
    def test_the_subject_names_the_event(self) -> None:
        self.assertEqual("Invitation: Standup", request_subject("Standup"))

    def test_the_body_describes_the_proposal(self) -> None:
        body = request_body(
            "Standup",
            starts_at=datetime(2026, 3, 5, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 3, 5, 15, 0, tzinfo=UTC),
            location="Room 3",
            description="Bring the deck.",
        )
        self.assertIn("'Standup'", body)
        self.assertIn("When:", body)
        self.assertIn("Where: Room 3", body)
        self.assertIn("Bring the deck.", body)

    def test_the_body_omits_what_is_missing(self) -> None:
        body = request_body("Standup", starts_at=None, ends_at=None)
        self.assertNotIn("When:", body)
        self.assertNotIn("Where:", body)


class BuildItipMessageTest(unittest.TestCase):
    def _built(self, method: str = "REQUEST") -> EmailMessage:
        message = build_itip_message(
            from_address="me@example.com",
            to_addresses=["bob@example.com", "ana@example.com"],
            subject="Invitation: Design review",
            body_text="You are invited.",
            ics=_REQUEST_ICS,
            method=method,
        )
        parsed = message_from_bytes(message.as_bytes(), policy=default_policy)
        assert isinstance(parsed, EmailMessage)
        return parsed

    def test_headers_name_the_sender_and_recipients(self) -> None:
        message = self._built()
        self.assertEqual("me@example.com", message["From"])
        self.assertEqual("bob@example.com, ana@example.com", message["To"])
        self.assertEqual("Invitation: Design review", message["Subject"])
        self.assertTrue(message["Message-ID"])

    def test_the_calendar_part_carries_the_method(self) -> None:
        # A receiving client offers accept/decline off this parameter,
        # which is why the part is an alternative and not just a file.
        types = [
            (part.get_content_type(), part.get_param("method"))
            for part in self._built().walk()
        ]
        self.assertIn(("text/calendar", "REQUEST"), types)

    def test_a_reply_carries_its_own_method(self) -> None:
        types = [
            (part.get_content_type(), part.get_param("method"))
            for part in self._built("REPLY").walk()
        ]
        self.assertIn(("text/calendar", "REPLY"), types)

    def test_the_plain_text_body_survives(self) -> None:
        body = self._built().get_body(preferencelist=("plain",))
        assert body is not None
        self.assertIn("You are invited.", body.get_content())

    def test_the_same_bytes_are_attached_for_clients_that_want_a_file(self) -> None:
        names = [
            part.get_filename() for part in self._built().walk() if part.get_filename()
        ]
        self.assertIn("invite.ics", names)

    def test_an_invitation_needs_a_recipient(self) -> None:
        with self.assertRaises(ValueError):
            build_itip_message(
                from_address="me@example.com",
                to_addresses=[],
                subject="s",
                body_text="b",
                ics=_REQUEST_ICS,
                method="REQUEST",
            )


if __name__ == "__main__":
    unittest.main()
