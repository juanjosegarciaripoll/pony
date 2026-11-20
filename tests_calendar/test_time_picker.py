"""The event form's clock-time field: what it accepts, and how it steps.

The field replaced a dropdown over the forty-eight half-hour slots of a
day, which is why the half hours are still one press away -- and why a
time between two of them has to be typable: the browser's half of the
same form has always taken one.
"""

from __future__ import annotations

import unittest
from datetime import time

from pony.tui.calendar.widgets.time_picker import (
    HALF_HOUR,
    InvalidTimeError,
    parse_time_input,
    step_time,
)


class ParseTimeInputTest(unittest.TestCase):
    def test_the_spelling_the_field_writes_round_trips(self) -> None:
        self.assertEqual(parse_time_input("09:30"), time(9, 30))
        self.assertEqual(parse_time_input("00:00"), time(0, 0))
        self.assertEqual(parse_time_input("23:59"), time(23, 59))

    def test_a_minute_off_the_half_hour_grid_is_accepted(self) -> None:
        """The whole point: the dropdown could not express this."""
        self.assertEqual(parse_time_input("09:47"), time(9, 47))
        self.assertEqual(parse_time_input("14:03"), time(14, 3))

    def test_the_shorthands_a_hand_types_mean_the_obvious_thing(self) -> None:
        for typed, expected in (
            ("9:30", time(9, 30)),
            ("9.30", time(9, 30)),
            ("930", time(9, 30)),
            ("0930", time(9, 30)),
            ("9", time(9, 0)),
            ("09", time(9, 0)),
            ("0", time(0, 0)),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(parse_time_input(typed), expected)

    def test_surrounding_space_is_not_a_mistake(self) -> None:
        self.assertEqual(parse_time_input("  9:30 "), time(9, 30))

    def test_an_empty_field_says_which_field(self) -> None:
        with self.assertRaises(InvalidTimeError) as caught:
            parse_time_input("", "start time")
        self.assertIn("start time", str(caught.exception))

    def test_a_well_formed_time_that_is_no_hour_of_the_day_is_refused(self) -> None:
        for typed in ("25:00", "24:00", "12:60", "99"):
            with self.subTest(typed=typed), self.assertRaises(InvalidTimeError):
                parse_time_input(typed)

    def test_a_half_typed_minute_is_a_mistake_and_not_three_past(self) -> None:
        """``9:3`` is a hand caught mid-``9:30``, not 09:03."""
        with self.assertRaises(InvalidTimeError):
            parse_time_input("9:3")

    def test_what_is_not_a_time_at_all_is_refused(self) -> None:
        for typed in ("lunch", "9:30pm", "9:30:15", "-9", "1_0", "09:3o"):
            with self.subTest(typed=typed), self.assertRaises(InvalidTimeError):
                parse_time_input(typed)

    def test_seconds_are_never_carried(self) -> None:
        """A `time` the form builds has to compare equal to a stored one."""
        self.assertEqual(parse_time_input("09:30").second, 0)
        self.assertEqual(parse_time_input("09:30").microsecond, 0)


class StepTimeTest(unittest.TestCase):
    def test_a_step_from_the_grid_is_the_next_slot(self) -> None:
        self.assertEqual(step_time("14:00", HALF_HOUR), "14:30")
        self.assertEqual(step_time("14:30", HALF_HOUR), "15:00")
        self.assertEqual(step_time("14:30", -HALF_HOUR), "14:00")
        self.assertEqual(step_time("14:00", -HALF_HOUR), "13:30")

    def test_a_step_off_the_grid_lands_on_it(self) -> None:
        """Otherwise 14:17 would walk 14:47, 15:17 and never meet a slot."""
        self.assertEqual(step_time("14:17", HALF_HOUR), "14:30")
        self.assertEqual(step_time("14:17", -HALF_HOUR), "14:00")
        self.assertEqual(step_time("14:47", HALF_HOUR), "15:00")
        self.assertEqual(step_time("14:47", -HALF_HOUR), "14:30")

    def test_the_day_is_a_circle(self) -> None:
        self.assertEqual(step_time("23:30", HALF_HOUR), "00:00")
        self.assertEqual(step_time("00:00", -HALF_HOUR), "23:30")

    def test_an_empty_field_enters_the_grid_from_the_right_end(self) -> None:
        """What opening the old dropdown on nothing and pressing a key did."""
        self.assertEqual(step_time("", HALF_HOUR), "00:00")
        self.assertEqual(step_time("", -HALF_HOUR), "23:30")
        self.assertEqual(step_time("   ", HALF_HOUR), "00:00")

    def test_a_shorthand_is_stepped_as_what_it_means(self) -> None:
        self.assertEqual(step_time("9", HALF_HOUR), "09:30")
        self.assertEqual(step_time("930", -HALF_HOUR), "09:00")

    def test_nonsense_is_stepped_as_an_empty_field(self) -> None:
        """It cannot be stepped, and it could not have been saved either."""
        self.assertEqual(step_time("lunch", HALF_HOUR), "00:00")
        self.assertEqual(step_time("lunch", -HALF_HOUR), "23:30")

    def test_the_result_is_always_the_spelling_the_field_shows(self) -> None:
        self.assertEqual(step_time("9", HALF_HOUR), "09:30")
        self.assertEqual(step_time("0", HALF_HOUR), "00:30")
