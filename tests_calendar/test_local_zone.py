"""The local zone is the one in force at the moment being read.

A zone is not an offset. Madrid is +01:00 in January and +02:00 in
July, and `local_tzinfo` used to answer with whichever was in force
*now* -- so every reading of a date on the other side of a transition
was an hour out, and three user-visible things went with it: a floating
event was anchored at the wrong offset, an all-day event at local
midnight stopped looking all-day, and the date written for a
``VALUE=DATE`` moved a day.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pony.calendar.ical_parser import local_tzinfo, localize_naive
from pony.calendar.mutations import all_day_date, is_all_day_span
from tests_calendar.zones import local_zone, requires_tzset

#: A zone that moves, and whose two offsets differ by an hour.
MOVING = "Europe/Madrid"
WINTER = datetime(2026, 1, 10, 9, 0)
SUMMER = datetime(2026, 7, 10, 9, 0)


@requires_tzset
class LocalZoneTest(unittest.TestCase):
    def test_the_offset_is_the_one_in_force_then(self) -> None:
        with local_zone(MOVING):
            self.assertEqual(local_tzinfo(WINTER).utcoffset(WINTER), timedelta(hours=1))
            self.assertEqual(local_tzinfo(SUMMER).utcoffset(SUMMER), timedelta(hours=2))

    def test_a_floating_time_keeps_its_wall_clock_either_side(self) -> None:
        """What a DTSTART with neither TZID nor Z means: local wall time."""
        with local_zone(MOVING):
            winter, summer = localize_naive(WINTER), localize_naive(SUMMER)

        self.assertEqual(winter.strftime("%H:%M %z"), "09:00 +0100")
        self.assertEqual(summer.strftime("%H:%M %z"), "09:00 +0200")

    def test_an_all_day_event_at_local_midnight_is_all_day_either_side(self) -> None:
        """How Outlook, Apple Calendar and Thunderbird write an all-day event.

        Read in summer, a January one stopped being all-day: the grids
        drew it as a timed event and `calendar edit` rewrote it timed.
        """
        with local_zone(MOVING):
            for label, day in (("winter", WINTER), ("summer", SUMMER)):
                start = day.replace(hour=0, minute=0).astimezone()
                with self.subTest(season=label):
                    self.assertTrue(is_all_day_span(start, start + timedelta(days=1)))

    def test_the_date_written_for_a_value_date_is_the_day_it_covers(self) -> None:
        """Taking the UTC date of a local-midnight span moved the event."""
        with local_zone(MOVING):
            for label, day in (("winter", WINTER), ("summer", SUMMER)):
                start = day.replace(hour=0, minute=0).astimezone()
                with self.subTest(season=label):
                    self.assertEqual(all_day_date(start), start.date())

    def test_utc_midnight_is_unaffected(self) -> None:
        """`VALUE=DATE` parses to UTC midnight, and that reading stands."""
        start = datetime(2026, 1, 10, tzinfo=UTC)
        with local_zone(MOVING):
            self.assertTrue(is_all_day_span(start, start + timedelta(days=1)))
