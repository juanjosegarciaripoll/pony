"""What the calendar's views are *of*, with no renderer attached.

Window arithmetic, the index queries each view needs, and the strings
those rows are made of all live here.  A screen is then a thin shell:
it asks for the rows and hands them to a widget.

Nothing in this module knows how a row will be drawn.  Where a row needs
emphasis — an event in progress, one that has already ended — it says so
as :class:`RowEmphasis` and leaves the terminal's bold and dim, or a
browser's CSS classes, to whatever is rendering.  That is what lets the
same projections serve a second front end, and it is why this module
sits beside the stores rather than under ``tui/``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

from chronos.domain import (
    AppConfig,
    CalendarRef,
    ComponentRef,
    LocalStatus,
    Occurrence,
    ParsedAlarm,
    StoredComponent,
    VEvent,
    VTodo,
)
from chronos.ical_parser import extract_alarm_triggers, extract_attendees
from chronos.mutations import is_all_day_span
from chronos.protocols import IndexRepository, MirrorRepository

DEFAULT_AGENDA_DAYS = 14

# Marks the time of an event in progress, in the agenda and on the
# timeline's current-slot label.
IN_PROGRESS_MARK = "▸"


class ViewKind(StrEnum):
    """Top-level view modes. `a` selects AGENDA; the `1`–`7` number
    keys select the timeline (`1` → DAY, `2`–`7` → GRID of that width).

    Older versions had separate Day/Week/Month/Todos views. Those
    collapsed into:

    - `AGENDA` — flat list, window controlled by `AgendaWindow`
    - `DAY`    — single-day timeline grid (the `1` key)
    - `GRID`   — multi-day timeline grid, 2–7 days wide (the `2`–`7`
                 keys)
    - `MONTH`  — calendar-style month grid (the `M` key)

    VTodos are rendered inline as full-day items in every view; there
    is no longer a dedicated Todos screen.
    """

    AGENDA = "agenda"
    DAY = "day"
    GRID = "grid"
    MONTH = "month"


class AgendaWindow(StrEnum):
    """Sub-mode of `ViewKind.AGENDA` controlled by `d` / `w` / `m`.

    Maps to the same windowing helpers the standalone Day/Week/Month
    views used to drive: `day_window`, `week_window`, `month_window`.
    """

    DAY = "day"
    WEEK = "week"
    MONTH = "month"


@dataclass(frozen=True, kw_only=True)
class CalendarSelection:
    """Set of (account, calendar) pairs the user is currently viewing.

    Empty selection means "all calendars across all accounts".
    """

    refs: frozenset[CalendarRef]

    def contains(self, ref: CalendarRef) -> bool:
        return not self.refs or ref in self.refs

    def first_of(self, calendars: Sequence[CalendarRef]) -> CalendarRef | None:
        """The calendar a new event should default to.

        The first one in view, so creating an event while looking at one
        calendar files it there rather than in whichever happens to come
        first alphabetically.
        """
        for ref in calendars:
            if self.contains(ref):
                return ref
        return calendars[0] if calendars else None


class RowEmphasis(StrEnum):
    """How much of the user's attention a row has earned.

    The model decides *that* a row is running or already over; bold,
    dim, a CSS class or nothing at all is the renderer's business.
    """

    NORMAL = "normal"
    IN_PROGRESS = "in_progress"
    PAST = "past"


@dataclass(frozen=True, kw_only=True)
class EventRow:
    """One agenda line: its cells, and how to emphasise them."""

    cells: tuple[str, str, str, str, str, str]
    emphasis: RowEmphasis = RowEmphasis.NORMAL


@dataclass(frozen=True, kw_only=True)
class OccurrenceRow:
    """Joined occurrence + its parent component, ready to render."""

    occurrence: Occurrence
    component: StoredComponent


def _local_midnight(d: date) -> datetime:
    # datetime.combine produces a naive datetime; .astimezone() with no
    # argument presumes naive == system local and attaches the local
    # tzinfo. The result is a tz-aware "local midnight" for `d`. The
    # SQL layer converts back to UTC for the index query, so callers
    # don't need to do that themselves.
    return datetime.combine(d, time.min).astimezone()


def day_window(viewed: date) -> tuple[datetime, datetime]:
    start = _local_midnight(viewed)
    return start, start + timedelta(days=1)


def week_window(viewed: date) -> tuple[datetime, datetime]:
    monday = viewed - timedelta(days=viewed.weekday())
    start = _local_midnight(monday)
    return start, start + timedelta(days=7)


def month_window(viewed: date) -> tuple[datetime, datetime]:
    first = viewed.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return _local_midnight(first), _local_midnight(nxt)


def relative_day_name(day: date, today: date) -> str | None:
    """``"Today"``, ``"Tomorrow"``, ``"Yesterday"`` -- or ``None``.

    Three columns label a day, and each decided which days have a name
    instead of a date for itself.  They agreed on the three; the
    interest is in them going on agreeing, because a view that stopped
    saying "Today" would be the only one not saying it.
    """
    return {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get((day - today).days)


def format_day(day: date, *, with_year: bool = False) -> str:
    """A day as a date, in the one field order the calendar uses.

    Weekday, day, month: "Wed 15 May", ten characters in every month,
    which is what the agenda's Day column pins its width to.  The same
    date used to be written three ways -- ``%a %d %b``, ``%d %b %a`` and
    ``%a %d %b %Y`` -- in three columns of one application.  The year is
    carried only where there is no view title to carry it.
    """
    return day.strftime("%a %d %b %Y" if with_year else "%a %d %b")


def day_column_label(day: date, today: date) -> str:
    """A timeline column's heading: "Today Sat", "Mon 27 Apr".

    Which day a column is showing is the same question in a terminal
    and in a browser, so it has the same answer: the relative name
    where there is one, the date otherwise.  The year comes from the
    view title and never repeats here.
    """
    name = relative_day_name(day, today)
    if name is not None:
        return f"{name} {day.strftime('%a')}"
    return format_day(day)


def step_viewed(
    viewed: date,
    *,
    kind: ViewKind,
    window: AgendaWindow,
    direction: int,
) -> date:
    """The date Prev / Next lands on: the view's own natural unit.

    Day, Grid and the agenda's day window move by a day; the agenda's
    week window by seven; Month and the agenda's month window by one
    calendar month, which ``relativedelta`` clamps to the end of a
    shorter one (31 Jan -> 28 Feb).  Adding 31 days instead lands on
    3 March and skips February altogether, which is why this is one
    function and not one per front end.
    """
    from dateutil.relativedelta import relativedelta

    if kind is ViewKind.MONTH or (
        kind is ViewKind.AGENDA and window is AgendaWindow.MONTH
    ):
        return viewed + relativedelta(months=direction)
    if kind is ViewKind.AGENDA and window is AgendaWindow.WEEK:
        return viewed + timedelta(days=7 * direction)
    return viewed + timedelta(days=direction)


def agenda_window(
    today: date, days: int = DEFAULT_AGENDA_DAYS
) -> tuple[datetime, datetime]:
    start = _local_midnight(today)
    return start, start + timedelta(days=days)


def all_calendar_refs(
    config: AppConfig, mirror: MirrorRepository
) -> tuple[CalendarRef, ...]:
    refs: list[CalendarRef] = []
    for account in config.accounts:
        for calendar_name in mirror.list_calendars(account.name):
            refs.append(CalendarRef(account.name, calendar_name))
    return tuple(refs)


def gather_occurrences(
    *,
    index: IndexRepository,
    calendars: Iterable[CalendarRef],
    selection: CalendarSelection,
    window: tuple[datetime, datetime],
) -> tuple[OccurrenceRow, ...]:
    """Query each calendar for occurrences in `window`, joining components.

    Trashed components are dropped. VTodos with a `due` (or `dtstart`)
    inside the window are injected as synthetic full-day rows so the
    user sees their tasks alongside events without a separate todos
    view. Result is sorted by start time, then by
    `(account, calendar, uid)` for deterministic output.
    """
    rows: list[OccurrenceRow] = []
    for calendar in calendars:
        if not selection.contains(calendar):
            continue
        occurrences = index.query_occurrences(calendar, window[0], window[1])
        # Only the components the window actually names. Reading the
        # whole calendar and joining in Python put every event's raw ICS
        # through SQLite on every render — 44 ms to draw 24 rows out of
        # 3,000 events, paid again by the mail reader's status line.
        components = {
            c.ref: c
            for c in index.list_components_for_uids(
                calendar,
                {occ.ref.uid for occ in occurrences},
            )
        }
        for occ in occurrences:
            component = components.get(occ.ref)
            if component is None:
                continue
            if component.local_status == LocalStatus.TRASHED:
                continue
            rows.append(OccurrenceRow(occurrence=occ, component=component))
        # Inject VTodos as full-day rows.  The window and the active
        # filter are the query's, so this does not read the calendar's
        # events again.
        #
        # This double-counts a task that also has an occurrence row:
        # `populate_occurrences` expands every master, tasks included,
        # so one with an anchor inside the window arrives from the join
        # above as well as from here.  Pre-existing -- the version that
        # read the whole calendar did the same -- and pinned by
        # `tests_calendar/test_occurrences_cache.py`; the comment that
        # used to sit here claimed the cache held only events, which is
        # what hid it.
        for component in index.list_todos(calendar, window=window):
            if not isinstance(component, VTodo):
                continue
            anchor = component.due or component.dtstart
            if anchor is None:
                continue
            day_start = datetime.combine(
                anchor.astimezone(UTC).date(), time.min, tzinfo=UTC
            )
            rows.append(
                OccurrenceRow(
                    occurrence=Occurrence(
                        ref=component.ref,
                        start=day_start,
                        end=day_start + timedelta(days=1),
                        recurrence_id=None,
                        is_override=False,
                    ),
                    component=component,
                )
            )
    rows.sort(key=_row_sort_key)
    return tuple(rows)


def gather_todos(
    *,
    index: IndexRepository,
    calendars: Iterable[CalendarRef],
    selection: CalendarSelection,
) -> tuple[VTodo, ...]:
    out: list[VTodo] = []
    for calendar in calendars:
        if not selection.contains(calendar):
            continue
        # `list_todos` filters on kind and status in SQL: a todo list
        # has no reason to read every event in the calendar first.
        for component in index.list_todos(calendar):
            if isinstance(component, VTodo):
                out.append(component)
    out.sort(key=_todo_sort_key)
    return tuple(out)


def format_friendly_start(start: datetime, today: date) -> str:
    """Render a datetime using day-of-week / Today / Tomorrow shortcuts.

    Converts to the system local timezone so displayed times match the
    clock on the user's machine.
    """
    moment = start.astimezone()
    moment_date = moment.date()
    delta = (moment_date - today).days
    time_str = moment.strftime("%H:%M")
    name = relative_day_name(moment_date, today)
    if name is not None:
        return f"{name} {time_str}"
    if 1 < delta < 7:
        return f"{moment.strftime('%a')} {time_str}"
    if -7 < delta < -1:
        return f"Last {moment.strftime('%a')} {time_str}"
    return f"{format_day(moment_date, with_year=moment.year != today.year)} {time_str}"


def format_duration(start: datetime, end: datetime | None) -> str:
    """Compact human-readable duration: 30m, 1h, 1h30m, 1d, 1d6h."""
    if end is None:
        return ""
    seconds = int((end - start).total_seconds())
    if seconds <= 0:
        return ""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return "".join(parts) or "0m"


def format_event_row(
    row: OccurrenceRow,
    today: date,
    *,
    now: datetime | None = None,
) -> EventRow:
    """Six cells for one agenda line: Day, Time, Duration, Summary,
    Calendar, Location — and how the line should be emphasised.

    Day and Time are separate columns so a renderer can blank the Day
    cell on rows that share the previous row's day, producing a
    paper-agenda look where each day is announced once. The view title
    carries the year, so the Day cell never repeats it.

    When `now` is supplied, a row that has fully ended (its `end`, or
    `start` if there is no end, is strictly before `now`) comes back as
    `PAST`, and one running at `now` as `IN_PROGRESS` with
    `IN_PROGRESS_MARK` before its time. Without `now` every row is
    `NORMAL`: a caller that does not say when now is cannot be told what
    is current.

    VTodo rows (and any synthesised full-day occurrence) get a
    📋 marker on the summary, "all day" in the Time column, and an
    empty Duration cell so the eye doesn't see "1d" repeated for
    every todo.
    """
    full_day = is_full_day(row.occurrence)
    is_todo = isinstance(row.component, VTodo)
    day = _format_event_day(row.occurrence.start, today)
    if full_day:
        event_time = "all day"
        duration = ""
    else:
        event_time = row.occurrence.start.astimezone().strftime("%H:%M")
        duration = format_duration(row.occurrence.start, row.occurrence.end)
    raw_summary = row.component.summary or "(no summary)"
    summary = f"📋 {raw_summary}" if is_todo else raw_summary
    calendar = row.component.ref.calendar_name
    location = row.component.location or ""
    if now is not None and is_in_progress(row.occurrence, now):
        return EventRow(
            cells=(
                day,
                f"{IN_PROGRESS_MARK} {event_time}",
                duration,
                summary,
                calendar,
                location,
            ),
            emphasis=RowEmphasis.IN_PROGRESS,
        )
    cells = (day, event_time, duration, summary, calendar, location)
    if now is not None and _occurrence_is_past(row.occurrence, now):
        return EventRow(cells=cells, emphasis=RowEmphasis.PAST)
    return EventRow(cells=cells)


def _format_event_day(start: datetime, today: date) -> str:
    """Day label for the agenda Day column.

    `Yesterday` / `Today` / `Tomorrow` for those three special days;
    everything else as `format_day` writes a date, which is ten
    characters whatever the month -- the column width pin in
    `EventList` depends on that -- and never carries the year, because
    the view title does.
    """
    moment_date = start.astimezone().date()
    return relative_day_name(moment_date, today) or format_day(moment_date)


def is_full_day(occurrence: Occurrence) -> bool:
    """An occurrence covering whole days, which the grids draw as a banner.

    The rule is :func:`chronos.mutations.is_all_day_span`, asked here of
    an occurrence instead of a stored span, so that what a grid draws as
    all-day is exactly what ``calendar edit`` keeps as ``VALUE=DATE``.
    """
    return is_all_day_span(occurrence.start, occurrence.end)


def _occurrence_is_past(occurrence: Occurrence, now: datetime) -> bool:
    boundary = occurrence.end or occurrence.start
    return boundary < now


def is_in_progress(occurrence: Occurrence, now: datetime) -> bool:
    """True when a timed occurrence is running at `now`.

    Full-day spans are excluded: they would be "in progress" all day
    long, so highlighting them says nothing about the current moment.
    """
    if occurrence.end is None or is_full_day(occurrence):
        return False
    return occurrence.start <= now < occurrence.end


def in_progress_keys(
    rows: Sequence[OccurrenceRow], now: datetime
) -> frozenset[tuple[ComponentRef, datetime]]:
    """`(ref, start)` of every row in progress at `now`.

    Keyed by start as well as ref so only the running instance of a
    recurring event counts, not its siblings on other days.
    """
    return frozenset(
        (row.component.ref, row.occurrence.start)
        for row in rows
        if is_in_progress(row.occurrence, now)
    )


def format_todo_row(todo: VTodo) -> tuple[str, str, str, str]:
    due = (
        todo.due.astimezone().strftime("%Y-%m-%d %H:%M") if todo.due is not None else ""
    )
    summary = todo.summary or "(no summary)"
    calendar = todo.ref.calendar_name
    status = todo.status or ""
    return due, summary, calendar, status


def search_components(
    *,
    components: Sequence[StoredComponent],
    query: str,
) -> tuple[StoredComponent, ...]:
    """Substring search over summary/description/location.

    Used by `SearchDialogScreen` for the live search box. The full
    `index.search()` FTS path is reserved for the MCP server; the TUI
    runs in-memory over already-loaded components for responsiveness
    and to avoid re-querying SQLite on every keystroke.
    """
    needle = query.strip().lower()
    if not needle:
        return ()
    out: list[StoredComponent] = []
    for component in components:
        if component.local_status == LocalStatus.TRASHED:
            continue
        haystack = " ".join(
            field
            for field in (
                component.summary,
                component.description,
                component.location,
            )
            if field
        ).lower()
        if needle in haystack:
            out.append(component)
    return tuple(out)


def _row_sort_key(row: OccurrenceRow) -> tuple[datetime, str, str, str]:
    return (
        row.occurrence.start,
        row.component.ref.account_name,
        row.component.ref.calendar_name,
        row.component.ref.uid,
    )


def _todo_sort_key(todo: VTodo) -> tuple[datetime, str]:
    due = todo.due or datetime.max.replace(tzinfo=UTC)
    return due, todo.ref.uid


#: What the notes section is called, for whoever draws it.  The
#: description is a paragraph rather than a field, so it is not one.
NOTES_LABEL = "Notes:"


@dataclass(frozen=True, slots=True)
class DetailField:
    """One labelled thing an event's detail pane says."""

    label: str
    value: str


def event_detail_fields(
    component: StoredComponent, today: date
) -> tuple[DetailField, ...]:
    """What the detail pane says about *component*, field by field.

    The fields and their order, with no padding and no newlines in
    them.  A monospace pane lines the colons up by padding to its
    longest label and joins them with newlines; a browser's panel would
    use a description list and let CSS decide.  Neither is a projection's
    decision -- this is the shape :class:`MessageRow` has had since the
    message list grew a second front end, and this function used to
    return one pre-padded string.

    Times go through `format_friendly_start`, so "Today 09:00" /
    "Tue 09:00" / "Wed 15 May 09:00" match the row list.  The iCal
    ``UID`` is not shown: it is how the storage layer names a component,
    not something a user asked about.  The description is not a field --
    see :data:`NOTES_LABEL`.
    """
    fields: list[DetailField] = [
        DetailField("Summary", component.summary or "(no summary)"),
        DetailField(
            "Source",
            f"{component.ref.calendar_name} ({component.ref.account_name})",
        ),
        DetailField("Location", component.location or "(no location)"),
    ]
    if isinstance(component, VEvent):
        fields.append(DetailField("Start", _detail_when(component.dtstart, today)))
        fields.append(DetailField("End", _detail_when(component.dtend, today)))
    else:
        if component.dtstart is not None:
            fields.append(DetailField("Start", _detail_when(component.dtstart, today)))
        fields.append(DetailField("Due", _detail_when(component.due, today)))
    if component.status:
        fields.append(DetailField("Status", component.status))
    if isinstance(component, VEvent):
        attendees = extract_attendees(component.raw_ics, component.ref.uid)
        if attendees:
            fields.append(DetailField("Attendees", ", ".join(attendees)))
    alarms = extract_alarm_triggers(component.raw_ics, component.ref.uid)
    if alarms:
        fields.append(
            DetailField("Reminders", ", ".join(_format_alarm(a) for a in alarms))
        )
    fields.append(DetailField(NOTES_LABEL, component.description or "(no notes)"))
    return tuple(fields)


def _detail_when(value: datetime | None, today: date) -> str:
    if value is None:
        return "(not set)"
    return format_friendly_start(value, today)


def _format_alarm(alarm: ParsedAlarm) -> str:
    """Human-readable alarm description for the detail pane."""
    if isinstance(alarm.trigger_offset, datetime):
        return f"at {alarm.trigger_offset.astimezone().strftime('%d %b %H:%M')}"
    total_secs = int(alarm.trigger_offset.total_seconds())
    anchor = alarm.trigger_related.lower()  # "start" or "end"
    if total_secs == 0:
        return f"at {anchor}"
    relation = "before" if total_secs < 0 else "after"
    mins = abs(total_secs) // 60
    if mins < 60:
        return f"{mins} min {relation} {anchor}"
    hours, rem = divmod(mins, 60)
    if rem == 0:
        return f"{hours} h {relation} {anchor}"
    return f"{hours}h{rem}m {relation} {anchor}"


# ----------------------------------------------------------------------
# Grid geometry
# ----------------------------------------------------------------------
#
# Where an occurrence belongs on a time axis or in a month of cells is
# arithmetic over dates, not drawing: the terminal turns the answer into
# `DataTable` rows and the browser into CSS grid lines.  So it lives
# here with the rest of the projections, and both front ends ask the
# same functions rather than each deciding for itself which column a
# Thursday-to-Monday trip occupies.

# One slot of the time axis.  Half an hour is fine enough to place a
# meeting and coarse enough to stay readable in 24 rows of terminal.
SLOT_MINUTES = 30
# The hours a timeline shows when nothing forces it wider.
DEFAULT_START_HOUR = 6
DEFAULT_END_HOUR = 22


def grid_dates(viewed: date) -> tuple[date, int]:
    """First day shown for *viewed*'s month and the number of weeks.

    Weeks run Monday to Sunday; the grid starts on the Monday on or
    before the 1st and ends on the Sunday on or after the last day, so
    it is 4 to 6 weeks tall.
    """
    first = viewed.replace(day=1)
    start = first - timedelta(days=first.weekday())
    next_month = (first + timedelta(days=32)).replace(day=1)
    last = next_month - timedelta(days=1)
    end = last + timedelta(days=6 - last.weekday())
    return start, ((end - start).days + 1) // 7


def full_day_dates(occurrence: Occurrence) -> tuple[date, date]:
    """Inclusive-start, exclusive-end date span of a full-day occurrence.

    Read in the frame its start aligns to: a ``VALUE=DATE`` all-day
    event is anchored at UTC midnight, so its day columns come from the
    UTC dates, while a local-midnight all-day event maps to local
    dates.  Picking the frame that matches the start's midnight lands
    the banner on the right columns either way.
    """
    end = occurrence.end or occurrence.start
    if occurrence.start.astimezone(UTC).time() == time.min:
        return occurrence.start.astimezone(UTC).date(), end.astimezone(UTC).date()
    return occurrence.start.astimezone().date(), end.astimezone().date()


def local_days(row: OccurrenceRow) -> tuple[date, date]:
    """First and last local day an occurrence touches, both inclusive."""
    occurrence = row.occurrence
    if is_full_day(occurrence):
        first, end_exclusive = full_day_dates(occurrence)
        return first, max(first, end_exclusive - timedelta(days=1))
    start = occurrence.start.astimezone()
    end = (occurrence.end or occurrence.start).astimezone()
    if end > start and end.time() == time.min:
        end -= timedelta(microseconds=1)
    return start.date(), max(start.date(), end.date())


def timed_day_minutes(occurrence: Occurrence, day: date) -> tuple[int, int] | None:
    """Minutes-from-local-midnight a timed occurrence covers on *day*.

    ``None`` when it never reaches that day.  A span that began earlier
    starts at 0 and one that ends later runs to ``24 * 60``, so every
    day of a multi-day meeting is covered rather than only the day it
    started on — a 17th-to-25th event has to paint the week opening on
    the 20th, where it neither starts nor ends.

    An end exactly at local midnight belongs to the day before, so an
    event running 09:00 to midnight claims no slot on the following day.
    A zero-length occurrence keeps ``end == start``, which overlaps no
    slot.
    """
    start_local = occurrence.start.astimezone()
    end_local = (occurrence.end or occurrence.start).astimezone()
    start_day = start_local.date()
    end_day = end_local.date()
    if day < start_day or day > end_day:
        return None
    start_min = 0 if day > start_day else start_local.hour * 60 + start_local.minute
    if day < end_day:
        end_min = 24 * 60
    else:
        end_min = end_local.hour * 60 + end_local.minute
        if end_min == 0 and day > start_day:
            return None
    return start_min, max(end_min, start_min)


def compute_hour_range(
    days: Sequence[tuple[date, Sequence[OccurrenceRow]]],
) -> tuple[int, int]:
    """The hours a timeline over *days* has to show.

    Default 06–22; widened if any timed event falls outside it, so an
    early or late event is never invisible.
    """
    start_hour = DEFAULT_START_HOUR
    end_hour = DEFAULT_END_HOUR
    for _, events in days:
        for row in events:
            if is_full_day(row.occurrence):
                continue
            occ_start = row.occurrence.start.astimezone()
            occ_end = (row.occurrence.end or row.occurrence.start).astimezone()
            start_hour = min(start_hour, occ_start.hour)
            if occ_end.date() > occ_start.date():
                # The event runs past local midnight, so it occupies the
                # rest of its own day. Taking the hour off the end would
                # read the wrapped hour on the *next* day — 0 for an event
                # ending at midnight — and leave a 23:00 meeting with no
                # row to appear in.
                tail_hour = 24
            else:
                # End-hour ceiling: if event ends at 22:30, we want a 22:30
                # row, so end_hour must be 23.
                tail_hour = occ_end.hour + (1 if occ_end.minute > 0 else 0)
            end_hour = max(end_hour, tail_hour)
    return start_hour, min(end_hour, 24)


def day_columns(
    rows: Sequence[OccurrenceRow],
    first_day: date,
    count: int,
) -> list[tuple[date, list[OccurrenceRow]]]:
    """Group *rows* into one ``(date, rows)`` pair per shown day.

    Every row goes under each shown day it covers, so a multi-day span —
    including one that began before *first_day* — appears in all of its
    columns.  Full-day spans use their anchoring frame's dates; timed
    ones use local dates, matching :func:`timed_day_minutes`.
    """
    buckets: list[tuple[date, list[OccurrenceRow]]] = [
        (first_day + timedelta(days=offset), []) for offset in range(count)
    ]
    for row in rows:
        if is_full_day(row.occurrence):
            start_d, end_d = full_day_dates(row.occurrence)
            indices = range(
                max(0, (start_d - first_day).days),
                min(
                    count,
                    max((end_d - first_day).days, (start_d - first_day).days + 1),
                ),
            )
            for index in indices:
                buckets[index][1].append(row)
            continue
        start_index = (row.occurrence.start.astimezone().date() - first_day).days
        for index in range(max(0, start_index), count):
            # The helper decides where the span really ends, including
            # the midnight-end case, so the loop only needs a floor.
            if timed_day_minutes(row.occurrence, buckets[index][0]) is None:
                break
            buckets[index][1].append(row)
    return buckets


def full_day_rows(
    day: date,
    events: Sequence[OccurrenceRow],
) -> list[OccurrenceRow]:
    """Full-day rows covering *day*, in the order *events* arrives in.

    Each gets its own banner line, so the order is preserved rather than
    collapsed into a ``+N`` count.  A multi-day span is included on every
    day from its start through the day before its end (``end`` is
    exclusive).
    """
    out: list[OccurrenceRow] = []
    for row in events:
        if not is_full_day(row.occurrence):
            continue
        start_d, end_d = full_day_dates(row.occurrence)
        if start_d <= day < end_d:
            out.append(row)
    return out


def month_buckets(
    rows: Sequence[OccurrenceRow],
    start: date,
    weeks: int,
) -> dict[date, list[OccurrenceRow]]:
    """Every shown day an occurrence covers, all-day items first."""
    end = start + timedelta(days=7 * weeks)
    out: dict[date, list[OccurrenceRow]] = {}
    for row in rows:
        first, last = local_days(row)
        day = max(first, start)
        while day <= last and day < end:
            out.setdefault(day, []).append(row)
            day += timedelta(days=1)
    for bucket in out.values():
        bucket.sort(key=lambda r: (not is_full_day(r.occurrence), r.occurrence.start))
    return out


# ----------------------------------------------------------------------
# What each view is a view of
# ----------------------------------------------------------------------
#
# These were four modules under `tui/screens/` named `*_view_screen.py`,
# none of which held a `Screen` or imported Textual: they were window
# arithmetic, a title and an index query, three per view.  So
# `pony.web.calendar` could not reach them -- the browser must not
# import anything under `chronos.tui` -- and reimplemented the
# arithmetic line for line, with a title that worded it differently.
# One copy each, here, where a second front end can ask.

# Default multi-day grid width, used the first time the grid is opened.
# The user then picks any width from 2 to 7 days live with the `2`-`7`
# keys (the `1` key drops to the dedicated single-day view).
DEFAULT_GRID_DAYS = 4


def agenda_mode_window(viewed: date, mode: AgendaWindow) -> tuple[datetime, datetime]:
    """The agenda's *mode* as a concrete range anchored at *viewed*.

    Calendar-aligned for week and month (Mon-Sun, calendar month) so the
    agenda label matches what users expect when they think "this week"
    or "this month" -- not "the next 7/30 days".

    Anchored on *viewed* rather than on today, so `n` / `p` actually
    navigate: pressing `n` in Agenda Week shifts the agenda by 7 days,
    the new *viewed* lands inside next week, and :func:`week_window`
    snaps to that week's Monday.
    """
    if mode is AgendaWindow.DAY:
        return day_window(viewed)
    if mode is AgendaWindow.WEEK:
        return week_window(viewed)
    return month_window(viewed)


def agenda_title(viewed: date, mode: AgendaWindow) -> str:
    """What the agenda is showing, as a person would say it.

    The browser wrote these four titles a second time and said them
    better -- ``Mon 05 May 2026`` against ``Day · 2026-05-05`` -- so its
    wording is the one that survived the two copies becoming one.  A
    title is read far more often than it is parsed, and an ISO date in a
    heading is a date formatted for a machine that is not reading it.
    """
    return f"Agenda · {mode.value} from {viewed:%d %b %Y}"


def agenda_rows(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
    viewed: date,
    mode: AgendaWindow,
) -> tuple[OccurrenceRow, ...]:
    return gather_occurrences(
        index=index,
        calendars=calendars,
        selection=selection,
        window=agenda_mode_window(viewed, mode),
    )


def day_title(viewed: date) -> str:
    return f"{viewed:%a %d %B %Y}"


def day_rows(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
    viewed: date,
) -> tuple[OccurrenceRow, ...]:
    return gather_occurrences(
        index=index,
        calendars=calendars,
        selection=selection,
        window=day_window(viewed),
    )


def grid_window(
    viewed: date, days: int = DEFAULT_GRID_DAYS
) -> tuple[datetime, datetime]:
    # Anchored at local midnight (see `day_window`) so events at local
    # 00:00-02:00 land in the right day for users not in UTC.
    start = _local_midnight(viewed)
    return start, start + timedelta(days=days)


def grid_title(viewed: date, days: int = DEFAULT_GRID_DAYS) -> str:
    """The span the grid covers, inclusive of both ends.

    The year is named once, at the end, because both dates are nearly
    always in the same one and repeating it reads as noise.
    """
    last = viewed + timedelta(days=days - 1)
    return f"{viewed:%d %b} – {last:%d %b %Y}"


def grid_rows(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
    viewed: date,
    days: int = DEFAULT_GRID_DAYS,
) -> tuple[OccurrenceRow, ...]:
    return gather_occurrences(
        index=index,
        calendars=calendars,
        selection=selection,
        window=grid_window(viewed, days),
    )


def month_grid_window(viewed: date) -> tuple[datetime, datetime]:
    """The whole six-week grid *viewed*'s month is drawn on.

    Not :func:`month_window`, which is the calendar month: the grid
    shows the tail of the previous month and the head of the next, and a
    cell with no events in it is a cell that silently lost them.
    """
    first, weeks = grid_dates(viewed)
    start = _local_midnight(first)
    return start, start + timedelta(days=7 * weeks)


def month_title(viewed: date) -> str:
    return f"{viewed:%B %Y}"


def month_rows(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
    viewed: date,
) -> tuple[OccurrenceRow, ...]:
    return gather_occurrences(
        index=index,
        calendars=calendars,
        selection=selection,
        window=month_grid_window(viewed),
    )


__all__ = [
    "DEFAULT_AGENDA_DAYS",
    "DEFAULT_END_HOUR",
    "DEFAULT_GRID_DAYS",
    "DEFAULT_START_HOUR",
    "IN_PROGRESS_MARK",
    "NOTES_LABEL",
    "SLOT_MINUTES",
    "AgendaWindow",
    "CalendarSelection",
    "DetailField",
    "EventRow",
    "OccurrenceRow",
    "RowEmphasis",
    "ViewKind",
    "agenda_mode_window",
    "agenda_rows",
    "agenda_title",
    "agenda_window",
    "all_calendar_refs",
    "compute_hour_range",
    "day_column_label",
    "day_columns",
    "day_rows",
    "day_title",
    "day_window",
    "event_detail_fields",
    "format_day",
    "format_duration",
    "format_event_row",
    "format_friendly_start",
    "format_todo_row",
    "full_day_dates",
    "full_day_rows",
    "gather_occurrences",
    "gather_todos",
    "grid_dates",
    "grid_rows",
    "grid_title",
    "grid_window",
    "in_progress_keys",
    "is_full_day",
    "is_in_progress",
    "local_days",
    "month_buckets",
    "month_grid_window",
    "month_rows",
    "month_title",
    "month_window",
    "relative_day_name",
    "search_components",
    "step_viewed",
    "timed_day_minutes",
    "week_window",
]
