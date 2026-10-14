"""Answering a meeting invitation, with no user interface attached.

Answering is two operations that the user experiences as one decision:
the event is filed in a calendar, and the organizer is told what the
answer was.  They are reported separately because the states differ in
ways that matter — an invitation that reached the calendar but whose
reply bounced is not the same as one that never landed, and saying
"accepted" for both would hide it.

Which calendar, and whether to answer at all, is the caller's question
to ask.  :class:`InvitationChoice` is the answer, whether it came from a
dialog or a form.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pony.calendar.outcome import Notice, NoticeLog, Outcome, failure, info, warn

from .delivery import resolve_send_password
from .invitation import (
    build_itip_message,
    parse_invitation,
    request_body,
    request_subject,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pony.calendar.domain import CalendarRef

    from .calendar_runtime import CalendarRuntime
    from .domain import AnyAccount, AppConfig
    from .invitation import Invitation
    from .protocols import CredentialsProvider

_log = logging.getLogger(__name__)

# What a PARTSTAT means to someone reading a notice.
PARTSTAT_WORDS = {
    "ACCEPTED": "Accepted",
    "DECLINED": "Declined",
    "TENTATIVE": "Tentatively accepted",
}


@dataclass(frozen=True, slots=True)
class InvitationChoice:
    """What the user decided about an invitation.

    ``partstat`` is None for "file it but tell nobody", which is the
    honest option when the message announces an event rather than asking
    about one, or when the user does not want to answer yet.
    """

    calendar: CalendarRef
    partstat: str | None


def file_invitation(
    invitation: Invitation,
    *,
    calendar: CalendarRuntime,
    target: CalendarRef,
) -> Outcome:
    """Put *invitation*'s event in *target*, or say why it did not go.

    A re-sent invitation for an event already filed is a correction, not
    a duplicate to skip, so an existing component is replaced.
    """
    from pony.calendar.ingest import IngestError, ingest_ics_bytes

    try:
        report = ingest_ics_bytes(
            invitation.raw_ics,
            target=target,
            mirror=calendar.mirror,
            index=calendar.index,
            on_conflict="replace",
        )
    except IngestError as exc:
        return Outcome.refused(failure(f"Could not file the invitation: {exc}"))
    if invitation.is_cancellation:
        return Outcome(
            changed=1 if report.cancelled else 0,
            notices=(
                info(f"Cancelled in {target.calendar_name}.")
                if report.cancelled
                else info("Nothing to cancel — the event is not in the calendar."),
            ),
        )
    return Outcome(changed=1, notices=(info(f"Filed in {target.calendar_name}."),))


def reply_to_invitation(
    invitation: Invitation,
    *,
    partstat: str,
    account: AnyAccount,
    config: AppConfig,
    credentials: CredentialsProvider | None,
    now: datetime | None = None,
) -> Outcome:
    """Mail the organizer a ``METHOD:REPLY`` carrying *partstat*.

    Blocks for as long as SMTP does, so both front ends run it off their
    own loop.  The reply goes from *account* — the one the invitation
    arrived on, picked by :func:`replying_account` — because that is the
    address the organizer's client wrote on the attendee line and the
    one it will match the answer to.
    """
    from .invitation import build_itip_message, reply_body, reply_ics, reply_subject
    from .smtp_sender import DEFAULT_CONNECT_ATTEMPTS, SMTPError
    from .smtp_sender import send_message as smtp_send

    organizer = invitation.organizer
    if not organizer:
        return Outcome.refused(warn("No organizer to reply to."))
    if account.smtp is None or account.username is None:
        return Outcome.refused(
            warn(f"Account {account.name!r} has no SMTP server configured."),
        )
    # `delivery.resolve_send_password` is the one answer to "what does
    # this account send with": it logs a backend failure and words both
    # refusals, which this used to do again in its own words through a
    # second function with the arguments the other way round.
    password = resolve_send_password(credentials, account)
    if isinstance(password, Notice):
        return Outcome.refused(password)

    sender = account.email_address or account.username
    message = build_itip_message(
        from_address=sender,
        to_addresses=[organizer],
        subject=reply_subject(invitation, partstat),
        body_text=reply_body(invitation, attendee=sender, partstat=partstat),
        ics=reply_ics(
            invitation,
            attendee=sender,
            partstat=partstat,
            now=now or datetime.now(UTC),
        ),
        method="REPLY",
        filename="reply.ics",
    )
    try:
        smtp_send(
            smtp=account.smtp,
            username=account.username,
            password=password,
            msg=message,
            connect_timeout=config.smtp_connect_timeout_seconds,
            connect_attempts=DEFAULT_CONNECT_ATTEMPTS,
        )
    except (SMTPError, ValueError) as exc:
        _log.error("invitation reply to %s failed: %s", organizer, exc)
        return Outcome.refused(
            failure(f"Filed, but the reply could not be sent: {exc}"),
        )
    word = PARTSTAT_WORDS.get(partstat, partstat.title())
    return Outcome(changed=1, notices=(info(f"{word}; the organizer was told."),))


def respond_to_invitation(
    invitation: Invitation,
    choice: InvitationChoice,
    *,
    calendar: CalendarRuntime,
    account: AnyAccount | None,
    config: AppConfig,
    credentials: CredentialsProvider | None,
    now: datetime | None = None,
) -> Outcome:
    """File *invitation* as *choice* says, then answer the organizer.

    Filing comes first and is reported on its own: there is no point
    telling an organizer an event was accepted into a calendar that
    refused it.
    """
    log = NoticeLog()
    filed = file_invitation(invitation, calendar=calendar, target=choice.calendar)
    log.notices.extend(filed.notices)
    if filed.failed or choice.partstat is None or not invitation.can_reply:
        return Outcome(changed=filed.changed, notices=tuple(log.notices))
    if account is None:
        log.warn("No account configured for sending — nothing was answered.")
        return Outcome(changed=filed.changed, notices=tuple(log.notices))
    replied = reply_to_invitation(
        invitation,
        partstat=choice.partstat,
        account=account,
        config=config,
        credentials=credentials,
        now=now,
    )
    log.notices.extend(replied.notices)
    return Outcome(
        changed=filed.changed + replied.changed,
        notices=tuple(log.notices),
    )


def replying_account(
    config: AppConfig,
    *,
    arrived_on: str | None,
) -> AnyAccount | Notice:
    """The identity to answer an invitation from, or why there is none.

    The account the invitation arrived on, when it can send; the first
    that can otherwise.
    """
    candidates = [account for account in config.accounts if account.can_send]
    if not candidates:
        return warn("No account configured for sending.")
    if arrived_on is not None:
        for account in candidates:
            if account.name == arrived_on:
                return account
    return candidates[0]


# ----------------------------------------------------------------------
# The outbound half: inviting people to an event of our own
# ----------------------------------------------------------------------
#
# This lived in `pony.tui.calendar_host`, a module that imports
# `textual.app.App`, although none of it touches a screen.  It was
# therefore unreachable from `pony.web` -- the browser must not import
# anything under a `tui` package, and `tests/test_layering.py` enforces
# that -- so saving an event with attendees from the browser invited
# nobody, silently, against what `docs/architecture.md` says happens.
#
# It belongs here, beside the inbound half: answering an invitation and
# sending one are the same iTIP conversation seen from the two ends, and
# both need an account, a password and an SMTP socket and nothing else.


def sender_for_invitation(
    config: AppConfig,
    credentials: CredentialsProvider | None,
) -> tuple[AnyAccount, str] | Notice:
    """Who posts an invitation and with what, or why nobody can.

    Both front ends post the same iTIP REQUEST after saving an event,
    and each asked these two questions and worded both refusals itself
    -- "no account is configured for sending invitations" in one,
    "no invitation was sent: No account configured for sending" in the
    other.

    Every refusal is about the invitation and never about the save: the
    event is already on the server by the time any of this runs, and a
    sentence that sounded like a failed save would invite the user to
    write it twice.
    """
    account = replying_account(config, arrived_on=None)
    if isinstance(account, Notice):
        return unsent_invitation(account)
    password = resolve_send_password(credentials, account)
    if isinstance(password, Notice):
        return unsent_invitation(password)
    return account, password


def unsent_invitation(reason: Notice) -> Notice:
    """*reason*, said as what it is: a saved event with no invitation out."""
    return Notice(
        f"Event saved, but no invitation was sent: {reason.text}",
        reason.severity,
    )


@dataclass(frozen=True, slots=True)
class InvitationDelivery:
    """What happened when an invitation was posted."""

    sent_to: tuple[str, ...]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def send_event_invitations(
    *,
    ics: bytes,
    attendees: Sequence[str],
    organizer: str | None,
    summary: str,
    is_update: bool,
    account: AnyAccount,
    password: str,
    connect_timeout: int,
) -> InvitationDelivery:
    """Mail *ics* to *attendees* as a ``METHOD:REQUEST``.

    The organizer is dropped from the recipients: they are the one
    sending, and clients treat an invitation addressed to its own
    organizer as a duplicate of the event they just created.
    """
    from .smtp_sender import DEFAULT_CONNECT_ATTEMPTS, SMTPError
    from .smtp_sender import send_message as smtp_send

    sender = account.email_address or account.username or ""
    organizer_address = (organizer or sender).strip().lower()
    recipients = tuple(
        address for address in attendees if address.strip().lower() != organizer_address
    )
    if not recipients:
        return InvitationDelivery(sent_to=())

    invitation = parse_invitation(ics)
    subject = request_subject(summary)
    if is_update:
        subject = f"Updated invitation: {summary}"
    body = request_body(
        summary,
        starts_at=invitation.starts_at if invitation is not None else None,
        ends_at=invitation.ends_at if invitation is not None else None,
        location=invitation.location if invitation is not None else "",
    )
    message = build_itip_message(
        from_address=sender,
        to_addresses=recipients,
        subject=subject,
        body_text=body,
        ics=_with_method(ics, "REQUEST"),
        method="REQUEST",
    )
    assert account.smtp is not None
    assert account.username is not None
    try:
        smtp_send(
            smtp=account.smtp,
            username=account.username,
            password=password,
            msg=message,
            connect_timeout=connect_timeout,
            connect_attempts=DEFAULT_CONNECT_ATTEMPTS,
        )
    except (SMTPError, ValueError) as exc:
        return InvitationDelivery(sent_to=recipients, error=str(exc))
    return InvitationDelivery(sent_to=recipients)


def _with_method(ics: bytes, method: str) -> bytes:
    """Return *ics* carrying ``METHOD:<method>``, adding it if absent.

    The calendar writes its events without a METHOD — they are entries,
    not scheduling messages.  Posting one as an invitation is exactly
    what makes it a scheduling message, so the property is added on the
    way out rather than stored.

    It goes before the first subcomponent, not before the first VEVENT:
    RFC 5545 puts the calendar's own properties ahead of its components,
    and an event with a VTIMEZONE would otherwise take the METHOD after
    that timezone block.
    """
    text = ics.decode("utf-8", errors="replace")
    if "\nMETHOD:" in text or text.startswith("METHOD:"):
        return ics
    opening = text.find("BEGIN:VCALENDAR")
    if opening < 0:
        return ics
    position = text.find("BEGIN:", opening + len("BEGIN:VCALENDAR"))
    if position < 0:
        return ics
    return (text[:position] + f"METHOD:{method}\r\n" + text[position:]).encode("utf-8")


__all__ = [
    "PARTSTAT_WORDS",
    "InvitationChoice",
    "InvitationDelivery",
    "file_invitation",
    "replying_account",
    "reply_to_invitation",
    "respond_to_invitation",
    "send_event_invitations",
    "sender_for_invitation",
    "unsent_invitation",
]
