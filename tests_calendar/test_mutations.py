from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from unittest import mock

from pony.calendar.ical_parser import parse_vcalendar
from pony.calendar.mutations import all_day_bounds, build_event_ics, is_all_day_span

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


class AllDayTest(unittest.TestCase):
    def test_bounds_are_utc_midnight_with_exclusive_end(self) -> None:
        self.assertEqual(
            all_day_bounds(date(2026, 9, 25), date(2026, 9, 26)),
            (datetime(2026, 9, 25, tzinfo=UTC), datetime(2026, 9, 27, tzinfo=UTC)),
        )

    def test_is_all_day_span(self) -> None:
        start, end = all_day_bounds(date(2026, 9, 25), date(2026, 9, 25))
        self.assertTrue(is_all_day_span(start, end))
        self.assertFalse(is_all_day_span(start, None))
        self.assertFalse(
            is_all_day_span(start, datetime(2026, 9, 25, 10, 0, tzinfo=UTC))
        )
        # A day-long span that begins at no midnight anywhere: 09:45 is
        # pinned rather than a round hour because the rule also accepts
        # the reader's own midnight, and no real zone is offset by 9h45.
        self.assertFalse(
            is_all_day_span(
                datetime(2026, 9, 25, 9, 45, tzinfo=UTC),
                datetime(2026, 9, 26, 9, 45, tzinfo=UTC),
            )
        )

    def test_a_local_midnight_all_day_event_is_an_all_day_span(self) -> None:
        """One rule for the grid and for `calendar edit`.

        Outlook, Apple Calendar and Thunderbird write an all-day event
        as local midnight to local midnight, which lands off UTC
        midnight.  The grid has always drawn those as banners; this test
        used to demand both ends at UTC midnight, so an edit of the same
        event decided it was not all-day and rewrote it timed.  The
        reader's zone is pinned, because in UTC there is nothing to
        tell apart.
        """
        zone = timezone(timedelta(hours=2))
        start = datetime(2026, 9, 25, 0, 0, tzinfo=zone)
        with mock.patch("pony.calendar.ical_parser.local_tzinfo", return_value=zone):
            self.assertTrue(is_all_day_span(start, start + timedelta(days=1)))
            self.assertFalse(
                is_all_day_span(start, start + timedelta(hours=2)),
            )

    def test_all_day_ics_round_trips_through_the_parser(self) -> None:
        start, end = all_day_bounds(date(2026, 9, 25), date(2026, 9, 26))
        ics = build_event_ics("u@x", "Trip", start, end, NOW, all_day=True)
        self.assertIn(b"DTSTART;VALUE=DATE:20260925\r\n", ics)
        self.assertIn(b"DTEND;VALUE=DATE:20260927\r\n", ics)
        parsed = parse_vcalendar(ics)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].dtstart, start)
        self.assertEqual(parsed[0].dtend, end)

    def test_all_day_end_defaults_to_next_day(self) -> None:
        start, _ = all_day_bounds(date(2026, 9, 25), date(2026, 9, 25))
        ics = build_event_ics("u@x", "Day", start, None, NOW, all_day=True)
        self.assertIn(b"DTEND;VALUE=DATE:20260926\r\n", ics)


if __name__ == "__main__":
    unittest.main()
