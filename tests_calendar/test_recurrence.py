from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from chronos.domain import (
    CalendarRef,
    ComponentRef,
    LocalStatus,
    VEvent,
    VTodo,
)
from chronos.recurrence import (
    MAX_OCCURRENCES,
    RecurrenceExpansionError,
    expand,
    populate_alarms,
    populate_occurrences,
    rebuild_caches,
)
from tests_calendar import corpus

ACCOUNT = "personal"
CALENDAR = "work"


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
    dtstart: datetime | None = None,
    dtend: datetime | None = None,
    recurrence_id: str | None = None,
    summary: str | None = None,
) -> VEvent:
    return VEvent(
        ref=_ref(uid, recurrence_id),
        href=None,
        etag=None,
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


class NonRecurringExpansionTest(unittest.TestCase):
    def test_single_event_in_window_returns_one_occurrence(self) -> None:
        master = _event(
            "simple-event-1@example.com",
            corpus.simple_event(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 2, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].start, datetime(2026, 5, 1, 9, 0, tzinfo=UTC))
        self.assertEqual(occs[0].end, datetime(2026, 5, 1, 10, 0, tzinfo=UTC))
        self.assertFalse(occs[0].is_override)

    def test_event_outside_window_returns_empty(self) -> None:
        master = _event(
            "simple-event-1@example.com",
            corpus.simple_event(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 6, 1, tzinfo=UTC),
            window_end=datetime(2026, 7, 1, tzinfo=UTC),
        )
        self.assertEqual(occs, [])

    def test_empty_window_returns_empty(self) -> None:
        master = _event(
            "simple-event-1@example.com",
            corpus.simple_event(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 1, tzinfo=UTC),
        )
        self.assertEqual(occs, [])

    def test_missing_anchor_returns_empty(self) -> None:
        master = _event(
            "no-anchor@example.com",
            b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
            dtstart=None,
            dtend=None,
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(occs, [])


class AllDayExpansionTest(unittest.TestCase):
    def test_all_day_event_midnight_utc(self) -> None:
        master = _event(
            "all-day-1@example.com",
            corpus.all_day_event(),
            dtstart=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 4, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].start, datetime(2026, 5, 1, 0, 0, tzinfo=UTC))
        self.assertEqual(occs[0].end, datetime(2026, 5, 2, 0, 0, tzinfo=UTC))

    def test_an_all_day_series_expands_to_utc_midnights(self) -> None:
        """The anchor and the stored span must read VALUE=DATE alike.

        `expand` takes its rrule anchor from the raw ICS so a timed
        series keeps its wall clock through DST.  An all-day DTSTART has
        no zone to keep: the stored span reads it as UTC midnight, and
        reading the anchor as *local* midnight instead moved every
        occurrence by the reader's own offset.  The reader's zone is
        pinned rather than ambient, because a run in UTC -- which is
        what CI does -- cannot tell the two readings apart.
        """
        master = _event(
            "all-day-weekly-1@example.com",
            corpus.recurring_all_day(),
            dtstart=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
        )
        for offset in (2, -7):
            zone = timezone(timedelta(hours=offset))
            with (
                self.subTest(offset=offset),
                mock.patch("chronos.ical_parser.local_tzinfo", return_value=zone),
            ):
                occs = expand(
                    master=master,
                    window_start=datetime(2026, 4, 1, tzinfo=UTC),
                    window_end=datetime(2026, 6, 1, tzinfo=UTC),
                )
                self.assertEqual(
                    [occ.start for occ in occs],
                    [
                        datetime(2026, 5, 1, tzinfo=UTC),
                        datetime(2026, 5, 8, tzinfo=UTC),
                        datetime(2026, 5, 15, tzinfo=UTC),
                    ],
                )


class ZeroDurationTest(unittest.TestCase):
    def test_event_without_dtend_has_none_end(self) -> None:
        master = _event(
            "zero-duration-1@example.com",
            corpus.zero_duration_event(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=None,
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 2, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].start, datetime(2026, 5, 1, 9, 0, tzinfo=UTC))
        self.assertIsNone(occs[0].end)


class LocalFloatingTimeTest(unittest.TestCase):
    def test_localize_naive_old_date_does_not_convert(self) -> None:
        # Regression: Windows may raise OSError when naive ancient dates are
        # pushed through astimezone(). We attach local tzinfo directly.
        from chronos.ical_parser import localize_naive

        naive = datetime(1900, 1, 1, 9, 0)
        localized = localize_naive(naive)
        self.assertIsNotNone(localized.tzinfo)
        self.assertEqual(localized.replace(tzinfo=None), naive)

    def test_a_naive_value_reaches_utc_without_resolving_localtime(self) -> None:
        """`to_utc` reads a floating value as local wall time.

        It used to hand the naive value straight to ``astimezone``,
        which resolves it through the platform's localtime -- the call
        `localize_naive` exists to keep away from an ancient date.
        """
        from chronos.ical_parser import to_utc

        zone = timezone(timedelta(hours=2))
        with mock.patch("chronos.ical_parser.local_tzinfo", return_value=zone):
            self.assertEqual(
                to_utc(datetime(1900, 1, 1, 9, 0)),
                datetime(1900, 1, 1, 7, 0, tzinfo=UTC),
            )


class WeeklyRecurrenceTest(unittest.TestCase):
    def _master(self) -> VEvent:
        return _event(
            "weekly-1@example.com",
            corpus.recurring_weekly(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )

    def test_weekly_one_month_window(self) -> None:
        occs = expand(
            master=self._master(),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        # FREQ=WEEKLY;BYDAY=FR, anchor 2026-05-01 (Fri).
        # Fridays in May 2026: 1, 8, 15, 22, 29.
        self.assertEqual(len(occs), 5)
        starts = [o.start.day for o in occs]
        self.assertEqual(starts, [1, 8, 15, 22, 29])

    def test_infinite_rrule_bounded_by_window(self) -> None:
        # 25-month window: weekly FR event => ~108 occurrences.
        # Proves no runaway expansion on an infinite RRULE.
        occs = expand(
            master=self._master(),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2028, 6, 1, tzinfo=UTC),
        )
        self.assertGreater(len(occs), 100)
        self.assertLess(len(occs), MAX_OCCURRENCES)

    def test_all_durations_equal_master_duration(self) -> None:
        occs = expand(
            master=self._master(),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        for occ in occs:
            assert occ.end is not None
            self.assertEqual(occ.end - occ.start, timedelta(hours=1))


class CountBoundedRrule(unittest.TestCase):
    def test_count_limits_to_five(self) -> None:
        master = _event(
            "count-1@example.com",
            corpus.recurring_count(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2027, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 5)


class UntilBoundedRrule(unittest.TestCase):
    def test_until_stops_series(self) -> None:
        master = _event(
            "until-1@example.com",
            corpus.recurring_until(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        # UNTIL=20260626T090000Z => last occurrence on June 26.
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2027, 1, 1, tzinfo=UTC),
        )
        self.assertTrue(occs)
        last = occs[-1]
        self.assertEqual(last.start, datetime(2026, 6, 26, 9, 0, tzinfo=UTC))

    def test_naive_until_with_tzid_dtstart_is_normalised(self) -> None:
        # Regression: RFC 5545 says UNTIL must be UTC (carry `Z`) when
        # DTSTART has a TZID, but Google + legacy Outlook routinely
        # emit naive UNTIL. dateutil's rrulestr would raise
        # `ValueError: RRULE UNTIL values must be specified in UTC...`
        # mid-sync. chronos normalises the UNTIL to UTC before parsing
        # so the recurrence still expands.
        master = _event(
            "until-naive-1@example.com",
            corpus.recurring_until_naive_with_tzid(),
            # DTSTART;TZID=Europe/Madrid:20260501T110000 = 09:00 UTC.
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2027, 1, 1, tzinfo=UTC),
        )
        self.assertTrue(occs, "RRULE with naive UNTIL should still expand")
        # UNTIL=20260626T090000 (treated as UTC) => Friday 26 June at
        # 09:00 UTC is the last weekday occurrence inside the bound.
        last = occs[-1]
        self.assertEqual(last.start, datetime(2026, 6, 26, 9, 0, tzinfo=UTC))


class MalformedRruleTest(unittest.TestCase):
    def test_unparseable_rrule_raises_recurrence_error(self) -> None:
        # If normalisation can't help (e.g. a syntactically broken
        # RRULE), `expand` must funnel the failure through
        # `RecurrenceExpansionError` so `populate_occurrences` can
        # skip just this master instead of failing the whole sync.
        from chronos.recurrence import RecurrenceExpansionError

        broken_ics = corpus.recurring_until().replace(
            b"RRULE:FREQ=WEEKLY;BYDAY=FR;UNTIL=20260626T090000Z\r\n",
            b"RRULE:FREQ=NONSENSE\r\n",
        )
        master = _event(
            "until-1@example.com",
            broken_ics,
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        with self.assertRaises(RecurrenceExpansionError):
            expand(
                master=master,
                window_start=datetime(2026, 5, 1, tzinfo=UTC),
                window_end=datetime(2027, 1, 1, tzinfo=UTC),
            )


class ExdateOverrideTest(unittest.TestCase):
    def _master(self) -> VEvent:
        return _event(
            "with-exceptions-1@example.com",
            corpus.recurring_with_exceptions(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )

    def _override(self) -> VEvent:
        # Override for 2026-05-08 09:00 UTC, rescheduled to 10:00.
        return _event(
            "with-exceptions-1@example.com",
            corpus.recurring_with_exceptions(),
            dtstart=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
            recurrence_id="2026-05-08T09:00:00+00:00",
            summary="Weekly meeting (rescheduled)",
        )

    def test_exdate_skips_occurrence(self) -> None:
        occs = expand(
            master=self._master(),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 31, tzinfo=UTC),
        )
        starts = [o.start for o in occs]
        self.assertNotIn(datetime(2026, 5, 15, 9, 0, tzinfo=UTC), starts)

    def test_override_replaces_occurrence(self) -> None:
        occs = expand(
            master=self._master(),
            overrides=(self._override(),),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 31, tzinfo=UTC),
        )
        overrides = [o for o in occs if o.is_override]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].start, datetime(2026, 5, 8, 10, 0, tzinfo=UTC))
        self.assertEqual(overrides[0].end, datetime(2026, 5, 8, 11, 0, tzinfo=UTC))


class MaxOccurrencesTest(unittest.TestCase):
    def test_exceeds_cap_raises(self) -> None:
        master = _event(
            "weekly-1@example.com",
            corpus.recurring_weekly(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        with self.assertRaises(RecurrenceExpansionError):
            expand(
                master=master,
                window_start=datetime(2026, 5, 1, tzinfo=UTC),
                window_end=datetime(2026, 8, 1, tzinfo=UTC),
                max_occurrences=3,
            )


class VtodoExpansionTest(unittest.TestCase):
    def test_vtodo_with_due_as_anchor(self) -> None:
        due = datetime(2026, 5, 5, 17, 0, tzinfo=UTC)
        todo = VTodo(
            ref=_ref("todo-1@example.com"),
            href=None,
            etag=None,
            raw_ics=corpus.simple_todo(),
            summary="x",
            description=None,
            location=None,
            dtstart=None,
            due=due,
            status="NEEDS-ACTION",
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        occs = expand(
            master=todo,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].start, due)


class PopulateOccurrencesTest(unittest.TestCase):
    """Tests for `populate_occurrences` focusing on the `uids` filter
    and `cancel_event` early-exit. Requires a real SQLite index."""

    def setUp(self) -> None:
        from chronos.index_store import SqliteIndexRepository

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.calendar = CalendarRef(account_name=ACCOUNT, calendar_name=CALENDAR)
        self.window_start = datetime(2026, 1, 1, tzinfo=UTC)
        self.window_end = datetime(2027, 1, 1, tzinfo=UTC)

    def _insert(self, uid: str, raw_ics: bytes, dtstart: datetime) -> None:
        from chronos.domain import LocalStatus

        self.index.upsert_component(
            _event(uid, raw_ics, dtstart=dtstart, dtend=dtstart + (dtstart - dtstart))
        )
        # Re-insert with a proper dtend so the occurrence window lands.
        from chronos.domain import ComponentRef, VEvent

        ref = ComponentRef(account_name=ACCOUNT, calendar_name=CALENDAR, uid=uid)
        ev = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=raw_ics,
            summary=uid,
            description=None,
            location=None,
            dtstart=dtstart,
            dtend=dtstart + __import__("datetime").timedelta(hours=1),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        self.index.upsert_component(ev)

    def test_uids_filter_expands_only_specified_masters(self) -> None:
        # Insert three non-recurring masters; only expand uid-a.
        dt = datetime(2026, 5, 1, 9, tzinfo=UTC)
        for uid in ("uid-a", "uid-b", "uid-c"):
            self._insert(uid, corpus.simple_event(), dt)

        count = populate_occurrences(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
            uids=frozenset({"uid-a"}),
        )
        # One occurrence written (uid-a only).
        self.assertEqual(count, 1)
        occs = self.index.query_occurrences(
            self.calendar, self.window_start, self.window_end
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].ref.uid, "uid-a")

    def test_uids_empty_set_expands_nothing(self) -> None:
        dt = datetime(2026, 5, 1, 9, tzinfo=UTC)
        self._insert("uid-x", corpus.simple_event(), dt)
        count = populate_occurrences(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
            uids=frozenset(),
        )
        self.assertEqual(count, 0)
        occs = self.index.query_occurrences(
            self.calendar, self.window_start, self.window_end
        )
        self.assertEqual(len(occs), 0)

    def test_uids_none_expands_all(self) -> None:
        dt = datetime(2026, 5, 1, 9, tzinfo=UTC)
        for uid in ("uid-1", "uid-2"):
            self._insert(uid, corpus.simple_event(), dt)
        count = populate_occurrences(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
            uids=None,
        )
        self.assertEqual(count, 2)

    def test_cancel_event_stops_expansion_early(self) -> None:
        dt = datetime(2026, 5, 1, 9, tzinfo=UTC)
        for uid in ("uid-p", "uid-q", "uid-r"):
            self._insert(uid, corpus.simple_event(), dt)
        cancel = threading.Event()
        expanded: list[str] = []
        original_set = self.index.set_occurrences

        def set_and_cancel(ref, occs):  # type: ignore[no-untyped-def]
            expanded.append(ref.uid)
            original_set(ref, occs)
            cancel.set()

        self.index.set_occurrences = set_and_cancel  # type: ignore[method-assign]
        populate_occurrences(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
            cancel_event=cancel,
        )
        # Only one master expanded before the loop noticed the cancel.
        self.assertEqual(len(expanded), 1)


def _alarm_ics(uid: str) -> bytes:
    """A VEVENT with one DISPLAY alarm, under the UID the caller names."""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        "DTSTAMP:20260422T120000Z\r\n"
        "DTSTART:20260501T090000Z\r\n"
        "DTEND:20260501T100000Z\r\n"
        "SUMMARY:Alarm event\r\n"
        "BEGIN:VALARM\r\n"
        "ACTION:DISPLAY\r\n"
        "TRIGGER:-PT15M\r\n"
        "DESCRIPTION:Soon\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode()


class PopulateAlarmsTest(unittest.TestCase):
    """Tests for ``populate_alarms``; requires a real SQLite index."""

    def setUp(self) -> None:
        from chronos.index_store import SqliteIndexRepository

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.calendar = CalendarRef(account_name=ACCOUNT, calendar_name=CALENDAR)
        self.window_start = datetime(2026, 1, 1, tzinfo=UTC)
        self.window_end = datetime(2027, 1, 1, tzinfo=UTC)

    def _insert_and_expand(self, uid: str, raw_ics: bytes, dtstart: datetime) -> None:
        """Insert a component, expand occurrences, ready for alarm population."""
        ref = ComponentRef(account_name=ACCOUNT, calendar_name=CALENDAR, uid=uid)
        ev = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=raw_ics,
            summary=uid,
            description=None,
            location=None,
            dtstart=dtstart,
            dtend=dtstart + timedelta(hours=1),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        self.index.upsert_component(ev)
        populate_occurrences(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
        )

    def test_display_alarm_written(self) -> None:
        uid = "alarm-display-1@example.com"
        dtstart = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        self._insert_and_expand(uid, corpus.event_with_display_alarm(-15), dtstart)
        count = populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
        )
        self.assertEqual(count, 1)
        # trigger_at = dtstart + (-15 min) = 08:45
        expected_trigger = dtstart + timedelta(minutes=-15)
        pending = self.index.query_pending_alarms(
            expected_trigger - timedelta(seconds=1),
            expected_trigger + timedelta(seconds=1),
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].trigger_at, expected_trigger)
        self.assertEqual(pending[0].summary, uid)

    def test_the_occurrence_window_is_read_once_for_every_master(self) -> None:
        """Per-master queries made a big calendar quadratic and silent."""
        for n in range(6):
            uid = f"alarm-many-{n}@example.com"
            self._insert_and_expand(
                uid,
                _alarm_ics(uid),
                datetime(2026, 5, 1 + n, 9, 0, tzinfo=UTC),
            )
        queries = 0
        real = self.index.query_occurrences

        def _counting(*args: object, **kwargs: object) -> object:
            nonlocal queries
            queries += 1
            return real(*args, **kwargs)  # type: ignore[arg-type]

        self.index.query_occurrences = _counting  # type: ignore[method-assign]

        count = populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
        )

        self.index.query_occurrences = real  # type: ignore[method-assign]
        # One alarm per master, and one query for all of them.
        self.assertEqual(count, 6)
        self.assertEqual(queries, 1)

    def test_restricting_the_masters_restricts_the_occurrence_query(self) -> None:
        """The fix for the stall reached the loop and not the query.

        `uids` narrows `masters` in Python while the occurrence read
        above it stayed unconditional, so rebuilding the alarms of one
        edited event still built every occurrence row in the window --
        thirteen thousand of them to use a handful.  Nothing in the
        output differs, which is why this watches the call.
        """
        for n in range(4):
            uid = f"alarm-scope-{n}@example.com"
            self._insert_and_expand(
                uid,
                _alarm_ics(uid),
                datetime(2026, 5, 1 + n, 9, 0, tzinfo=UTC),
            )
        wanted = frozenset({"alarm-scope-1@example.com"})
        seen: list[object] = []
        real = self.index.query_occurrences

        def _watching(*args: object, **kwargs: object) -> object:
            seen.append(kwargs.get("uids"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        self.index.query_occurrences = _watching  # type: ignore[method-assign]
        self.addCleanup(setattr, self.index, "query_occurrences", real)

        count = populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
            uids=wanted,
        )

        self.assertEqual(1, count)
        self.assertEqual([wanted], seen)

    def test_email_alarm_not_stored(self) -> None:
        uid = "alarm-email-1@example.com"
        self._insert_and_expand(
            uid, corpus.event_with_email_alarm(), datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        )
        count = populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
        )
        self.assertEqual(count, 0)

    def test_end_related_alarm_uses_dtend_as_anchor(self) -> None:
        uid = "alarm-end-1@example.com"
        dtstart = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        dtend = dtstart + timedelta(hours=1)
        self._insert_and_expand(uid, corpus.event_with_end_related_alarm(), dtstart)
        populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
        )
        # trigger = DTEND + (-5 min) = 09:55
        expected_trigger = dtend + timedelta(minutes=-5)
        pending = self.index.query_pending_alarms(
            expected_trigger - timedelta(seconds=1),
            expected_trigger + timedelta(seconds=1),
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].trigger_at, expected_trigger)

    def test_idempotent_on_second_call(self) -> None:
        uid = "alarm-display-1@example.com"
        self._insert_and_expand(
            uid,
            corpus.event_with_display_alarm(),
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
        populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
        )
        count2 = populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
        )
        # Second call replaces the existing rows — total written equals 1.
        self.assertEqual(count2, 1)
        pending = self.index.query_pending_alarms(self.window_start, self.window_end)
        self.assertEqual(len(pending), 1)

    def test_uids_filter_skips_other_masters(self) -> None:
        uid_a = "alarm-display-1@example.com"
        uid_b = "alarm-email-1@example.com"
        dtstart = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        self._insert_and_expand(uid_a, corpus.event_with_display_alarm(), dtstart)
        self._insert_and_expand(uid_b, corpus.event_with_email_alarm(), dtstart)
        count = populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
            uids=frozenset({uid_a}),
        )
        self.assertEqual(count, 1)

    def test_empty_uids_set_is_noop(self) -> None:
        self._insert_and_expand(
            "alarm-display-1@example.com",
            corpus.event_with_display_alarm(),
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
        count = populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
            uids=frozenset(),
        )
        self.assertEqual(count, 0)

    def test_cancel_event_stops_loop(self) -> None:
        for uid in ("alarm-display-1@example.com", "alarm-audio-1@example.com"):
            self._insert_and_expand(
                uid,
                corpus.event_with_display_alarm(),
                datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            )
        cancel = threading.Event()
        cancel.set()
        count = populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.window_start,
            window_end=self.window_end,
            cancel_event=cancel,
        )
        self.assertEqual(count, 0)


class RestrictedRebuildCostTest(unittest.TestCase):
    """A rebuild of one event must not read the whole calendar.

    `rebuild_caches` runs on every local create, edit and delete, and
    both of its passes used to read every component row in the calendar
    -- raw ICS included -- and then drop all but the one UID they were
    given.  The alarm pass read the whole occurrence window the same
    way.  On a 3,000-event calendar that was 95 ms of SQLite per edit,
    growing with the calendar.
    """

    def setUp(self) -> None:
        from chronos.index_store import SqliteIndexRepository

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.calendar = CalendarRef(account_name=ACCOUNT, calendar_name=CALENDAR)
        self.now = datetime(2026, 5, 1, tzinfo=UTC)
        for n in range(6):
            uid = f"rebuild-cost-{n}@example.com"
            self.index.upsert_component(
                _event(
                    uid,
                    _alarm_ics(uid),
                    dtstart=datetime(2026, 5, 1 + n, 9, 0, tzinfo=UTC),
                    dtend=datetime(2026, 5, 1 + n, 10, 0, tzinfo=UTC),
                    summary=uid,
                )
            )
        rebuild_caches(
            index=self.index, calendar=self.calendar, now=self.now, uids=None
        )

    def test_one_uid_reads_one_component_and_its_occurrences(self) -> None:
        whole_calendar_rows = 0
        keyed_rows = 0
        occurrence_rows = 0
        real_all = self.index.list_calendar_components
        real_keyed = self.index.list_components_for_uids
        real_occ = self.index.query_occurrences

        def _all(*args: object, **kwargs: object) -> object:
            nonlocal whole_calendar_rows
            rows = real_all(*args, **kwargs)  # type: ignore[arg-type]
            whole_calendar_rows += len(rows)
            return rows

        def _keyed(*args: object, **kwargs: object) -> object:
            nonlocal keyed_rows
            rows = real_keyed(*args, **kwargs)  # type: ignore[arg-type]
            keyed_rows += len(rows)
            return rows

        def _occ(*args: object, **kwargs: object) -> object:
            nonlocal occurrence_rows
            rows = real_occ(*args, **kwargs)  # type: ignore[arg-type]
            occurrence_rows += len(rows)
            return rows

        self.index.list_calendar_components = _all  # type: ignore[method-assign]
        self.index.list_components_for_uids = _keyed  # type: ignore[method-assign]
        self.index.query_occurrences = _occ  # type: ignore[method-assign]
        try:
            rebuild_caches(
                index=self.index,
                calendar=self.calendar,
                now=self.now,
                uids=frozenset({"rebuild-cost-2@example.com"}),
            )
        finally:
            self.index.list_calendar_components = real_all  # type: ignore[method-assign]
            self.index.list_components_for_uids = real_keyed  # type: ignore[method-assign]
            self.index.query_occurrences = real_occ  # type: ignore[method-assign]

        # Six masters in the calendar; the rebuild named one of them.
        self.assertEqual(whole_calendar_rows, 0, "read the whole calendar")
        self.assertEqual(keyed_rows, 2, "one component, read once per pass")
        self.assertEqual(occurrence_rows, 1, "read the whole occurrence window")

    def test_restricted_rebuild_still_writes_the_alarm(self) -> None:
        self.index.set_alarms(
            ComponentRef(ACCOUNT, CALENDAR, "rebuild-cost-2@example.com"),
            datetime(2026, 5, 3, 9, 0, tzinfo=UTC),
            [],
        )
        count = populate_alarms(
            index=self.index,
            calendar=self.calendar,
            window_start=self.now - timedelta(days=1),
            window_end=self.now + timedelta(days=30),
            uids=frozenset({"rebuild-cost-2@example.com"}),
        )
        self.assertEqual(count, 1)
