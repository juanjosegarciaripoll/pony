from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from chronos.domain import CalendarRef
from chronos.protocols import IndexRepository
from chronos.views import (
    CalendarSelection,
    OccurrenceRow,
    gather_occurrences,
    grid_dates,
)


def title_for(viewed: date) -> str:
    return f"Month · {viewed:%B %Y}"


def window_for(viewed: date) -> tuple[datetime, datetime]:
    # Anchored at local midnight (see views.day_window) so every cell
    # holds that local day's events.
    start, weeks = grid_dates(viewed)
    start_dt = datetime.combine(start, time.min).astimezone()
    return start_dt, start_dt + timedelta(days=7 * weeks)


def rows_for(
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
        window=window_for(viewed),
    )


__all__ = ["grid_dates", "rows_for", "title_for", "window_for"]
