"""The calendar in a browser: agenda, timeline and month grid.

The same four views the terminal has, built from the same projections —
:func:`chronos.views.gather_occurrences` for the rows,
:func:`~chronos.views.day_columns` and
:func:`~chronos.views.month_buckets` for where they go, and
:func:`~chronos.views.compute_hour_range` for how tall the time axis has
to be.  What differs is that a CSS grid places a block by percentage
where a ``DataTable`` places it by cell.

Which calendars are shown, which view, and which date all live in the
URL, so a browser's back button means what it looks like it means and a
view can be linked to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from chronos.outcome import Outcome, warn
from chronos.views import (
    AgendaWindow,
    CalendarSelection,
    ViewKind,
    agenda_window,
    all_calendar_refs,
    compute_hour_range,
    day_column_label,
    day_columns,
    day_window,
    format_event_row,
    full_day_rows,
    gather_occurrences,
    grid_dates,
    is_full_day,
    month_buckets,
    month_window,
    step_viewed,
    timed_day_minutes,
    week_window,
)

from . import urls
from .actions import calendar_value, held_for_confirmation, parse_calendar
from .html import Raw, element, fragment, join
from .layout import PageSurface, Tab, columns, page, pane

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chronos.domain import CalendarRef, StoredComponent
    from chronos.event_actions import Calendars
    from chronos.outcome import Notice
    from chronos.views import OccurrenceRow

    from ..calendar import CalendarRuntime
    from .actions import Action, ActionContext, Reply
    from .app import WebServices

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True, kw_only=True, slots=True)
class View:
    """Which calendar view a request asks for, already validated."""

    kind: ViewKind = ViewKind.AGENDA
    window: AgendaWindow = AgendaWindow.WEEK
    viewed: date
    span: int = 1
    selected: frozenset[str] = frozenset()
    query: str = ""

    @property
    def selection(self) -> CalendarSelection:
        refs = {
            parsed
            for parsed in (parse_calendar(value) for value in self.selected)
            if parsed is not None
        }
        return CalendarSelection(refs=frozenset(refs))

    def at(
        self,
        *,
        kind: ViewKind | None = None,
        viewed: date | None = None,
        span: int | None = None,
        window: AgendaWindow | None = None,
    ) -> str:
        """A link to this view with the given changes applied."""
        kind = kind or self.kind
        viewed = viewed or self.viewed
        span = span or self.span
        window = window or self.window
        link = urls.calendar_url(
            view=kind.value,
            date=str(viewed),
            span=span if kind is ViewKind.GRID else None,
            window=window.value if kind is ViewKind.AGENDA else None,
        )
        if self.query:
            link += "&" + urls.query(q=self.query).removeprefix("?")
        for value in sorted(self.selected):
            # Encoded per value: a calendar name is a server's string,
            # and `&` in one would otherwise end the parameter.
            link += "&" + urls.query(cal=value).removeprefix("?")
        return link


def read_view(params: dict[str, list[str]], *, today: date) -> View:
    """The view a query string asks for, falling back to the agenda."""

    def one(name: str, default: str = "") -> str:
        values = params.get(name, [])
        return values[0] if values else default

    try:
        viewed = date.fromisoformat(one("date")) if one("date") else today
    except ValueError:
        viewed = today
    kind = ViewKind.AGENDA
    for candidate in ViewKind:
        if candidate.value == one("view"):
            kind = candidate
    window = AgendaWindow.WEEK
    for candidate_window in AgendaWindow:
        if candidate_window.value == one("window"):
            window = candidate_window
    span = one("span")
    return View(
        kind=kind,
        window=window,
        viewed=viewed,
        span=int(span) if span.isdigit() and 1 <= int(span) <= 7 else 1,
        selected=frozenset(params.get("cal", [])),
        query=one("q"),
    )


# ----------------------------------------------------------------------
# The page
# ----------------------------------------------------------------------


def calendar_page(
    services: WebServices,
    view: View,
    *,
    notices: Sequence[Notice] = (),
) -> str:
    """One calendar view, with its own navigation beside it."""
    calendar = services.calendar
    if calendar is None:
        return page(
            title="Calendar — Pony Express",
            surface=PageSurface.CALENDAR,
            tab=Tab.CALENDAR,
            status="No calendar configured",
            panes=pane(
                name="main",
                heading=element("span", "Calendar", class_="title"),
                body=element(
                    "p",
                    "No calendar configured — add a [calendar] table to config.toml.",
                    class_="empty",
                ),
            ),
            notices=notices,
        )
    now = datetime.now(UTC)
    today = now.astimezone().date()
    calendars = all_calendar_refs(calendar.config, calendar.mirror)
    rows = gather_occurrences(
        index=calendar.index,
        calendars=calendars,
        selection=view.selection,
        window=_search_window(now) if view.query else _window(view),
    )
    if view.query:
        rows = _matching(rows, view.query)
    return page(
        title=f"{_title(view)} — Pony Express",
        surface=PageSurface.CALENDAR,
        tab=Tab.CALENDAR,
        status=_title(view),
        panes=fragment(
            _nav(view, calendars),
            _view_pane(view, rows, today=today, now=now),
        ),
        data={
            "view": view.kind.value,
            "date": str(view.viewed),
            "span": str(view.span),
            "window": view.window.value,
            # Which calendars are shown, so a key that navigates keeps
            # the filter. `View.at()` re-encodes it carefully for every
            # link the server builds and the script dropped it on every
            # one it built itself, silently resetting the view to "all
            # calendars". JSON because a calendar name may contain
            # anything a server likes, a space and a quote included.
            "cals": json.dumps(sorted(view.selected)),
        },
        notices=notices,
    )


def _matching(
    rows: Sequence[OccurrenceRow],
    query: str,
) -> tuple[OccurrenceRow, ...]:
    """The occurrences whose component matches *query*.

    The same substring search over summary, description and location
    that the terminal's search dialog runs, applied to the rows the
    window already produced: a search here narrows what is on screen
    rather than opening a view of its own.
    """
    from chronos.views import search_components

    matched = {
        component.ref
        for component in search_components(
            components=[row.component for row in rows],
            query=query,
        )
    }
    return tuple(row for row in rows if row.component.ref in matched)


def _search_window(now: datetime) -> tuple[datetime, datetime]:
    """Everything a search can see: the whole occurrence cache.

    Narrowing a search to the window on screen means it cannot find
    next month's event, which the terminal's search dialog can -- it
    reads every component of every calendar.  The bounds here are the
    cache's own, so this asks for exactly what has been expanded.
    """
    from chronos.recurrence import OCCURRENCE_WINDOW_FUTURE, OCCURRENCE_WINDOW_PAST

    return now - OCCURRENCE_WINDOW_PAST, now + OCCURRENCE_WINDOW_FUTURE


def _window(view: View) -> tuple[datetime, datetime]:
    match view.kind:
        case ViewKind.DAY:
            return day_window(view.viewed)
        case ViewKind.GRID:
            start, _end = day_window(view.viewed)
            return start, start + timedelta(days=view.span)
        case ViewKind.MONTH:
            first, weeks = grid_dates(view.viewed)
            start = datetime.combine(first, time.min).astimezone()
            return start, start + timedelta(days=7 * weeks)
    match view.window:
        case AgendaWindow.DAY:
            return day_window(view.viewed)
        case AgendaWindow.WEEK:
            return week_window(view.viewed)
        case AgendaWindow.MONTH:
            return month_window(view.viewed)
    return agenda_window(view.viewed)


def _title(view: View) -> str:
    if view.query:
        return f"Search: {view.query}"
    match view.kind:
        case ViewKind.DAY:
            return f"{view.viewed:%a %d %B %Y}"
        case ViewKind.GRID:
            last = view.viewed + timedelta(days=view.span - 1)
            return f"{view.viewed:%d %b} – {last:%d %b %Y}"
        case ViewKind.MONTH:
            return f"{view.viewed:%B %Y}"
    return f"Agenda · {view.window.value} from {view.viewed:%d %b %Y}"


def _nav(view: View, calendars: Sequence[CalendarRef]) -> Raw:
    """Views, dates and calendars — the calendar's left-hand pane."""
    views = (
        ("Agenda", view.at(kind=ViewKind.AGENDA), view.kind is ViewKind.AGENDA),
        ("Day", view.at(kind=ViewKind.DAY), view.kind is ViewKind.DAY),
        (
            "Week",
            view.at(kind=ViewKind.GRID, span=7),
            view.kind is ViewKind.GRID,
        ),
        ("Month", view.at(kind=ViewKind.MONTH), view.kind is ViewKind.MONTH),
    )
    return pane(
        name="folders",
        heading=columns(("folder-name", "Calendar")),
        body=fragment(
            element(
                "div",
                join(
                    element(
                        "a",
                        label,
                        href=link,
                        class_="view-link current" if current else "view-link",
                    )
                    for label, link, current in views
                ),
                class_="view-switch",
            ),
            element(
                "div",
                element(
                    "a",
                    "‹ Prev",
                    href=view.at(viewed=_stepped(view, -1)),
                    class_="date-step",
                    id="prev-date",
                ),
                element(
                    "a",
                    "Today",
                    href=view.at(viewed=datetime.now(UTC).astimezone().date()),
                    class_="date-step",
                    id="today",
                ),
                element(
                    "a",
                    "Next ›",
                    href=view.at(viewed=_stepped(view, 1)),
                    class_="date-step",
                    id="next-date",
                ),
                class_="date-nav",
            ),
            element(
                "section",
                element("h2", "Calendars"),
                element(
                    "ul",
                    join(_calendar_toggle(view, ref) for ref in calendars),
                    class_="tree",
                )
                if calendars
                else element("p", "None yet — sync to fetch them.", class_="empty"),
            ),
            element(
                "a",
                "New event",
                href=urls.event_form_url(date=str(view.viewed)),
                class_="action primary",
                id="new-event",
            ),
            element(
                "form",
                element(
                    "input",
                    type="search",
                    name="q",
                    value=view.query,
                    placeholder="Search events",
                    aria_label="Search events",
                ),
                element("input", type="hidden", name="view", value=view.kind.value),
                element("input", type="hidden", name="date", value=str(view.viewed)),
                # The whole view, not half of it: searching used to
                # reset the span, the agenda window and the calendar
                # filter while Prev and Next kept all three.
                element(
                    "input",
                    type="hidden",
                    name="span",
                    value=str(view.span),
                )
                if view.kind is ViewKind.GRID
                else None,
                element(
                    "input",
                    type="hidden",
                    name="window",
                    value=view.window.value,
                )
                if view.kind is ViewKind.AGENDA
                else None,
                join(
                    element("input", type="hidden", name="cal", value=value)
                    for value in sorted(view.selected)
                ),
                method="get",
                action=urls.CALENDAR,
                class_="search-form",
            ),
        ),
    )


def _stepped(view: View, direction: int) -> date:
    """Where Prev / Next lands, by the shared rule."""
    return step_viewed(
        view.viewed,
        kind=view.kind,
        window=view.window,
        direction=direction,
    )


def _calendar_toggle(view: View, ref: CalendarRef) -> Raw:
    """A calendar, linked to the view with it added or taken away.

    Empty selection means every calendar, which is what an unfiltered
    view should show. Clicking one narrows to it, clicking another adds
    it, and clicking the last one off widens back out — the terminal's
    calendar panel, as links.
    """
    value = calendar_value(ref)
    shown = value in view.selected
    selected = view.selected - {value} if shown else view.selected | {value}
    toggled = View(
        kind=view.kind,
        window=view.window,
        viewed=view.viewed,
        span=view.span,
        selected=frozenset(selected),
        query=view.query,
    )
    return element(
        "li",
        element(
            "a",
            element("span", ref.calendar_name, class_="folder-name"),
            element("span", ref.account_name, class_="account-name"),
            href=toggled.at(),
            class_="folder current" if shown else "folder",
        ),
    )


def _view_pane(
    view: View,
    rows: Sequence[OccurrenceRow],
    *,
    today: date,
    now: datetime,
) -> Raw:
    if view.query:
        # Results are a list wherever the search was launched from: a
        # timeline has nowhere to put a match from another month.
        return _agenda_pane(view, rows, today=today, now=now)
    match view.kind:
        case ViewKind.MONTH:
            return _month_pane(view, rows, today=today, now=now)
        case ViewKind.DAY:
            return _timeline_pane(view, rows, days=1, today=today, now=now)
        case ViewKind.GRID:
            return _timeline_pane(view, rows, days=view.span, today=today, now=now)
    return _agenda_pane(view, rows, today=today, now=now)


# ----------------------------------------------------------------------
# The agenda
# ----------------------------------------------------------------------


def _agenda_pane(
    view: View,
    rows: Sequence[OccurrenceRow],
    *,
    today: date,
    now: datetime,
) -> Raw:
    del view
    entries = [
        element(
            "li",
            element(
                "a",
                element("span", projected.cells[0], class_="day"),
                element("span", projected.cells[1], class_="time"),
                element("span", projected.cells[2], class_="duration"),
                element("span", projected.cells[3], class_="summary"),
                element("span", projected.cells[5], class_="location"),
                href=_event_link(row),
            ),
            class_=f"row agenda-row {projected.emphasis}",
            data_id=_event_id(row),
            data_cal=calendar_value(row.component.ref.calendar),
            data_uid=row.component.ref.uid,
        )
        for row, projected in (
            (row, format_event_row(row, today, now=now)) for row in rows
        )
    ]
    return pane(
        name="main",
        classes="agenda",
        heading=columns(
            ("day", "Day"),
            ("time", "Time"),
            ("duration", "Length"),
            ("summary", "Event"),
            ("location", "Where"),
        ),
        body=element("ol", join(entries), id="rows", class_="rows")
        if entries
        else element("p", "Nothing in this window.", class_="empty", id="rows"),
    )


# ----------------------------------------------------------------------
# The timeline
# ----------------------------------------------------------------------


def _timeline_pane(
    view: View,
    rows: Sequence[OccurrenceRow],
    *,
    days: int,
    today: date,
    now: datetime,
) -> Raw:
    buckets = day_columns(rows, view.viewed, days)
    start_hour, end_hour = compute_hour_range(buckets)
    hours = max(1, end_hour - start_hour)
    labels = [
        element("span", f"{hour:02d}:00", class_="hour-label")
        for hour in range(start_hour, end_hour)
    ]
    return pane(
        name="main",
        classes="timeline-pane",
        heading=element(
            "div",
            element("span", "", class_="hour-gutter"),
            join(
                element(
                    "span",
                    day_column_label(day, today),
                    class_="day-head today" if day == today else "day-head",
                )
                for day, _events in buckets
            ),
            class_="timeline-head",
        ),
        body=fragment(
            _all_day_banner(buckets),
            element(
                "div",
                element("div", join(labels), class_="hours"),
                join(
                    _day_column(
                        day,
                        events,
                        start_hour=start_hour,
                        hours=hours,
                        now=now,
                    )
                    for day, events in buckets
                ),
                class_="timeline",
                style=(
                    f"--hours:{hours};--columns:{len(buckets)};"
                    f"--start-hour:{start_hour}"
                ),
            ),
        ),
    )


def _all_day_banner(
    buckets: Sequence[tuple[date, Sequence[OccurrenceRow]]],
) -> Raw | None:
    """The full-day row above the axis, when any day has one."""
    banners = [(day, full_day_rows(day, events)) for day, events in buckets]
    if not any(rows for _day, rows in banners):
        return None
    return element(
        "div",
        element("span", "all day", class_="hour-gutter"),
        join(
            element(
                "div",
                join(
                    element(
                        "a",
                        row.component.summary or "(no summary)",
                        href=_event_link(row),
                        class_="event all-day",
                        data_id=_event_id(row),
                        data_cal=calendar_value(row.component.ref.calendar),
                        data_uid=row.component.ref.uid,
                    )
                    for row in rows
                ),
                class_="banner-cell",
            )
            for _day, rows in banners
        ),
        class_="all-day-banner",
        style=f"--columns:{len(banners)}",
    )


def _day_column(
    day: date,
    events: Sequence[OccurrenceRow],
    *,
    start_hour: int,
    hours: int,
    now: datetime,
) -> Raw:
    """One day's timed events, placed by where they are in the window."""
    from chronos.views import is_in_progress

    blocks: list[Raw] = []
    for row in events:
        if is_full_day(row.occurrence):
            continue
        minutes = timed_day_minutes(row.occurrence, day)
        if minutes is None:
            continue
        top, height = _placement(minutes, start_hour=start_hour, hours=hours)
        if height <= 0:
            continue
        local = row.occurrence.start.astimezone()
        classes = ["event"]
        if is_in_progress(row.occurrence, now):
            classes.append("in-progress")
        blocks.append(
            element(
                "a",
                element("span", f"{local:%H:%M}", class_="event-time"),
                element(
                    "span",
                    row.component.summary or "(no summary)",
                    class_="event-summary",
                ),
                href=_event_link(row),
                class_=" ".join(classes),
                data_id=_event_id(row),
                data_cal=calendar_value(row.component.ref.calendar),
                data_uid=row.component.ref.uid,
                style=f"top:{top:.4f}%;height:{height:.4f}%",
            ),
        )
    return element(
        "div",
        join(blocks),
        class_="day-column",
        data_date=str(day),
    )


def _placement(
    minutes: tuple[int, int],
    *,
    start_hour: int,
    hours: int,
) -> tuple[float, float]:
    """Where a block sits in its column, as percentages of the window."""
    window_start = start_hour * 60
    window_minutes = hours * 60
    start = max(minutes[0], window_start)
    end = min(minutes[1], window_start + window_minutes)
    top = (start - window_start) / window_minutes * 100
    height = max(end - start, 15) / window_minutes * 100
    return top, min(height, 100 - top)


# ----------------------------------------------------------------------
# The month grid
# ----------------------------------------------------------------------

# How many events a month cell lists before it says "+N more".
MONTH_CELL_EVENTS = 3


def _month_pane(
    view: View,
    rows: Sequence[OccurrenceRow],
    *,
    today: date,
    now: datetime,
) -> Raw:
    del now
    first, weeks = grid_dates(view.viewed)
    buckets = month_buckets(rows, first, weeks)
    cells: list[Raw] = []
    for offset in range(7 * weeks):
        day = first + timedelta(days=offset)
        cells.append(
            _month_cell(
                day,
                buckets.get(day, []),
                in_month=day.month == view.viewed.month,
                today=today,
                view=view,
            ),
        )
    return pane(
        name="main",
        classes="month-pane",
        heading=element(
            "div",
            join(element("span", name, class_="day-head") for name in WEEKDAYS),
            class_="month-head",
        ),
        body=element(
            "div",
            join(cells),
            class_="month",
            style=f"--weeks:{weeks}",
            id="rows",
        ),
    )


def _month_cell(
    day: date,
    events: Sequence[OccurrenceRow],
    *,
    in_month: bool,
    today: date,
    view: View,
) -> Raw:
    shown = events[:MONTH_CELL_EVENTS]
    hidden = len(events) - len(shown)
    classes = ["month-cell"]
    if not in_month:
        classes.append("other-month")
    if day == today:
        classes.append("today")
    return element(
        "div",
        element(
            "a",
            str(day.day),
            href=view.at(kind=ViewKind.DAY, viewed=day),
            class_="day-number",
        ),
        join(
            element(
                "a",
                element("span", _cell_time(row, day), class_="event-time"),
                element(
                    "span",
                    row.component.summary or "(no summary)",
                    class_="event-summary",
                ),
                href=_event_link(row),
                class_="event",
                data_id=_event_id(row),
                data_cal=calendar_value(row.component.ref.calendar),
                data_uid=row.component.ref.uid,
            )
            for row in shown
        ),
        element("span", f"+{hidden} more", class_="more") if hidden > 0 else None,
        class_=" ".join(classes),
        data_date=str(day),
    )


def _cell_time(row: OccurrenceRow, day: date) -> str:
    start = row.occurrence.start.astimezone()
    if is_full_day(row.occurrence) or start.date() != day:
        return ""
    return f"{start:%H:%M}"


def _event_id(row: OccurrenceRow) -> str:
    """What identifies a row to the page: the calendar and the UID.

    The two travel separately as ``data-cal`` and ``data-uid`` as well,
    because this one string cannot be taken apart again: a calendar name
    may contain a slash and so may a UID, so the page used to split it
    at the second slash and guess.
    """
    ref = row.component.ref
    return f"{calendar_value(ref.calendar)}/{ref.uid}"


def _event_link(row: OccurrenceRow) -> str:
    ref = row.component.ref
    return urls.event_form_url(
        account=ref.account_name,
        calendar=ref.calendar_name,
        uid=ref.uid,
    )


# ----------------------------------------------------------------------
# The event form
# ----------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class EventForm:
    """An event's fields as the form holds them."""

    target: str = ""
    summary: str = ""
    starts: str = ""
    ends: str = ""
    all_day: bool = False
    location: str = ""
    description: str = ""
    attendees: str = ""
    reminders: str = ""
    uid: str = ""

    @staticmethod
    def of(component: StoredComponent) -> EventForm:
        from chronos.event_actions import (
            all_day_form_dates,
            existing_alarms,
            reminders_text,
        )
        from chronos.ical_parser import extract_attendees
        from chronos.mutations import is_all_day_span

        ref = component.ref
        dtend = getattr(component, "dtend", None)
        all_day = is_all_day_span(component.dtstart, dtend)
        if all_day:
            # The dates, with the *inclusive* last day, so saving an
            # event again without touching it leaves it where it was.
            # Both fields stay ``datetime-local``, so the dates carry a
            # midnight the all-day flag tells everything to ignore.
            first, last = all_day_form_dates(component.dtstart, dtend)
            return EventForm(
                target=calendar_value(ref.calendar),
                summary=component.summary or "",
                starts=f"{first}T00:00" if first else "",
                ends=f"{last}T00:00" if last else "",
                all_day=True,
                location=component.location or "",
                description=component.description or "",
                attendees=", ".join(extract_attendees(component.raw_ics, ref.uid)),
                reminders=reminders_text(existing_alarms(component)),
                uid=ref.uid,
            )
        return EventForm(
            target=calendar_value(ref.calendar),
            summary=component.summary or "",
            starts=_local_field(component.dtstart),
            # A VTodo has a due date rather than an end; the form only
            # edits events, so there is nothing to show for one.
            ends=_local_field(dtend),
            all_day=False,
            location=component.location or "",
            description=component.description or "",
            attendees=", ".join(extract_attendees(component.raw_ics, ref.uid)),
            reminders=reminders_text(existing_alarms(component)),
            uid=ref.uid,
        )

    @staticmethod
    def blank(*, target: str, day: date) -> EventForm:
        from chronos.event_actions import default_event_start

        start = default_event_start(day, datetime.now(UTC))
        return EventForm(
            target=target,
            starts=_local_field(start),
            ends=_local_field(start + timedelta(hours=1)),
        )


def _local_field(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.astimezone().strftime("%Y-%m-%dT%H:%M")


def event_form_page(
    services: WebServices,
    form: EventForm,
    *,
    notices: Sequence[Notice] = (),
) -> str:
    """One event's fields, new or existing."""
    calendar = services.calendar
    targets = (
        []
        if calendar is None
        else list(all_calendar_refs(calendar.config, calendar.mirror))
    )
    heading = form.summary or "New event"
    return page(
        title=f"{heading} — Pony Express",
        surface=PageSurface.EVENT,
        tab=Tab.CALENDAR,
        status=heading,
        panes=pane(
            name="compose",
            heading=fragment(
                element("span", heading, class_="title"),
                element(
                    "span",
                    element(
                        "button",
                        "Save",
                        type="submit",
                        form="event-form",
                        class_="action primary",
                    ),
                    element(
                        "button",
                        "Delete",
                        type="button",
                        class_="action",
                        id="delete-event",
                        data_uid=form.uid,
                        data_target=form.target,
                    )
                    if form.uid
                    else None,
                    element("a", "Cancel", href=urls.CALENDAR, class_="action"),
                    class_="actions",
                ),
            ),
            body=_event_fields(form, targets),
        ),
        data={"uid": form.uid, "target": form.target},
        notices=notices,
    )


def _event_fields(form: EventForm, targets: Sequence[CalendarRef]) -> Raw:
    return element(
        "form",
        element("input", type="hidden", name="uid", value=form.uid),
        element(
            "div",
            element("label", "Calendar"),
            element(
                "select",
                join(
                    element(
                        "option",
                        calendar_value(ref),
                        value=calendar_value(ref),
                        selected=calendar_value(ref) == form.target,
                    )
                    for ref in targets
                ),
                name="target",
                id="target",
            ),
            class_="field",
        ),
        _event_field("Summary", "summary", form.summary, autofocus=True),
        _event_field("Starts", "starts", form.starts, kind="datetime-local"),
        _event_field("Ends", "ends", form.ends, kind="datetime-local"),
        element(
            "div",
            element(
                "label",
                element(
                    "input",
                    type="checkbox",
                    name="all_day",
                    value="1",
                    checked=form.all_day,
                ),
                element("span", "All day"),
                class_="checkbox",
            ),
            class_="field",
        ),
        _event_field("Where", "location", form.location),
        _event_field("Attendees", "attendees", form.attendees),
        _event_field(
            "Reminders (minutes before)",
            "reminders",
            form.reminders,
        ),
        element(
            "div",
            element("label", "Description"),
            element(
                "textarea",
                form.description,
                name="description",
                id="description",
                rows="8",
            ),
            class_="field",
        ),
        id="event-form",
        method="post",
        action=urls.EVENT_FORM,
    )


def _event_field(
    label: str,
    name: str,
    value: str,
    *,
    kind: str = "text",
    autofocus: bool = False,
) -> Raw:
    return element(
        "div",
        element("label", label),
        element(
            "input",
            type=kind,
            name=name,
            id=name,
            value=value,
            autocomplete="off",
            autofocus=autofocus,
        ),
        class_="field",
    )


def form_from_fields(fields: dict[str, list[str]]) -> EventForm:
    def one(name: str) -> str:
        values = fields.get(name, [])
        return values[0] if values else ""

    return EventForm(
        target=one("target"),
        summary=one("summary"),
        starts=one("starts"),
        ends=one("ends"),
        all_day=bool(one("all_day")),
        location=one("location"),
        description=one("description"),
        attendees=one("attendees"),
        reminders=one("reminders"),
        uid=one("uid"),
    )


def calendars_of(runtime: CalendarRuntime) -> Calendars:
    """The stores the calendar's own operations work over."""
    from chronos.event_actions import Calendars

    return Calendars(
        config=runtime.config,
        mirror=runtime.mirror,
        index=runtime.index,
    )


def save(services: WebServices, form: EventForm) -> Outcome:
    """Store *form* as a new event or an edit of an existing one."""
    from chronos.event_actions import (
        EditDraft,
        all_day_form_bounds,
        parse_attendees,
        parse_reminders,
        save_event,
    )

    runtime = services.calendar
    if runtime is None:
        return Outcome.refused(warn("No calendar configured."))
    target = parse_calendar(form.target)
    if target is None:
        return Outcome.refused(warn("Choose a calendar."))
    if not form.summary.strip():
        return Outcome.refused(warn("An event needs a summary."))
    starts = _parse_moment(form.starts)
    if starts is None:
        return Outcome.refused(warn("The start is not a date and time."))
    ends = _parse_moment(form.ends)
    if form.all_day:
        # An all-day event is stored as UTC-midnight bounds with an
        # exclusive end, and the form's end date is the inclusive last
        # day.  Handing over the raw local values instead stores the
        # previous day east of UTC.
        starts, ends = all_day_form_bounds(
            starts.date(),
            ends.date() if ends is not None else None,
        )
    elif ends is not None and ends <= starts:
        return Outcome.refused(warn("The event ends before it starts."))
    try:
        attendees = parse_attendees(form.attendees)
    except ValueError as bad:
        return Outcome.refused(warn(str(bad)))
    existing = (
        _existing(services, uid=form.uid, target=form.target) if form.uid else None
    )
    draft = EditDraft(
        target=target,
        summary=form.summary.strip(),
        dtstart=starts,
        dtend=ends,
        location=form.location.strip(),
        description=form.description.strip(),
        attendees=attendees,
        alarms=parse_reminders(form.reminders),
        existing=existing,
        all_day=form.all_day,
    )
    saved = save_event(calendars_of(runtime), draft, now=datetime.now(UTC))
    return saved.outcome


def _parse_moment(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def _existing(
    services: WebServices,
    *,
    uid: str,
    target: str = "",
) -> StoredComponent | None:
    """The component *uid* names, in the calendar *target* names.

    *target* is ``account/calendar`` as a form or a row spells it.  A
    UID is unique within a calendar and not beyond it, so without it
    the lookup would pick an arbitrary copy.
    """
    from chronos.event_actions import find_component

    runtime = services.calendar
    if runtime is None:
        return None
    return find_component(runtime.index, uid, within=parse_calendar(target))


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------


def _delete_event(context: ActionContext) -> Outcome | Reply:
    """Trash the event, once the user has said so.

    The terminal asks before deleting an event and the browser did not,
    which is the same operation reached two ways with the question on
    only one of them.
    """
    from chronos.event_actions import trash_event

    runtime = context.services.calendar
    if runtime is None:
        return Outcome.refused(warn("No calendar configured."))
    component = _existing(
        context.services,
        uid=context.first("uid"),
        target=context.first("cal"),
    )
    if component is None:
        return Outcome.refused(warn("That event is no longer here."))
    held = held_for_confirmation(
        context,
        f"Delete {component.summary or 'this event'}?",
    )
    if held is not None:
        return held
    return trash_event(calendars_of(runtime), component, now=context.now)


def _import_ics(context: ActionContext) -> Outcome:
    """File pasted iCalendar text, which is what an import is here.

    The terminal reads a path; a browser has no path to offer, and
    uploading a file to be parsed is the same thing with an extra step.
    """
    from chronos.event_actions import import_ics_bytes

    runtime = context.services.calendar
    if runtime is None:
        return Outcome.refused(warn("No calendar configured."))
    into = parse_calendar(context.first("target"))
    if into is None:
        return Outcome.refused(warn("Choose a calendar to import into."))
    payload = _ics_payload(context)
    if payload is None:
        return Outcome.refused(
            warn("Paste the iCalendar text, or open a message that carries one."),
        )
    return import_ics_bytes(calendars_of(runtime), into, payload)


def _ics_payload(context: ActionContext) -> bytes | None:
    """The iCalendar bytes to import: pasted text, or an attachment.

    A `.ics` someone sent is the common case and nobody should have to
    copy it out of a message by hand, so an attachment is named by the
    message it is on and its index -- the same pair the download link
    carries -- and read back here rather than travelling through the
    browser twice.
    """
    text = context.first("ics").strip()
    if text:
        return text.encode()
    index = context.integer("attachment")
    messages = context.messages
    if index is None or not messages:
        return None
    from ..message_renderer import extract_attachment
    from .mail import raw_bytes

    raw = raw_bytes(context.services, messages[0])
    if raw is None:
        return None
    payload = extract_attachment(raw, index)
    return None if payload is None else payload.data


CALENDAR_ACTIONS: dict[str, Action] = {
    "delete_event": _delete_event,
    "import_ics": _import_ics,
}


__all__ = [
    "CALENDAR_ACTIONS",
    "MONTH_CELL_EVENTS",
    "WEEKDAYS",
    "EventForm",
    "View",
    "calendar_page",
    "event_form_page",
    "form_from_fields",
    "read_view",
    "save",
]
