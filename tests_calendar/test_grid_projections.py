"""Where an occurrence belongs on a grid, with no grid to draw it on.

The month cells, the day columns and the hour range used to be private
helpers inside the two Textual widgets, which meant a browser could
only have them by writing them again — and a second implementation of
"which columns does a Thursday-to-Monday trip occupy" is a second set
of answers.  They are projections in :mod:`pony.calendar.views` now, and these
tests pin the arithmetic directly.
"""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, time, timedelta

from pony.calendar.domain import ComponentRef, LocalStatus, Occurrence, VEvent
from pony.calendar.views import (
    OccurrenceRow,
    compute_hour_range,
    day_column_label,
    day_columns,
    format_friendly_start,
    full_day_dates,
    full_day_rows,
    grid_dates,
    local_days,
    month_buckets,
    relative_day_name,
    timed_day_minutes,
)

ACCOUNT = "personal"
CALENDAR = "work"


def _row(
    uid: str,
    start: datetime,
    end: datetime | None,
    *,
    summary: str = "Event",
) -> OccurrenceRow:
    ref = ComponentRef(ACCOUNT, CALENDAR, uid)
    return OccurrenceRow(
        occurrence=Occurrence(
            ref=ref,
            start=start,
            end=end,
            recurrence_id=None,
            is_override=False,
        ),
        component=VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary=summary,
            description=None,
            location=None,
            dtstart=start,
            dtend=end,
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        ),
    )


def _timed(
    day: date, hour: int, *, minutes: int = 60, uid: str = "timed"
) -> OccurrenceRow:
    start = datetime.combine(day, time(hour=hour)).astimezone()
    return _row(uid, start, start + timedelta(minutes=minutes))


def _all_day(first: date, days: int = 1, *, uid: str = "all-day") -> OccurrenceRow:
    start = datetime.combine(first, time.min, tzinfo=UTC)
    return _row(uid, start, start + timedelta(days=days))


class MonthGeometryTest(unittest.TestCase):
    def test_a_month_grid_spans_whole_weeks(self) -> None:
        self.assertEqual(grid_dates(date(2026, 9, 17)), (date(2026, 8, 31), 5))
        # February 2027 starts on a Monday and ends on a Sunday exactly.
        self.assertEqual(grid_dates(date(2027, 2, 10)), (date(2027, 2, 1), 4))
        self.assertEqual(grid_dates(date(2026, 8, 1)), (date(2026, 7, 27), 6))

    def test_every_day_a_span_touches_gets_the_event(self) -> None:
        start = date(2026, 9, 1)
        trip = _all_day(date(2026, 9, 2), days=3, uid="trip")
        evening = _timed(date(2026, 9, 4), 19, uid="evening")

        buckets = month_buckets([trip, evening], start, 5)

        self.assertEqual(
            sorted(buckets),
            [date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)],
        )
        # All-day items come first in a cell, whatever order they arrived.
        self.assertEqual(
            [row.component.ref.uid for row in buckets[date(2026, 9, 4)]],
            ["trip", "evening"],
        )

    def test_a_span_that_began_before_the_grid_still_fills_its_cells(self) -> None:
        trip = _all_day(date(2026, 8, 28), days=6, uid="trip")

        buckets = month_buckets([trip], date(2026, 9, 1), 1)

        self.assertEqual(sorted(buckets), [date(2026, 9, 1), date(2026, 9, 2)])

    def test_the_last_day_of_a_full_day_span_is_the_day_before_its_end(self) -> None:
        """``DTEND`` is exclusive, so a one-day event is one cell."""
        single = _all_day(date(2026, 9, 2))

        self.assertEqual(local_days(single), (date(2026, 9, 2), date(2026, 9, 2)))
        self.assertEqual(
            full_day_dates(single.occurrence),
            (date(2026, 9, 2), date(2026, 9, 3)),
        )

    def test_a_timed_event_ending_at_midnight_belongs_to_the_day_it_started(
        self,
    ) -> None:
        start = datetime.combine(date(2026, 9, 2), time(hour=21)).astimezone()
        evening = _row("evening", start, start + timedelta(hours=3))

        self.assertEqual(local_days(evening), (date(2026, 9, 2), date(2026, 9, 2)))


class TimelineGeometryTest(unittest.TestCase):
    def test_the_default_hour_range_covers_the_working_day(self) -> None:
        day = date(2026, 5, 1)
        inside = [_timed(day, 9), _timed(day, 14, uid="later")]

        self.assertEqual(compute_hour_range([(day, inside)]), (6, 22))

    def test_an_early_or_late_event_widens_the_range(self) -> None:
        day = date(2026, 5, 1)
        early = _timed(day, 4, uid="early")
        late = _timed(day, 22, minutes=90, uid="late")

        self.assertEqual(compute_hour_range([(day, [early, late])]), (4, 24))

    def test_a_full_day_item_does_not_widen_the_range(self) -> None:
        """It is shown in the banner row, not on the time axis."""
        day = date(2026, 5, 1)

        self.assertEqual(compute_hour_range([(day, [_all_day(day)])]), (6, 22))

    def test_a_day_column_holds_every_event_that_reaches_it(self) -> None:
        first = date(2026, 5, 20)
        # 09:45 local, because a multi-day span that happens to begin at
        # midnight in *either* frame is a full-day span by
        # `is_full_day`'s rule, and no real zone is offset by 9h45.
        trip = _row(
            "trip",
            datetime.combine(first, time(hour=9, minute=45)).astimezone(),
            datetime.combine(first + timedelta(days=2), time(hour=17)).astimezone(),
        )

        columns = day_columns([trip], first, 4)

        self.assertEqual([len(rows) for _day, rows in columns], [1, 1, 1, 0])

    def test_a_day_column_stops_at_a_midnight_end(self) -> None:
        first = date(2026, 5, 20)
        evening = _row(
            "evening",
            datetime.combine(first, time(hour=21)).astimezone(),
            datetime.combine(first + timedelta(days=1), time.min).astimezone(),
        )

        columns = day_columns([evening], first, 3)

        self.assertEqual([len(rows) for _day, rows in columns], [1, 0, 0])

    def test_minutes_on_a_day_are_clipped_to_that_day(self) -> None:
        first = date(2026, 5, 20)
        trip = _row(
            "trip",
            datetime.combine(first, time(hour=9, minute=45)).astimezone(),
            datetime.combine(first + timedelta(days=2), time(hour=17)).astimezone(),
        )

        self.assertEqual(
            timed_day_minutes(trip.occurrence, first),
            (9 * 60 + 45, 24 * 60),
        )
        self.assertEqual(
            timed_day_minutes(trip.occurrence, first + timedelta(days=1)),
            (0, 24 * 60),
        )
        self.assertEqual(
            timed_day_minutes(trip.occurrence, first + timedelta(days=2)),
            (0, 17 * 60),
        )
        self.assertIsNone(
            timed_day_minutes(trip.occurrence, first + timedelta(days=3)),
        )

    def test_the_banner_lists_only_the_full_day_items_covering_the_day(self) -> None:
        day = date(2026, 5, 20)
        spanning = _all_day(day - timedelta(days=1), days=3, uid="spanning")
        elsewhere = _all_day(day + timedelta(days=5), uid="elsewhere")
        timed = _timed(day, 10)

        banner = full_day_rows(day, [spanning, elsewhere, timed])

        self.assertEqual(
            [row.component.ref.uid for row in banner],
            ["spanning"],
        )


if __name__ == "__main__":
    unittest.main()


class OneDayLabelTest(unittest.TestCase):
    """Three columns label a day, and they used to decide separately.

    The timeline heading, the agenda's Day column and
    `format_friendly_start` each answered "which days have a name
    instead of a date" for itself, and wrote the date three different
    ways when the answer was none: `%a %d %b`, `%d %b %a` and
    `%a %d %b %Y`, three field orders for the same date in one
    application.
    """

    TODAY = date(2026, 5, 20)  # Wednesday

    def _moment(self, day: date) -> datetime:
        return datetime.combine(day, time(9, 0)).astimezone()

    def test_the_three_labels_name_the_same_three_days(self) -> None:
        from pony.calendar.views import _format_event_day

        for offset, name in ((0, "Today"), (1, "Tomorrow"), (-1, "Yesterday")):
            day = self.TODAY + timedelta(days=offset)
            with self.subTest(name=name):
                self.assertEqual(relative_day_name(day, self.TODAY), name)
                self.assertTrue(day_column_label(day, self.TODAY).startswith(name))
                self.assertEqual(_format_event_day(self._moment(day), self.TODAY), name)
                self.assertTrue(
                    format_friendly_start(self._moment(day), self.TODAY).startswith(
                        name
                    )
                )

    def test_a_day_with_no_name_is_written_one_way(self) -> None:
        from pony.calendar.views import _format_event_day

        day = date(2026, 8, 12)  # Wednesday, a season away
        self.assertIsNone(relative_day_name(day, self.TODAY))
        self.assertEqual(day_column_label(day, self.TODAY), "Wed 12 Aug")
        self.assertEqual(_format_event_day(self._moment(day), self.TODAY), "Wed 12 Aug")
        self.assertEqual(
            format_friendly_start(self._moment(day), self.TODAY), "Wed 12 Aug 09:00"
        )

    def test_a_day_in_another_year_carries_it(self) -> None:
        day = date(2014, 9, 30)  # Tuesday
        self.assertEqual(
            format_friendly_start(self._moment(day), self.TODAY),
            "Tue 30 Sep 2014 09:00",
        )
