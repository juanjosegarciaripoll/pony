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

from chronos.outcome import Notice, NoticeLog, Outcome, failure, info, warn

from .accounts import resolve_smtp_password

if TYPE_CHECKING:
    from chronos.domain import CalendarRef

    from .calendar import CalendarRuntime
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
    from chronos.ingest import IngestError, ingest_ics_bytes

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
    try:
        password = resolve_smtp_password(account, credentials)
    except Exception as exc:  # noqa: BLE001 — any backend may fail
        _log.error("credential lookup failed for %r: %s", account.name, exc)
        return Outcome.refused(failure(f"Could not get password: {exc}"))
    if password is None:
        return Outcome.refused(
            failure(f"No password available for account {account.name!r}."),
        )

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
    if filed.failed or choice.partstat is None or invitation.is_cancellation:
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


__all__ = [
    "PARTSTAT_WORDS",
    "InvitationChoice",
    "file_invitation",
    "replying_account",
    "reply_to_invitation",
    "respond_to_invitation",
]
