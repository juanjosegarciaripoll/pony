"""The one upload rule both sync engines ask."""

from __future__ import annotations

import unittest

from pony.sync_guards import (
    MASS_UPLOAD_ABSOLUTE,
    MASS_UPLOAD_MIN,
    MASS_UPLOAD_RATIO,
    holds_mass_upload,
)


class HoldsMassUploadTest(unittest.TestCase):
    """Three numbers and one predicate, for the mail half and the calendar.

    They were written out twice, identically, under two sets of names,
    while `ai/calendar/SYNCHRONIZATION.md` said the numbers did not line
    up across the halves.
    """

    def test_a_handful_goes_out(self) -> None:
        """Below the floor nothing is held, whatever the share."""
        for count in range(MASS_UPLOAD_MIN):
            self.assertFalse(holds_mass_upload(push_count=count, known=0))
            self.assertFalse(holds_mass_upload(push_count=count, known=1))

    def test_the_floor_is_not_itself_exempt(self) -> None:
        """At exactly the floor the ratio is consulted, not skipped."""
        self.assertTrue(
            holds_mass_upload(push_count=MASS_UPLOAD_MIN, known=MASS_UPLOAD_MIN - 1)
        )

    def test_half_a_collection_is_the_line_and_not_over_it(self) -> None:
        """A morning's work on a small collection still goes out."""
        self.assertFalse(holds_mass_upload(push_count=5, known=10))
        self.assertTrue(holds_mass_upload(push_count=6, known=10))

    def test_the_ratio_is_a_half(self) -> None:
        self.assertEqual(MASS_UPLOAD_RATIO, 0.50)

    def test_nothing_known_falls_to_the_absolute_ceiling(self) -> None:
        """A ratio cannot fire when no row remembers its handle.

        Which is the case this guard exists for, so the ceiling has to
        carry it alone -- and a collection the server holds nothing in
        can still be written offline.
        """
        self.assertFalse(
            holds_mass_upload(push_count=MASS_UPLOAD_ABSOLUTE - 1, known=0)
        )
        self.assertTrue(holds_mass_upload(push_count=MASS_UPLOAD_ABSOLUTE, known=0))

    def test_the_ceiling_fires_whatever_the_share(self) -> None:
        self.assertTrue(
            holds_mass_upload(push_count=MASS_UPLOAD_ABSOLUTE, known=10_000)
        )
