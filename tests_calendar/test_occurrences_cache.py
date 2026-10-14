from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pony.calendar.domain import (
    CalendarRef,
    ComponentRef,
    LocalStatus,
    Occurrence,
    VEvent,
    VTodo,
)
from pony.calendar.index_store import SqliteIndexRepository
from pony.calendar.recurrence import populate_occurrences
from pony.calendar.storage import VdirMirrorRepository
from pony.calendar.storage_indexing import index_calendar
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
        from pony.calendar.domain import ResourceRef

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


class RenderCostTest(unittest.TestCase):
    """What a view render is allowed to read out of the index.

    `gather_occurrences` joined the window's occurrences against
    `list_calendar_components` -- every row in the calendar, raw ICS
    included -- and `gather_todos` read the same thing to pick out the
    tasks.  A one-day render of a 3,000-event calendar cost 44 ms, and
    the mail reader's status line pays it too (`next_event_status`).
    """

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.window_start = datetime(2026, 5, 1, tzinfo=UTC)
        self.window_end = datetime(2026, 6, 1, tzinfo=UTC)
        for day in range(1, 21):
            uid = f"render-cost-{day}@example.com"
            start = datetime(2026, 5, day, 9, 0, tzinfo=UTC)
            self.index.upsert_component(
                _event(
                    uid,
                    corpus.simple_event(),
                    dtstart=start,
                    dtend=start.replace(hour=10),
                    summary=uid,
                )
            )
        for day in (2, 3):
            uid = f"render-cost-todo-{day}@example.com"
            self.index.upsert_component(
                VTodo(
                    ref=_ref(uid),
                    href="/dav/t.ics",
                    etag="v1",
                    raw_ics=corpus.simple_todo(),
                    summary=uid,
                    description=None,
                    location=None,
                    dtstart=None,
                    due=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
                    status=None,
                    local_flags=frozenset(),
                    server_flags=frozenset(),
                    local_status=LocalStatus.ACTIVE,
                    trashed_at=None,
                    synced_at=None,
                )
            )
        populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=self.window_start,
            window_end=self.window_end,
        )

    def _count_component_reads(self) -> list[int]:
        """Record how many component rows each read returns."""
        seen: list[int] = []
        real_all = self.index.list_calendar_components
        real_keyed = self.index.list_components_for_uids

        def _all(*args: object, **kwargs: object) -> object:
            rows = real_all(*args, **kwargs)  # type: ignore[arg-type]
            seen.append(len(rows))
            return rows

        def _keyed(*args: object, **kwargs: object) -> object:
            rows = real_keyed(*args, **kwargs)  # type: ignore[arg-type]
            seen.append(len(rows))
            return rows

        self.index.list_calendar_components = _all  # type: ignore[method-assign]
        self.index.list_components_for_uids = _keyed  # type: ignore[method-assign]
        self.addCleanup(setattr, self.index, "list_calendar_components", real_all)
        self.addCleanup(setattr, self.index, "list_components_for_uids", real_keyed)
        return seen

    def test_one_day_render_reads_one_component(self) -> None:
        from pony.calendar.views import CalendarSelection, gather_occurrences

        seen = self._count_component_reads()
        rows = gather_occurrences(
            index=self.index,
            calendars=[CAL],
            selection=CalendarSelection(refs=frozenset()),
            window=(
                datetime(2026, 5, 7, tzinfo=UTC),
                datetime(2026, 5, 8, tzinfo=UTC),
            ),
        )
        self.assertEqual(
            [r.component.ref.uid for r in rows], ["render-cost-7@example.com"]
        )
        # Twenty events in the calendar; the window names one.
        self.assertEqual(sum(seen), 1, f"component rows read: {seen}")

    def test_a_render_still_injects_the_window_s_todos(self) -> None:
        """The same rows as before the narrow queries, duplicate included.

        A VTODO with an anchor in the window arrives twice: once from
        the occurrence cache, which `populate_occurrences` fills for
        every master including tasks, and once from the full-day
        injection.  That is pre-existing -- `gather_occurrences`
        double-counted it when it read the whole calendar too -- and
        this test pins it so the narrow queries can be shown not to
        have changed what a render shows.  The duplicate itself is a
        separate bug.
        """
        from pony.calendar.views import CalendarSelection, gather_occurrences

        rows = gather_occurrences(
            index=self.index,
            calendars=[CAL],
            selection=CalendarSelection(refs=frozenset()),
            window=(
                datetime(2026, 5, 2, tzinfo=UTC),
                datetime(2026, 5, 3, tzinfo=UTC),
            ),
        )
        self.assertEqual(
            sorted(r.component.ref.uid for r in rows),
            [
                "render-cost-2@example.com",
                "render-cost-todo-2@example.com",
                "render-cost-todo-2@example.com",
            ],
        )

    def test_listing_todos_does_not_read_the_events(self) -> None:
        from pony.calendar.views import CalendarSelection, gather_todos

        seen = self._count_component_reads()
        todos = gather_todos(
            index=self.index,
            calendars=[CAL],
            selection=CalendarSelection(refs=frozenset()),
        )
        self.assertEqual(len(todos), 2)
        self.assertEqual(sum(seen), 0, f"component rows read: {seen}")
