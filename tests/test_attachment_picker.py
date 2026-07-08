"""Tests for ``parse_attachment_selection`` in ``attachment_picker_screen``."""

from __future__ import annotations

import unittest

from pony.tui.screens.attachment_picker_screen import parse_attachment_selection


class ParseAttachmentSelectionTest(unittest.TestCase):
    """Input grammar for the O / S attachment pickers."""

    def test_star_expands_to_full_range(self) -> None:
        self.assertEqual(
            parse_attachment_selection("*", total=3),
            [1, 2, 3],
        )

    def test_single_index(self) -> None:
        self.assertEqual(parse_attachment_selection("2", total=3), [2])

    def test_comma_list_preserves_order(self) -> None:
        self.assertEqual(
            parse_attachment_selection("3,1,2", total=3),
            [3, 1, 2],
        )

    def test_whitespace_is_tolerated(self) -> None:
        self.assertEqual(
            parse_attachment_selection(" 1 , 3 ", total=3),
            [1, 3],
        )

    def test_empty_input_returns_none(self) -> None:
        self.assertIsNone(parse_attachment_selection("", total=3))
        self.assertIsNone(parse_attachment_selection("   ", total=3))

    def test_non_numeric_returns_none(self) -> None:
        self.assertIsNone(parse_attachment_selection("a,b", total=3))
        self.assertIsNone(parse_attachment_selection("1,foo", total=3))

    def test_out_of_range_low_returns_none(self) -> None:
        self.assertIsNone(parse_attachment_selection("0", total=3))
        self.assertIsNone(parse_attachment_selection("-1", total=3))

    def test_out_of_range_high_returns_none(self) -> None:
        self.assertIsNone(parse_attachment_selection("4", total=3))
        self.assertIsNone(parse_attachment_selection("1,99", total=3))

    def test_duplicates_rejected(self) -> None:
        self.assertIsNone(parse_attachment_selection("1,1", total=3))

    def test_star_with_zero_total(self) -> None:
        self.assertEqual(parse_attachment_selection("*", total=0), [])

    def test_trailing_comma_tolerated(self) -> None:
        self.assertEqual(parse_attachment_selection("1,", total=3), [1])

    def test_range_expands_inclusive(self) -> None:
        self.assertEqual(
            parse_attachment_selection("1-3", total=3),
            [1, 2, 3],
        )

    def test_single_element_range(self) -> None:
        self.assertEqual(parse_attachment_selection("2-2", total=3), [2])

    def test_range_mixed_with_singles(self) -> None:
        self.assertEqual(
            parse_attachment_selection("1-2, 4", total=5),
            [1, 2, 4],
        )

    def test_reversed_range_returns_none(self) -> None:
        self.assertIsNone(parse_attachment_selection("3-1", total=3))

    def test_range_out_of_bounds_returns_none(self) -> None:
        self.assertIsNone(parse_attachment_selection("1-4", total=3))
        self.assertIsNone(parse_attachment_selection("0-2", total=3))

    def test_range_overlap_duplicates_rejected(self) -> None:
        self.assertIsNone(parse_attachment_selection("1-3, 2", total=3))

    def test_malformed_range_returns_none(self) -> None:
        self.assertIsNone(parse_attachment_selection("1-", total=3))
        self.assertIsNone(parse_attachment_selection("-3", total=3))
        self.assertIsNone(parse_attachment_selection("1-2-3", total=3))


if __name__ == "__main__":
    unittest.main()
