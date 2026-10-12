"""What saving, rescheduling, trashing and importing an event do.

Every one of these is the same sequence — decide the iCalendar text,
write it to the mirror, upsert the index row, rebuild the occurrence and
alarm caches, say what happened — and each had been written out again
inside :class:`~chronos.tui.screens.main_screen.MainScreen`.  Saving had
two near-identical copies of it, one for a new event and one for an edit,
which is how the organizer of an edited event came to be worked out in
a different place from the organizer of a new one.

They live here instead, over the stores rather than over widgets, and
report through :class:`chronos.outcome.Outcome` instead of calling
``app.notify``.  A second front end offers the same operations without
reimplementing them, and a test reaches the recurrence-cache rebuild
without starting a Textual app.

Posting an invitation to the attendees is deliberately *not* here: it
needs a mail sender, which only a host supplies.  :func:`save_event`
returns the iCalendar text and the organizer it settled on, and the
caller does the posting.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from chronos.domain import (
    AlarmAction,
    AppConfig,
    CalendarRef,
    ComponentRef,
    LocalStatus,
    ParsedAlarm,
    ResourceRef,
    StoredComponent,
    VEvent,
)
from chronos.ical_parser import extract_organizer
from chronos.mutations import (
    build_event_ics,
    edited_flags,
    generate_uid,
    reschedule_event_ics,
    trashed_copy,
)
from chronos.outcome import Notice, Outcome, failure, info
from chronos.recurrence import rebuild_caches

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from chronos.ingest import OnConflict
    from chronos.protocols import IndexRepository, MirrorRepository


@dataclass(frozen=True, slots=True)
class Calendars:
    """The stores every event operation works on."""

    config: AppConfig
    mirror: MirrorRepository
    index: IndexRepository


@dataclass(frozen=True, kw_only=True, slots=True)
class EditDraft:
    """What a user asked an event to become.

    Produced by whatever collected the edits — the TUI's event form, a
    web form, a script — and consumed by :func:`save_event`.  It names
    the calendar and carries the fields; it does no I/O and knows
    nothing about how it was filled in.
    """

    target: CalendarRef
    summary: str
    dtstart: datetime
    dtend: datetime | None
    location: str
    description: str
    attendees: tuple[str, ...]
    alarms: tuple[ParsedAlarm, ...]
    existing: StoredComponent | None
    # When set, `dtstart`/`dtend` are the UTC-midnight bounds from
    # `all_day_bounds` (end exclusive) and the event is saved as
    # `VALUE=DATE`.
    all_day: bool = False
    # A caller that already knows the UID it wants, which only
    # `chronos calendar add --uid` does: a form has no business naming
    # one.  Ignored for an edit, which keeps the UID it has.
    uid: str | None = None


@dataclass(frozen=True, slots=True)
class SavedEvent:
    """A stored event, and what a host needs to announce it.

    *ics* and *organizer* are what posting a ``METHOD:REQUEST`` to the
    attendees requires, and *created* is what decides whether that post
    is an invitation or an update.
    """

    component: StoredComponent
    ics: bytes
    organizer: str | None
    created: bool
    outcome: Outcome


def reminders_text(alarms: Sequence[ParsedAlarm]) -> str:
    """*alarms* as the comma-separated minutes a form shows.

    Only START-relative alarms with a negative offset are representable
    that way; anything else — an absolute trigger, one relative to the
    end — is left out of the field and carried through untouched by
    whoever is editing.
    """
    parts: list[str] = []
    for alarm in alarms:
        if not isinstance(alarm.trigger_offset, timedelta):
            continue
        if alarm.trigger_related != "START":
            continue
        seconds = alarm.trigger_offset.total_seconds()
        if seconds >= 0:
            continue
        minutes = abs(int(seconds)) // 60
        if minutes > 0:
            parts.append(str(minutes))
    return ", ".join(parts)


def parse_reminders(raw: str) -> tuple[ParsedAlarm, ...]:
    """A comma-separated list of minutes-before-start, as alarms.

    Each token must be a positive integer; anything else is ignored
    rather than refused, because a reminder field is not where a user
    should be stopped.
    """
    alarms: list[ParsedAlarm] = []
    for token in raw.split(","):
        stripped = token.strip()
        if not stripped:
            continue
        try:
            minutes = int(stripped)
        except ValueError:
            continue
        if minutes <= 0:
            continue
        alarms.append(
            ParsedAlarm(
                action=AlarmAction.DISPLAY,
                trigger_offset=timedelta(minutes=-minutes),
                trigger_related="START",
                description=None,
            ),
        )
    return tuple(alarms)


def find_component(
    index: IndexRepository,
    uid: str,
    *,
    within: CalendarRef | None = None,
) -> StoredComponent | None:
    """The component *uid* names, in *within* when one is given.

    A UID identifies a component within a calendar, not across a
    server: the same invitation mirrored into two calendars is two
    components with one UID.  Resolving by UID alone therefore picks an
    arbitrary one, which is how an edit opened from one calendar could
    be saved against another.  With *within*, only that calendar's copy
    matches.
    """
    found = list(index.list_components_by_uid(uid))
    if within is not None:
        for component in found:
            if component.ref.calendar == within:
                return component
        return None
    return found[0] if found else None


def default_event_start(day: date, now: datetime) -> datetime:
    """Where a new event starts when nothing else decides it.

    On today, the next local half hour: an event being created now is
    usually about to happen.  On any other day, the usual start of a
    working day.  One rule, so a new event does not begin at a
    different time depending on which front end opened the form.
    """
    local_now = now.astimezone()
    if day != local_now.date():
        return datetime.combine(day, time(9, 0)).astimezone()
    rounded = local_now.replace(second=0, microsecond=0)
    remainder = rounded.minute % 30
    if remainder:
        return rounded + timedelta(minutes=30 - remainder)
    if local_now.second or local_now.microsecond:
        return rounded + timedelta(minutes=30)
    return rounded


def parse_attendees(raw: str) -> tuple[str, ...]:
    """A comma-separated attendee field, as bare email addresses.

    Both front ends collect attendees as one line of text, and both
    need the same reading of it: ``Name <addr>`` and ``mailto:`` are
    unwrapped, duplicates collapse, and an address that cannot be
    serialised into an ``ATTENDEE`` property raises ``ValueError`` with
    the offending value named.  A caller reports that its own way --
    inline in the terminal, as a refusal notice in the browser.
    """
    from chronos.mutations import normalize_attendee_emails

    values = tuple(token.strip() for token in raw.split(",") if token.strip())
    if not values:
        return ()
    return normalize_attendee_emails(values)


def all_day_form_bounds(first: date, last: date | None) -> tuple[datetime, datetime]:
    """Stored bounds for the dates an all-day form holds.

    *last* is the **inclusive** last day as the user typed it, or None
    for a single-day event; the pair that comes back is what
    :class:`EditDraft` requires for ``all_day`` -- UTC midnight, end
    exclusive.  Both front ends read their form through this, because
    handing ``save_event`` the raw local values instead stores the
    previous day east of UTC.
    """
    from chronos.mutations import all_day_bounds

    return all_day_bounds(first, max(first, last) if last is not None else first)


def all_day_form_dates(
    dtstart: datetime | None,
    dtend: datetime | None,
) -> tuple[str, str]:
    """A stored all-day span as the ``YYYY-MM-DD`` pair a form shows.

    The inverse of :func:`all_day_form_bounds`: the second value is the
    *inclusive* last day, so re-opening an event and saving it
    unchanged leaves it where it was.
    """
    if dtstart is None:
        return "", ""
    first = dtstart.astimezone(UTC).date()
    if dtend is None:
        return first.isoformat(), ""
    last = dtend.astimezone(UTC).date() - timedelta(days=1)
    return first.isoformat(), max(first, last).isoformat()


def existing_alarms(component: StoredComponent | None) -> tuple[ParsedAlarm, ...]:
    """The alarms *component* already carries, or none."""
    if component is None:
        return ()
    from chronos.ical_parser import extract_alarm_triggers

    return tuple(extract_alarm_triggers(component.raw_ics, component.ref.uid))


def save_event(
    calendars: Calendars,
    draft: EditDraft,
    *,
    now: datetime,
) -> SavedEvent:
    """Store *draft*, as a new event or as an edit of an existing one.

    The two cases differ only in what they carry over: a new event
    invents a UID and starts with no server identity, while an edit
    keeps the href, etag, status and sync state it already had and
    records that it was edited locally.  Everything after that — the
    iCalendar text, the mirror write, the index row and the cache
    rebuild — is one path, so the two cannot drift apart.
    """
    existing = draft.existing
    if existing is None:
        uid = draft.uid or generate_uid(
            draft.target.account_name,
            draft.target.calendar_name,
            draft.summary,
            draft.dtstart,
            now,
        )
        ref = ComponentRef(draft.target.account_name, draft.target.calendar_name, uid)
        resource = ResourceRef(
            draft.target.account_name,
            draft.target.calendar_name,
            uid,
        )
        organizer = organizer_for(draft.target, calendars.config)
    else:
        ref = existing.ref
        resource = existing.ref.resource
        # An edit keeps the organizer the event already names; only one
        # that names nobody falls back to the account.
        organizer = extract_organizer(existing.raw_ics, existing.ref.uid)
        if organizer is None:
            organizer = organizer_for(existing.ref.calendar, calendars.config)

    ics = build_event_ics(
        ref.uid,
        draft.summary,
        draft.dtstart,
        draft.dtend,
        now,
        location=draft.location,
        description=draft.description,
        attendees=draft.attendees,
        organizer=organizer,
        alarms=draft.alarms,
        all_day=draft.all_day,
    )
    calendars.mirror.write(resource, ics)
    component = VEvent(
        ref=ref,
        href=existing.href if existing is not None else None,
        etag=existing.etag if existing is not None else None,
        raw_ics=ics,
        summary=draft.summary,
        description=draft.description or None,
        location=draft.location or None,
        dtstart=draft.dtstart,
        dtend=draft.dtend,
        status=existing.status if existing is not None else None,
        local_flags=edited_flags(existing) if existing is not None else frozenset(),
        server_flags=existing.server_flags if existing is not None else frozenset(),
        local_status=existing.local_status
        if existing is not None
        else LocalStatus.ACTIVE,
        trashed_at=existing.trashed_at if existing is not None else None,
        synced_at=existing.synced_at if existing is not None else None,
    )
    _store(calendars, component, now=now)
    verb = "Updated" if existing is not None else "Created"
    return SavedEvent(
        component=component,
        ics=ics,
        organizer=organizer,
        created=existing is None,
        outcome=Outcome(changed=1, notices=(info(f"{verb} {draft.summary!r}"),)),
    )


def reschedule_event(
    calendars: Calendars,
    component: StoredComponent,
    delta: timedelta,
    *,
    now: datetime,
) -> Outcome:
    """Shift *component* by *delta*, keeping everything else as it was.

    This is what dragging an event on the timeline asks for.  It rewrites
    the DTSTART/DTEND of the stored iCalendar text rather than rebuilding
    it from fields, so properties the editor never shows survive the
    move.
    """
    if not isinstance(component, VEvent) or component.dtstart is None:
        return Outcome()
    dtstart = component.dtstart + delta
    dtend = component.dtend + delta if component.dtend is not None else None
    try:
        raw_ics = reschedule_event_ics(
            component.raw_ics,
            component.ref.uid,
            dtstart,
            dtend,
            now,
        )
    except ValueError as exc:
        return Outcome.refused(Notice(str(exc)))
    updated = replace(
        component,
        raw_ics=raw_ics,
        dtstart=dtstart,
        dtend=dtend,
        local_flags=edited_flags(component),
    )
    calendars.mirror.write(component.ref.resource, raw_ics)
    _store(calendars, updated, now=now)
    return Outcome(
        changed=1,
        notices=(info(f"Moved {component.summary or component.ref.uid!r}"),),
    )


def trash_event(
    calendars: Calendars,
    component: StoredComponent,
    *,
    now: datetime,
) -> Outcome:
    """Flip *component* to ``TRASHED``.

    The next sync's ``_push_trashed`` issues the server DELETE and purges
    the local row, so a delete made here is picked up by ``chronos sync``
    without anything else being recorded.
    """
    calendars.index.upsert_component(trashed_copy(component, trashed_at=now))
    return Outcome(
        changed=1,
        notices=(info(f"Trashed {component.summary or component.ref.uid!r}"),),
    )


def import_ics_bytes(
    calendars: Calendars,
    target: CalendarRef,
    payload: bytes,
    *,
    on_conflict: OnConflict = "skip",
) -> Outcome:
    """File everything in *payload* into *target*.

    ``skip`` by default, as ``chronos import`` and the MCP tool default:
    an import that silently overwrote the local copy of an event would
    mean something different depending on which front end ran it.
    """
    from chronos.ingest import ingest_ics_bytes as ingest

    try:
        report = ingest(
            payload,
            target=target,
            mirror=calendars.mirror,
            index=calendars.index,
            on_conflict=on_conflict,
        )
    except Exception as exc:  # noqa: BLE001 — a bad file is the user's news
        return Outcome.refused(failure(f"Import failed: {exc}"))
    return Outcome(
        changed=report.imported + report.updated + report.cancelled,
        notices=(
            info(
                f"Imported {report.imported}, updated {report.updated}, "
                f"cancelled {report.cancelled} (skipped {report.skipped})",
            ),
        ),
    )


def import_ics_file(
    calendars: Calendars,
    target: CalendarRef,
    path: Path,
) -> Outcome:
    """Read *path* and file everything in it into *target*."""
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return Outcome.refused(failure(f"Import failed: {exc}"))
    return import_ics_bytes(calendars, target, payload)


def organizer_for(calendar: CalendarRef, config: AppConfig) -> str | None:
    """The address to put in ORGANIZER for events on *calendar*.

    The account's username when it is an address, and nothing otherwise:
    an ORGANIZER that is not a mail address is worse than none, since a
    reply would have nowhere to go.
    """
    for account in config.accounts:
        if account.name != calendar.account_name:
            continue
        username = account.username.strip()
        return username if "@" in username else None
    return None


def _store(
    calendars: Calendars,
    component: StoredComponent,
    *,
    now: datetime,
) -> None:
    """Upsert *component* and rebuild the caches the views read.

    ``upsert_component`` invalidates the occurrence rows for the master
    so a stale cache cannot outlive a content change.  Sync repopulates
    them, but a local create or edit has no such backstop — without this
    the saved event vanishes from every view that joins ``components``
    against ``occurrences`` (agenda, day, grid) until the next sync.
    Rebuilding through :func:`chronos.recurrence.rebuild_caches` is what
    keeps the window and override handling the same as sync's and
    ingest's.
    """
    calendars.index.upsert_component(component)
    rebuild_caches(
        index=calendars.index,
        calendar=component.ref.calendar,
        now=now,
        uids=frozenset({component.ref.uid}),
    )


__all__ = [
    "Calendars",
    "EditDraft",
    "SavedEvent",
    "existing_alarms",
    "import_ics_file",
    "organizer_for",
    "parse_reminders",
    "reminders_text",
    "reschedule_event",
    "save_event",
    "trash_event",
]
