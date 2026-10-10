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
from typing import TYPE_CHECKING

from chronos.domain import (
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
    from datetime import datetime, timedelta
    from pathlib import Path

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
        uid = generate_uid(
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


def import_ics_file(
    calendars: Calendars,
    target: CalendarRef,
    path: Path,
) -> Outcome:
    """Read *path* and file everything in it into *target*."""
    from chronos.ingest import ingest_ics_bytes

    try:
        payload = path.read_bytes()
        report = ingest_ics_bytes(
            payload,
            target=target,
            mirror=calendars.mirror,
            index=calendars.index,
            on_conflict="skip",
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
    "import_ics_file",
    "organizer_for",
    "reschedule_event",
    "save_event",
    "trash_event",
]
