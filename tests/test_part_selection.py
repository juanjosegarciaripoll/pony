"""Tests for ``parse_part_selection``, the grammar both ``S`` prompts take."""

from __future__ import annotations

import unittest

from pony.message_export import parse_part_selection


class ParsePartSelectionTest(unittest.TestCase):
    """Input grammar for the O / S part pickers, in both front ends."""

    def test_star_expands_to_every_part(self) -> None:
        """The message, its text, and each attachment."""
        self.assertEqual(
            parse_part_selection("*", total=3),
            [0, "T", 1, 2, 3],
        )

    def test_the_message_and_its_text_are_parts(self) -> None:
        self.assertEqual(parse_part_selection("0", total=3), [0])
        self.assertEqual(parse_part_selection("T", total=3), ["T"])
        self.assertEqual(parse_part_selection("t", total=0), ["T"])
        self.assertEqual(parse_part_selection("0,T,2", total=3), [0, "T", 2])

    def test_a_name_that_is_neither(self) -> None:
        self.assertIsNone(parse_part_selection("x", total=3))
        self.assertIsNone(parse_part_selection("T,T", total=3))

    def test_single_index(self) -> None:
        self.assertEqual(parse_part_selection("2", total=3), [2])

    def test_comma_list_preserves_order(self) -> None:
        self.assertEqual(
            parse_part_selection("3,1,2", total=3),
            [3, 1, 2],
        )

    def test_whitespace_is_tolerated(self) -> None:
        self.assertEqual(
            parse_part_selection(" 1 , 3 ", total=3),
            [1, 3],
        )

    def test_empty_input_returns_none(self) -> None:
        self.assertIsNone(parse_part_selection("", total=3))
        self.assertIsNone(parse_part_selection("   ", total=3))

    def test_non_numeric_returns_none(self) -> None:
        self.assertIsNone(parse_part_selection("a,b", total=3))
        self.assertIsNone(parse_part_selection("1,foo", total=3))

    def test_out_of_range_low_returns_none(self) -> None:
        self.assertIsNone(parse_part_selection("-1", total=3))

    def test_out_of_range_high_returns_none(self) -> None:
        self.assertIsNone(parse_part_selection("4", total=3))
        self.assertIsNone(parse_part_selection("1,99", total=3))

    def test_duplicates_rejected(self) -> None:
        self.assertIsNone(parse_part_selection("1,1", total=3))

    def test_star_on_a_message_with_no_attachments(self) -> None:
        """Still two parts: the message, and the message as text."""
        self.assertEqual(parse_part_selection("*", total=0), [0, "T"])

    def test_trailing_comma_tolerated(self) -> None:
        self.assertEqual(parse_part_selection("1,", total=3), [1])

    def test_range_expands_inclusive(self) -> None:
        self.assertEqual(
            parse_part_selection("1-3", total=3),
            [1, 2, 3],
        )

    def test_single_element_range(self) -> None:
        self.assertEqual(parse_part_selection("2-2", total=3), [2])

    def test_range_mixed_with_singles(self) -> None:
        self.assertEqual(
            parse_part_selection("1-2, 4", total=5),
            [1, 2, 4],
        )

    def test_reversed_range_returns_none(self) -> None:
        self.assertIsNone(parse_part_selection("3-1", total=3))

    def test_range_out_of_bounds_returns_none(self) -> None:
        self.assertIsNone(parse_part_selection("1-4", total=3))
        # A range may start at the message and run into the attachments.
        self.assertEqual(parse_part_selection("0-2", total=3), [0, 1, 2])

    def test_range_overlap_duplicates_rejected(self) -> None:
        self.assertIsNone(parse_part_selection("1-3, 2", total=3))

    def test_malformed_range_returns_none(self) -> None:
        self.assertIsNone(parse_part_selection("1-", total=3))
        self.assertIsNone(parse_part_selection("-3", total=3))
        self.assertIsNone(parse_part_selection("1-2-3", total=3))


if __name__ == "__main__":
    unittest.main()
