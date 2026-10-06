from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from chronos.domain import (
    CalendarRef,
    ComponentRef,
    LocalStatus,
    Occurrence,
    VEvent,
)
from chronos.index_store import SqliteIndexRepository
from chronos.recurrence import populate_occurrences
from chronos.storage import VdirMirrorRepository
from chronos.storage_indexing import index_calendar
from tests_calendar import corpus

ACCOUNT = "personal"
CALENDAR = "work"
CAL = CalendarRef(ACCOUNT, CALENDAR)


def _ref(uid: str, recurrence_id: str | None = None) -> ComponentRef:
    return ComponentRef(
        account_name=ACCOUNT,
        calendar_name=CALENDAR,
        uid=uid,
        recurrence_id=recurrence_id,
    )


def _event(
    uid: str,
    raw_ics: bytes,
    *,
    dtstart: datetime,
    dtend: datetime,
    href: str | None = "/dav/x.ics",
    etag: str | None = "v1",
    recurrence_id: str | None = None,
    summary: str | None = None,
) -> VEvent:
    return VEvent(
        ref=_ref(uid, recurrence_id),
        href=href,
        etag=etag,
        raw_ics=raw_ics,
        summary=summary,
        description=None,
        location=None,
        dtstart=dtstart,
        dtend=dtend,
        status=None,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )


class SetAndQueryOccurrencesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)

    def _seed_weekly(self) -> None:
        from chronos.domain import ResourceRef

        self.mirror.write(
            ResourceRef(ACCOUNT, CALENDAR, "weekly-1@example.com"),
            corpus.recurring_weekly(),
        )
        index_calendar(mirror=self.mirror, index=self.index, calendar=CAL)

    def test_populate_writes_occurrences_into_window(self) -> None:
        self._seed_weekly()
        written = populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(written, 5)
        occs = self.index.query_occurrences(
            CAL,
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 5)
        for occ in occs:
            self.assertEqual(occ.ref.uid, "weekly-1@example.com")
            self.assertFalse(occ.is_override)

    def test_query_respects_window_bounds(self) -> None:
        self._seed_weekly()
        populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        narrow = self.index.query_occurrences(
            CAL,
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 9, tzinfo=UTC),
        )
        self.assertEqual(len(narrow), 2)  # May 1 and May 8

    def test_repopulate_replaces_previous_rows(self) -> None:
        self._seed_weekly()
        populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        first = self.index.query_occurrences(
            CAL,
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
        )
        populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 6, 1, tzinfo=UTC),
            window_end=datetime(2026, 7, 1, tzinfo=UTC),
        )
        after = self.index.query_occurrences(
            CAL,
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(after, ())
        june = self.index.query_occurrences(
            CAL,
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 7, 1, tzinfo=UTC),
        )
        self.assertGreater(len(june), 0)
        self.assertNotEqual(first, june)


class OverlappingWindowQueryTest(unittest.TestCase):
    """A query window must catch spans that merely overlap it.

    An eight-day trip has a single occurrence row anchored on its first
    day; every view whose window opens after that day was dropping it,
    so a trip from the 17th to the 25th vanished from the week starting
    on the 20th.
    """

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)

    def _store(self, uid: str, start: datetime, end: datetime | None) -> ComponentRef:
        ref = _ref(uid)
        self.index.upsert_component(_event(uid, b"", dtstart=start, dtend=end or start))
        self.index.set_occurrences(
            ref,
            [
                Occurrence(
                    ref=ref,
                    start=start,
                    end=end,
                    recurrence_id=None,
                    is_override=False,
                )
            ],
        )
        return ref

    def _uids_in(self, start: datetime, end: datetime) -> list[str]:
        return [occ.ref.uid for occ in self.index.query_occurrences(CAL, start, end)]

    def test_span_started_before_the_window_is_returned(self) -> None:
        self._store(
            "trip",
            datetime(2026, 5, 17, 9, 0, tzinfo=UTC),
            datetime(2026, 5, 25, 17, 0, tzinfo=UTC),
        )
        week_of_20th = self._uids_in(
            datetime(2026, 5, 20, tzinfo=UTC), datetime(2026, 5, 27, tzinfo=UTC)
        )
        self.assertEqual(week_of_20th, ["trip"])
        # And in the week after it ends, it is gone again.
        self.assertEqual(
            self._uids_in(
                datetime(2026, 5, 27, tzinfo=UTC), datetime(2026, 6, 3, tzinfo=UTC)
            ),
            [],
        )

    def test_window_bounds_stay_half_open(self) -> None:
        # Ends exactly when the window opens: no overlap.
        self._store(
            "ends-at-open",
            datetime(2026, 5, 19, 9, 0, tzinfo=UTC),
            datetime(2026, 5, 20, tzinfo=UTC),
        )
        # Starts exactly when the window closes: no overlap either.
        self._store(
            "starts-at-close",
            datetime(2026, 5, 27, tzinfo=UTC),
            datetime(2026, 5, 27, 10, 0, tzinfo=UTC),
        )
        self.assertEqual(
            self._uids_in(
                datetime(2026, 5, 20, tzinfo=UTC), datetime(2026, 5, 27, tzinfo=UTC)
            ),
            [],
        )

    def test_occurrence_without_an_end_is_treated_as_an_instant(self) -> None:
        self._store("no-end", datetime(2026, 5, 22, 9, 0, tzinfo=UTC), None)
        self.assertEqual(
            self._uids_in(
                datetime(2026, 5, 20, tzinfo=UTC), datetime(2026, 5, 27, tzinfo=UTC)
            ),
            ["no-end"],
        )
        self.assertEqual(
            self._uids_in(
                datetime(2026, 5, 27, tzinfo=UTC), datetime(2026, 6, 3, tzinfo=UTC)
            ),
            [],
        )

    def test_rows_still_come_back_sorted_by_start(self) -> None:
        self._store(
            "late",
            datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
            datetime(2026, 5, 22, 10, 0, tzinfo=UTC),
        )
        self._store(
            "early-long",
            datetime(2026, 5, 17, 9, 0, tzinfo=UTC),
            datetime(2026, 5, 25, 17, 0, tzinfo=UTC),
        )
        self.assertEqual(
            self._uids_in(
                datetime(2026, 5, 20, tzinfo=UTC), datetime(2026, 5, 27, tzinfo=UTC)
            ),
            ["early-long", "late"],
        )


class InvalidationOnWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.master = _event(
            "weekly-1@example.com",
            corpus.recurring_weekly(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        self.index.upsert_component(self.master)
        populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )

    def _count(self) -> int:
        return len(
            self.index.query_occurrences(
                CAL,
                datetime(2026, 5, 1, tzinfo=UTC),
                datetime(2026, 6, 1, tzinfo=UTC),
            )
        )

    def test_upserting_master_clears_occurrences(self) -> None:
        self.assertGreater(self._count(), 0)
        self.index.upsert_component(self.master)
        self.assertEqual(self._count(), 0)

    def test_upserting_override_clears_master_occurrences(self) -> None:
        self.assertGreater(self._count(), 0)
        override = _event(
            "weekly-1@example.com",
            corpus.recurring_weekly(),
            dtstart=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
            recurrence_id="2026-05-08T09:00:00+00:00",
            summary="Rescheduled",
        )
        self.index.upsert_component(override)
        self.assertEqual(self._count(), 0)

    def test_deleting_master_clears_occurrences(self) -> None:
        self.assertGreater(self._count(), 0)
        self.index.delete_component(self.master.ref)
        self.assertEqual(self._count(), 0)
