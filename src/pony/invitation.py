"""Meeting invitations: reading them out of mail, and writing them back.

A calendar invitation travels as a ``text/calendar`` part inside an
ordinary message (RFC 5546, "iTIP").  Mail and calendar being one
program is what lets Pony Express do something with it: read the part,
show what it proposes, put it in the calendar, and answer the organizer.

This module is the shared vocabulary for both directions:

* :func:`extract_invitation` finds and parses the part on a message;
* :func:`reply_ics` builds the ``METHOD:REPLY`` that answers it;
* :func:`build_itip_message` wraps either method in a message ready for
  :mod:`pony.smtp_sender`.

It does no I/O: importing an invitation into the calendar is
``chronos.ingest``'s job, and sending is the SMTP sender's.  Parsing
leans on the calendar's own iCalendar reader rather than a second one,
so what the reader pane shows and what the calendar stores cannot
disagree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import formatdate, make_msgid, parseaddr

from chronos.ical_parser import (
    IcalParseError,
    ParsedComponent,
    extract_attendees,
    extract_organizer,
    parse_method,
    parse_vcalendar,
)

# Content types an invitation arrives as.  `text/calendar` is what RFC
# 5546 specifies; `application/ics` is what several clients attach it as
# instead, and the bytes inside are the same.
CALENDAR_CONTENT_TYPES = frozenset({"text/calendar", "application/ics"})

# The participation statuses a reply can carry (RFC 5545 §3.2.12).
PARTSTAT_ACCEPTED = "ACCEPTED"
PARTSTAT_DECLINED = "DECLINED"
PARTSTAT_TENTATIVE = "TENTATIVE"

_REPLY_VERB = {
    PARTSTAT_ACCEPTED: "Accepted",
    PARTSTAT_DECLINED: "Declined",
    PARTSTAT_TENTATIVE: "Tentative",
}


@dataclass(frozen=True, slots=True)
class Invitation:
    """What a message's calendar part proposes."""

    method: str
    uid: str
    summary: str
    organizer: str | None
    attendees: tuple[str, ...]
    starts_at: datetime | None
    ends_at: datetime | None
    location: str
    sequence: int
    raw_ics: bytes

    @property
    def is_request(self) -> bool:
        """True for an invitation asking for an answer."""
        return self.method in ("REQUEST", "PUBLISH", "")

    @property
    def is_cancellation(self) -> bool:
        return self.method == "CANCEL"

    @property
    def is_reply(self) -> bool:
        """True for an attendee's answer to an invitation we sent."""
        return self.method == "REPLY"

    def headline(self) -> str:
        """One line naming what this is, for the reader pane."""
        if self.is_cancellation:
            return f"Cancelled: {self.summary}"
        if self.is_reply:
            return f"Reply: {self.summary}"
        return f"Invitation: {self.summary}"

    def describe(self) -> tuple[str, ...]:
        """Lines describing the proposal, for a reader pane or a dialog."""
        lines = [self.headline()]
        if self.starts_at is not None:
            when = self.starts_at.astimezone().strftime("%a %d %b %Y %H:%M")
            if self.ends_at is not None:
                when += f" – {self.ends_at.astimezone().strftime('%H:%M')}"
            lines.append(f"When: {when}")
        if self.location:
            lines.append(f"Where: {self.location}")
        if self.organizer:
            lines.append(f"Organizer: {self.organizer}")
        if self.attendees:
            lines.append(f"Attendees: {', '.join(self.attendees)}")
        return tuple(lines)


def extract_invitation(raw_bytes: bytes) -> Invitation | None:
    """Return the invitation carried by *raw_bytes*, or None for a plain message.

    Malformed calendar data is treated as "no invitation": a message
    whose calendar part cannot be parsed is still a message, and the
    reader must open it.
    """
    payload = find_calendar_payload(raw_bytes)
    if payload is None:
        return None
    return parse_invitation(payload)


def find_calendar_payload(raw_bytes: bytes) -> bytes | None:
    """Return the bytes of the message's first calendar part, if any."""
    try:
        message = message_from_bytes(raw_bytes, policy=default_policy)
    except Exception:  # noqa: BLE001 — a message that will not parse has no part
        return None
    for part in message.walk():
        if part.get_content_type().lower() not in CALENDAR_CONTENT_TYPES:
            continue
        content = part.get_payload(decode=True)
        if isinstance(content, bytes) and content.strip():
            return content
    return None


def parse_invitation(ics: bytes) -> Invitation | None:
    """Project raw iCalendar bytes into an :class:`Invitation`."""
    try:
        components = parse_vcalendar(ics)
        method = parse_method(ics) or ""
    except IcalParseError:
        return None
    master = _master(components)
    if master is None or not master.uid:
        return None
    uid = master.uid
    return Invitation(
        method=method.upper(),
        uid=uid,
        summary=(master.summary or "(no subject)").strip(),
        organizer=extract_organizer(ics, uid),
        attendees=extract_attendees(ics, uid),
        starts_at=master.dtstart,
        ends_at=master.dtend,
        location=(master.location or "").strip(),
        sequence=master.sequence or 0,
        raw_ics=ics,
    )


def _master(components: Sequence[ParsedComponent]) -> ParsedComponent | None:
    """The component an invitation is about: the master, not an override."""
    for component in components:
        if component.recurrence_id is None:
            return component
    return components[0] if components else None


def reply_ics(
    invitation: Invitation, *, attendee: str, partstat: str, now: datetime
) -> bytes:
    """Build the ``METHOD:REPLY`` answering *invitation* as *attendee*.

    RFC 5546 §3.2.3: a reply carries the organizer, the one attendee
    answering with their new ``PARTSTAT``, and the ``UID`` and
    ``SEQUENCE`` of what is being answered — that is what lets the
    organizer's client match the answer to the right invitation and
    ignore an answer to a superseded one.
    """
    if partstat not in _REPLY_VERB:
        raise ValueError(f"unknown participation status: {partstat!r}")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Pony Express//EN",
        "METHOD:REPLY",
        "BEGIN:VEVENT",
        f"UID:{invitation.uid}",
        f"DTSTAMP:{_ical_datetime(now)}",
        f"SEQUENCE:{invitation.sequence}",
    ]
    if invitation.organizer:
        lines.append(f"ORGANIZER:mailto:{invitation.organizer}")
    lines.append(
        f"ATTENDEE;PARTSTAT={partstat}:mailto:{_address_only(attendee)}",
    )
    if invitation.starts_at is not None:
        lines.append(f"DTSTART:{_ical_datetime(invitation.starts_at)}")
    if invitation.ends_at is not None:
        lines.append(f"DTEND:{_ical_datetime(invitation.ends_at)}")
    lines.append(f"SUMMARY:{_escape_text(invitation.summary)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def reply_subject(invitation: Invitation, partstat: str) -> str:
    """The ``Accepted: …`` subject line an organizer's client expects."""
    verb = _REPLY_VERB.get(partstat, partstat.title())
    return f"{verb}: {invitation.summary}"


def reply_body(invitation: Invitation, *, attendee: str, partstat: str) -> str:
    """Plain-text body for a reply, for clients that show no calendar part."""
    verb = _REPLY_VERB.get(partstat, partstat.title()).lower()
    return (
        f"{_address_only(attendee)} has {verb} the invitation "
        f"to {invitation.summary!r}.\n"
    )


def request_subject(summary: str) -> str:
    return f"Invitation: {summary}"


def request_body(
    summary: str,
    *,
    starts_at: datetime | None,
    ends_at: datetime | None,
    location: str = "",
    description: str = "",
) -> str:
    """Plain-text body describing the event an invitation proposes."""
    lines = [f"You are invited to {summary!r}.", ""]
    if starts_at is not None:
        when = starts_at.astimezone().strftime("%a %d %b %Y %H:%M")
        if ends_at is not None:
            when += f" – {ends_at.astimezone().strftime('%H:%M')}"
        lines.append(f"When: {when}")
    if location:
        lines.append(f"Where: {location}")
    if description:
        lines += ["", description]
    return "\n".join(lines) + "\n"


def build_itip_message(
    *,
    from_address: str,
    to_addresses: Sequence[str],
    subject: str,
    body_text: str,
    ics: bytes,
    method: str,
    filename: str = "invite.ics",
) -> EmailMessage:
    """Wrap *ics* in a message carrying the iTIP *method*.

    The calendar part is an alternative to the plain text rather than an
    attachment, which is what makes a receiving client offer its own
    accept/decline buttons instead of showing a file.  The same bytes
    are attached as well, for clients that only look for a file.
    """
    if not to_addresses:
        raise ValueError("an invitation needs at least one recipient")
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = ", ".join(to_addresses)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body_text)
    message.add_alternative(
        ics.decode("utf-8"),
        subtype="calendar",
        params={"method": method.upper(), "charset": "UTF-8", "component": "VEVENT"},
    )
    message.add_attachment(
        ics,
        maintype="application",
        subtype="ics",
        filename=filename,
    )
    return message


def _address_only(address: str) -> str:
    """The bare address out of ``Name <addr@example.com>``."""
    _name, bare = parseaddr(address)
    return bare or address.strip()


def _ical_datetime(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


__all__ = [
    "CALENDAR_CONTENT_TYPES",
    "PARTSTAT_ACCEPTED",
    "PARTSTAT_DECLINED",
    "PARTSTAT_TENTATIVE",
    "Invitation",
    "build_itip_message",
    "extract_invitation",
    "find_calendar_payload",
    "parse_invitation",
    "reply_body",
    "reply_ics",
    "reply_subject",
    "request_body",
    "request_subject",
]
