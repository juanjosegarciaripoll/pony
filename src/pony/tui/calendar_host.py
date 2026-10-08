"""Where the mail application meets the calendar's screens.

:class:`~pony.tui.app.PonyApp` hosts the calendar's own ``MainScreen``
rather than launching a second program, so three things have to be
arranged here:

* the calendar's :class:`TuiServices` bundle, including the OAuth flow
  that has to run *inside* the app it interrupts;
* the one line each half shows about the other — the next event in the
  mail reader, unread mail in the agenda;
* reminders falling due, turned into announcements for the shared
  notification centre.

Everything in this module imports freely from both halves.  It is the
only place that does, which is what keeps the calendar a subsystem
rather than a dependency of the mail code.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from textual.app import App

from chronos.cli import CliContext, build_sync_runner, use_remote_browser_flow
from chronos.credentials import DefaultCredentialsProvider
from chronos.domain import OAuthCredential
from chronos.oauth import StoredTokens
from chronos.protocols import IndexRepository as CalendarIndexRepository
from chronos.tui.app import AttendeeCompleter, InvitationSender, TuiServices
from chronos.tui.bindings import BindingType
from chronos.tui.views import (
    CalendarSelection,
    all_calendar_refs,
    format_friendly_start,
    gather_occurrences,
)

from ..calendar import CalendarRuntime
from ..compose_utils import format_display_address
from ..domain import AnyAccount, AppConfig
from ..invitation import (
    build_itip_message,
    parse_invitation,
    request_body,
    request_subject,
)
from ..notifications import Notification, NotificationSource
from ..protocols import ContactRepository
from ..protocols import IndexRepository as MailIndexRepository

# How far ahead the mail reader's status line looks for an event.  A day
# is enough to answer "what is next" without turning the line into an
# agenda of its own.
NEXT_EVENT_HORIZON = timedelta(days=1)

# A reminder that fell due while the user was away still deserves to be
# shown, but not indefinitely; this matches the calendar's own window.
ALARM_LOOKBACK = timedelta(minutes=15)

# Set beside the next event and the unread count when the terminal can
# render them.  `use_utf8 = false` falls back to a word.
EVENT_MARK = "◷"
MAIL_MARK = "✉"


def build_calendar_services(
    runtime: CalendarRuntime,
    *,
    host: App[None],
    startup_ics_path: Path | None = None,
    now: Callable[[], datetime] | None = None,
    invitation_sender: InvitationSender | None = None,
    contacts: ContactRepository | None = None,
    host_bindings: Sequence[BindingType] = (),
) -> TuiServices:
    """Bundle what the calendar screens need, hosted inside *host*.

    The credentials provider is rebuilt here rather than taken from
    *runtime*: an account whose OAuth tokens are missing has to be
    authorized through a screen on the running application, and
    *runtime* is built before there is an application to push one onto.

    *invitation_sender* and *contacts* are what the calendar gains from
    running inside a mail client: it can post an invitation, and it can
    complete an attendee from the address book.  *host_bindings* are the
    host's own keys worth listing on the calendar's help screen.  All
    three are optional, and without them the calendar behaves as it does
    on its own.
    """
    credentials = DefaultCredentialsProvider(
        interactive_authorizer=_in_app_authorizer(host)
    )
    context = CliContext(
        config=runtime.config,
        mirror=runtime.mirror,
        index=runtime.index,
        creds=credentials,
        # `build_sync_runner` returns its results rather than printing
        # them, but `CliContext` requires both streams and a running TUI
        # owns the terminal, so they go nowhere.
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        now=datetime.now(UTC),
    )
    return TuiServices(
        config=runtime.config,
        mirror=runtime.mirror,
        index=runtime.index,
        creds=credentials,
        now=now if now is not None else (lambda: datetime.now(UTC)),
        sync_runner=build_sync_runner(context),
        startup_ics_path=startup_ics_path,
        invitation_sender=invitation_sender,
        attendee_completer=(
            contact_completer(contacts) if contacts is not None else None
        ),
        contacts_browser=(
            contact_browser_opener(host, contacts) if contacts is not None else None
        ),
        host_bindings=tuple(host_bindings),
    )


def _in_app_authorizer(
    host: App[None],
) -> Callable[[str, OAuthCredential, Path], StoredTokens]:
    """An OAuth authorizer that runs as a screen on *host*.

    Sync runs on a worker thread, so the screen is pushed through
    `call_from_thread` and the worker blocks on an event until the user
    finishes or the flow fails.
    """

    def authorize(
        account_name: str, spec: OAuthCredential, _token_path: Path
    ) -> StoredTokens:
        from chronos.tui.screens.oauth_progress_screen import OAuthProgressScreen

        outcome: list[StoredTokens | BaseException] = []
        done = threading.Event()

        def on_complete(result: StoredTokens | BaseException) -> None:
            outcome.append(result)
            done.set()

        screen = OAuthProgressScreen(
            account_name,
            spec,
            on_complete=on_complete,
            remote_browser=use_remote_browser_flow(),
        )
        host.call_from_thread(host.push_screen, screen)
        done.wait()
        result = outcome[0]
        if isinstance(result, BaseException):
            raise result
        return result

    return authorize


# ---------------------------------------------------------------------------
# What each half says about the other
# ---------------------------------------------------------------------------


def next_event_status(
    services: TuiServices, *, now: datetime, use_utf8: bool = False
) -> str:
    """One line naming the event in progress, or the next one due.

    Empty when nothing falls inside :data:`NEXT_EVENT_HORIZON`, so the
    mail reader shows no calendar status rather than "nothing on".  An
    event already under way wins over a later one, which is what makes
    the line answer "where should I be now?".
    """
    rows = gather_occurrences(
        index=services.index,
        calendars=all_calendar_refs(services.config, services.mirror),
        # An empty selection means every calendar on every account.
        selection=CalendarSelection(refs=frozenset()),
        window=(now - NEXT_EVENT_HORIZON, now + NEXT_EVENT_HORIZON),
    )
    upcoming = [
        row for row in rows if row.occurrence.end is None or row.occurrence.end > now
    ]
    if not upcoming:
        return ""
    row = upcoming[0]
    start = row.occurrence.start.astimezone()
    summary = (row.component.summary or "(no summary)").strip()
    when = (
        start.strftime("%H:%M")
        if start.date() == now.astimezone().date()
        else format_friendly_start(row.occurrence.start, now.astimezone().date())
    )
    mark = EVENT_MARK if use_utf8 else "Next:"
    return f"{mark} {when} {summary}"


def mail_status(
    index: MailIndexRepository, config: AppConfig, *, use_utf8: bool = False
) -> str:
    """One line counting unread mail, for the agenda's title row.

    Empty when nothing is unread: the agenda should not carry a "0
    unread" badge all day.
    """
    unread = 0
    for account in config.accounts:
        unread += sum(index.unread_counts_by_folder(account_name=account.name).values())
    if unread == 0:
        return ""
    mark = MAIL_MARK if use_utf8 else "Mail:"
    return f"{mark} {unread} unread"


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AlarmPoller:
    """Turns due reminders into announcements, exactly once each.

    The calendar records a fired alarm in its own index, so a reminder
    survives a restart without being shown twice.  Marking happens as
    each alarm is converted, not in a second pass, so a failure midway
    cannot replay the ones already announced.
    """

    index: CalendarIndexRepository
    lookback: timedelta = ALARM_LOOKBACK

    def due(self, now: datetime) -> tuple[Notification, ...]:
        """Announcements for every alarm that fell due since the last call."""
        from chronos.tui.app import alarm_message

        pending = self.index.query_pending_alarms(now - self.lookback, now)
        notifications: list[Notification] = []
        for alarm in pending:
            if alarm.db_id is None:
                continue
            notifications.append(
                Notification(
                    source=NotificationSource.CALENDAR,
                    title=alarm.summary or "Reminder",
                    body=alarm_message(alarm, now),
                    at=now,
                    urgent=True,
                )
            )
            self.index.mark_alarm_fired(alarm.db_id, now)
        return tuple(notifications)


def mail_arrival_notification(
    fetched_by_account: Sequence[tuple[str, int]], *, now: datetime
) -> Notification | None:
    """An announcement for mail a sync just fetched, or None for none.

    The mail reader reports its own sync results on its own screen, so
    this exists for the case the agenda is the screen in front of the
    user.
    """
    arrived = [(name, count) for name, count in fetched_by_account if count > 0]
    if not arrived:
        return None
    total = sum(count for _name, count in arrived)
    plural = "" if total == 1 else "s"
    body = ", ".join(f"{name}: {count}" for name, count in arrived)
    return Notification(
        source=NotificationSource.MAIL,
        title=f"{total} new message{plural}",
        body=body,
        at=now,
    )


__all__ = [
    "ALARM_LOOKBACK",
    "InvitationDelivery",
    "EVENT_MARK",
    "MAIL_MARK",
    "NEXT_EVENT_HORIZON",
    "AlarmPoller",
    "build_calendar_services",
    "contact_completer",
    "mail_arrival_notification",
    "mail_status",
    "next_event_status",
    "send_event_invitations",
]


# ---------------------------------------------------------------------------
# Invitations out of the calendar
# ---------------------------------------------------------------------------


def contact_browser_opener(
    host: App[None], contacts: ContactRepository
) -> Callable[[], None]:
    """Open Pony's contact browser from the calendar's `B` key.

    The browser is a mail-side screen, and the calendar may not import
    one, so the calendar is handed this closure instead and only binds
    the key. Pressing it in either half therefore reaches the same list
    of people, which is the point of the two halves sharing a process.
    """

    def open_browser() -> None:
        from .screens.contact_browser_screen import ContactBrowserScreen

        host.push_screen(ContactBrowserScreen(contacts))  # pyright: ignore[reportUnknownMemberType]

    return open_browser


def contact_completer(contacts: ContactRepository) -> AttendeeCompleter:
    """An attendee completer answering from Pony's contact index.

    The same source the composer's address fields complete from, which
    is the point of mail and calendar sharing a process: an invitation
    goes to the people already in the user's mail.
    """

    def complete(prefix: str, *, limit: int = 10) -> Sequence[str]:
        if len(prefix.strip()) < 2:
            return ()
        addresses: list[str] = []
        for contact in contacts.search_contacts(prefix=prefix, limit=limit):
            for email in contact.emails:
                addresses.append(format_display_address(contact.display_name, email))
                if len(addresses) >= limit:
                    return tuple(addresses)
        return tuple(addresses)

    return complete


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
    from ..smtp_sender import DEFAULT_CONNECT_ATTEMPTS, SMTPError
    from ..smtp_sender import send_message as smtp_send

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
