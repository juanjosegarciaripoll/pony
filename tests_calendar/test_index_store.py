from __future__ import annotations

import dataclasses
import tempfile
import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pony.calendar.domain import (
    AlarmAction,
    AlarmRecord,
    CalendarRef,
    ComponentRef,
    LocalStatus,
    SyncState,
    VEvent,
    VTodo,
)
from pony.calendar.index_store import SqliteIndexRepository


def _ref(uid: str, recurrence_id: str | None = None) -> ComponentRef:
    return ComponentRef(
        account_name="personal",
        calendar_name="work",
        uid=uid,
        recurrence_id=recurrence_id,
    )


def _event(
    ref: ComponentRef,
    *,
    href: str | None = None,
    etag: str | None = None,
    summary: str | None = "Weekly sync",
    description: str | None = None,
    location: str | None = None,
    raw_ics: bytes = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    local_status: LocalStatus = LocalStatus.ACTIVE,
    local_flags: frozenset[str] = frozenset(),
) -> VEvent:
    return VEvent(
        ref=ref,
        href=href,
        etag=etag,
        raw_ics=raw_ics,
        summary=summary,
        description=description,
        location=location,
        dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        status=None,
        local_flags=local_flags,
        server_flags=frozenset(),
        local_status=local_status,
        trashed_at=None,
        synced_at=None,
    )


def _todo(ref: ComponentRef, *, due: datetime | None = None) -> VTodo:
    return VTodo(
        ref=ref,
        href="/dav/todo.ics",
        etag="etag-1",
        raw_ics=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
        summary="File tax",
        description=None,
        location=None,
        dtstart=None,
        due=due or datetime(2026, 6, 1, 17, 0, tzinfo=UTC),
        status="NEEDS-ACTION",
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )


class IndexRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.repo.close)

    def test_upsert_then_get_event(self) -> None:
        ref = _ref("e1@example.com")
        event = _event(ref, href="/dav/e1.ics", etag="v1")
        self.repo.upsert_component(event)
        fetched = self.repo.get_component(ref)
        self.assertIsNotNone(fetched)
        assert isinstance(fetched, VEvent)
        self.assertEqual(fetched.ref, ref)
        self.assertEqual(fetched.summary, "Weekly sync")
        self.assertEqual(fetched.dtstart, event.dtstart)

    def test_upsert_is_idempotent(self) -> None:
        ref = _ref("e1@example.com")
        event = _event(ref, href="/dav/e1.ics")
        self.repo.upsert_component(event)
        self.repo.upsert_component(event)
        components = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(len(components), 1)

    def test_upsert_updates_on_conflict(self) -> None:
        ref = _ref("e1@example.com")
        first = _event(ref, href="/dav/e1.ics", etag="v1", summary="Original")
        second = _event(ref, href="/dav/e1.ics", etag="v2", summary="Revised")
        self.repo.upsert_component(first)
        self.repo.upsert_component(second)
        fetched = self.repo.get_component(ref)
        assert isinstance(fetched, VEvent)
        self.assertEqual(fetched.etag, "v2")
        self.assertEqual(fetched.summary, "Revised")

    def test_master_and_override_are_distinct_rows(self) -> None:
        master = _event(_ref("series@example.com"), href="/dav/s.ics", etag="v1")
        override = _event(
            _ref("series@example.com", recurrence_id="2026-05-08T09:00:00+00:00"),
            href="/dav/s.ics",
            etag="v1",
            summary="Rescheduled",
        )
        self.repo.upsert_component(master)
        self.repo.upsert_component(override)
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(len(rows), 2)

    def test_delete_component(self) -> None:
        ref = _ref("gone@example.com")
        self.repo.upsert_component(_event(ref, href="/dav/gone.ics"))
        self.repo.delete_component(ref)
        self.assertIsNone(self.repo.get_component(ref))

    def test_list_pending_pushes_returns_href_null_rows(self) -> None:
        pending = _event(_ref("pending@example.com"))  # href is None
        synced = _event(_ref("synced@example.com"), href="/dav/s.ics", etag="v1")
        self.repo.upsert_component(pending)
        self.repo.upsert_component(synced)
        pushes = self.repo.list_pending_pushes(CalendarRef("personal", "work"))
        uids = {c.ref.uid for c in pushes}
        self.assertEqual(uids, {"pending@example.com"})

    def test_local_flags_round_trip(self) -> None:
        ref = _ref("flagged@example.com")
        flagged = _event(
            ref,
            href="/dav/f.ics",
            etag="v1",
            local_flags=frozenset({"starred", "important"}),
        )
        self.repo.upsert_component(flagged)
        fetched = self.repo.get_component(ref)
        assert fetched is not None
        self.assertEqual(fetched.local_flags, frozenset({"starred", "important"}))

    def test_vtodo_round_trip_preserves_due_not_dtend(self) -> None:
        ref = _ref("t1@example.com")
        todo = _todo(ref)
        self.repo.upsert_component(todo)
        fetched = self.repo.get_component(ref)
        self.assertIsInstance(fetched, VTodo)
        assert isinstance(fetched, VTodo)
        self.assertEqual(fetched.due, todo.due)

    def test_sync_state_round_trip(self) -> None:
        calendar = CalendarRef("personal", "work")
        state = SyncState(
            calendar=calendar,
            ctag="ctag-1",
            sync_token="https://example.com/tok/1",
            synced_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        )
        self.assertIsNone(self.repo.get_sync_state(calendar))
        self.repo.set_sync_state(state)
        fetched = self.repo.get_sync_state(calendar)
        self.assertEqual(fetched, state)

    def test_sync_state_round_trips_calendar_url(self) -> None:
        calendar = CalendarRef("personal", "work")
        state = SyncState(
            calendar=calendar,
            ctag="ctag-1",
            sync_token="tok-1",
            synced_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            calendar_url="https://cal.example.com/dav/work/",
        )
        self.repo.set_sync_state(state)
        fetched = self.repo.get_sync_state(calendar)
        assert fetched is not None
        self.assertEqual(fetched.calendar_url, "https://cal.example.com/dav/work/")

    def test_sync_state_overwrites_on_upsert(self) -> None:
        calendar = CalendarRef("personal", "work")
        self.repo.set_sync_state(
            SyncState(calendar=calendar, ctag="a", sync_token=None, synced_at=None)
        )
        self.repo.set_sync_state(
            SyncState(calendar=calendar, ctag="b", sync_token="t", synced_at=None)
        )
        fetched = self.repo.get_sync_state(calendar)
        assert fetched is not None
        self.assertEqual(fetched.ctag, "b")
        self.assertEqual(fetched.sync_token, "t")

    def test_clear_account_sync_state_scopes_to_one_account(self) -> None:
        for acct in ("personal", "work"):
            self.repo.set_sync_state(
                SyncState(
                    calendar=CalendarRef(acct, "cal"),
                    ctag="c",
                    sync_token=None,
                    synced_at=None,
                )
            )
        removed = self.repo.clear_account_sync_state("personal")
        self.assertEqual(removed, 1)
        self.assertIsNone(self.repo.get_sync_state(CalendarRef("personal", "cal")))
        # The sibling account's row is left intact.
        self.assertIsNotNone(self.repo.get_sync_state(CalendarRef("work", "cal")))


class SchemaMigrationTest(unittest.TestCase):
    def test_adds_calendar_url_column_to_legacy_database(self) -> None:
        import sqlite3

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp / "legacy.sqlite3"
        # Create a pre-migration calendar_sync_state without calendar_url
        # and seed a row, mimicking a database from an older release.
        legacy = sqlite3.connect(db_path)
        legacy.executescript(
            """
            CREATE TABLE calendar_sync_state(
                account_name   TEXT NOT NULL,
                calendar_name  TEXT NOT NULL,
                ctag           TEXT,
                sync_token     TEXT,
                synced_at      TEXT,
                PRIMARY KEY (account_name, calendar_name)
            );
            INSERT INTO calendar_sync_state
                (account_name, calendar_name, ctag, sync_token, synced_at)
                VALUES ('personal', 'work', 'ctag-1', 'tok-1', NULL);
            """
        )
        legacy.commit()
        legacy.close()

        repo = SqliteIndexRepository(db_path)
        self.addCleanup(repo.close)

        # The legacy row reads back with calendar_url == None (no spurious
        # invalidation), and new writes persist the column.
        state = repo.get_sync_state(CalendarRef("personal", "work"))
        assert state is not None
        self.assertEqual(state.ctag, "ctag-1")
        self.assertIsNone(state.calendar_url)

        repo.set_sync_state(
            SyncState(
                calendar=CalendarRef("personal", "work"),
                ctag="ctag-2",
                sync_token="tok-2",
                synced_at=None,
                calendar_url="https://cal.example.com/dav/work/",
            )
        )
        refreshed = repo.get_sync_state(CalendarRef("personal", "work"))
        assert refreshed is not None
        self.assertEqual(refreshed.calendar_url, "https://cal.example.com/dav/work/")

    def test_migration_is_idempotent(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp / "index.sqlite3"
        # Opening twice must not fail on a duplicate ADD COLUMN.
        repo1 = SqliteIndexRepository(db_path)
        repo1.close()
        repo2 = SqliteIndexRepository(db_path)
        self.addCleanup(repo2.close)
        self.assertIsNone(repo2.get_sync_state(CalendarRef("personal", "work")))


class FtsSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.repo.close)
        self.repo.upsert_component(
            _event(
                _ref("standup@example.com"),
                href="/dav/s.ics",
                etag="v1",
                summary="Daily standup",
                description="Team sync every morning",
                location="Office",
            )
        )
        self.repo.upsert_component(
            _event(
                _ref("review@example.com"),
                href="/dav/r.ics",
                etag="v2",
                summary="Quarterly review",
                description="Planning session",
                location="Board room",
            )
        )

    def test_search_matches_summary(self) -> None:
        hits = self.repo.search("standup")
        self.assertEqual([h.ref.uid for h in hits], ["standup@example.com"])

    def test_search_matches_description(self) -> None:
        hits = self.repo.search("planning")
        self.assertEqual([h.ref.uid for h in hits], ["review@example.com"])

    def test_search_matches_location(self) -> None:
        hits = self.repo.search("office")
        self.assertEqual([h.ref.uid for h in hits], ["standup@example.com"])

    def test_search_empty_query_returns_nothing(self) -> None:
        self.assertEqual(self.repo.search(""), ())

    def test_search_updated_content_reflected(self) -> None:
        ref = _ref("standup@example.com")
        updated = _event(
            ref,
            href="/dav/s.ics",
            etag="v2",
            summary="Daily gathering",
            description="Team sync every morning",
            location="Office",
        )
        self.repo.upsert_component(updated)
        hits = self.repo.search("standup")
        self.assertEqual(hits, ())
        hits = self.repo.search("gathering")
        self.assertEqual([h.ref.uid for h in hits], ["standup@example.com"])

    def test_search_respects_calendar_filter(self) -> None:
        other = _ref("standup2@example.com")
        other_ref = ComponentRef(
            account_name=other.account_name,
            calendar_name="personal",
            uid=other.uid,
        )
        self.repo.upsert_component(
            _event(
                other_ref,
                href="/dav/other.ics",
                etag="v1",
                summary="Daily standup",
            )
        )
        personal_hits = self.repo.search(
            "standup", calendar=CalendarRef("personal", "personal")
        )
        self.assertEqual(len(personal_hits), 1)
        self.assertEqual(personal_hits[0].ref.calendar_name, "personal")


class ConnectionContextManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.repo.close)

    def test_batched_writes_are_one_transaction(self) -> None:
        with self.repo.connection():
            self.repo.upsert_component(
                _event(_ref("a@example.com"), href="/dav/a.ics", etag="v1")
            )
            self.repo.upsert_component(
                _event(_ref("b@example.com"), href="/dav/b.ics", etag="v1")
            )
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(len(rows), 2)

    def test_rollback_on_exception_reverts_writes(self) -> None:
        with (
            self.assertRaises(RuntimeError),
            self.repo.connection(),
        ):
            self.repo.upsert_component(
                _event(_ref("a@example.com"), href="/dav/a.ics", etag="v1")
            )
            raise RuntimeError("boom")
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(rows, ())

    def test_keyboard_interrupt_inside_transaction_rolls_back(self) -> None:
        # The connection() context manager catches BaseException, not
        # just Exception, so a Ctrl-C mid-batch rolls back rather than
        # leaving partial rows behind. This is the load-bearing
        # invariant for resumable sync: ingest of one resource is
        # all-or-nothing, and the next sync re-fetches what's missing.
        with self.assertRaises(KeyboardInterrupt), self.repo.connection():
            self.repo.upsert_component(
                _event(_ref("partial@example.com"), href="/dav/p.ics", etag="v1")
            )
            raise KeyboardInterrupt
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(rows, ())

    def test_nested_connection_shares_transaction(self) -> None:
        # `_ingest_resource` opens a transaction, then calls
        # `upsert_component`, which itself uses connection(). The inner
        # call must reuse the outer transaction; otherwise the inner's
        # auto-commit would expose a half-built resource to readers
        # before the outer is done.
        with self.repo.connection():
            self.repo.upsert_component(
                _event(_ref("nested-a@example.com"), href="/dav/a.ics", etag="v1")
            )
            self.repo.upsert_component(
                _event(_ref("nested-b@example.com"), href="/dav/b.ics", etag="v1")
            )
            # Mid-transaction read sees the rows because we're using
            # the same connection that's mid-write.
            mid = self.repo.list_calendar_components(CalendarRef("personal", "work"))
            self.assertEqual(len(mid), 2)
        # Post-commit read still sees them.
        after = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(len(after), 2)

    def test_outer_rolls_back_when_inner_raises_after_partial_writes(self) -> None:
        # Ingest one resource successfully, start a second, blow up
        # mid-second. Both must roll back together because they're
        # in the same outer transaction.
        with self.assertRaises(KeyboardInterrupt), self.repo.connection():
            self.repo.upsert_component(
                _event(_ref("outer-a@example.com"), href="/dav/a.ics", etag="v1")
            )
            self.repo.upsert_component(
                _event(_ref("outer-b@example.com"), href="/dav/b.ics", etag="v1")
            )
            raise KeyboardInterrupt
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(rows, ())

    def test_resource_committed_independently_when_separate_transactions(self) -> None:
        # `_fetch_and_ingest` calls `_ingest_resource` per fetched
        # resource, each opening its own transaction. An interrupt
        # between resources must preserve everything committed before
        # it — that's what makes sync resumable.
        # First resource: separate transaction.
        with self.repo.connection():
            self.repo.upsert_component(
                _event(_ref("first@example.com"), href="/dav/1.ics", etag="v1")
            )
        # Second resource: blows up mid-transaction.
        with self.assertRaises(KeyboardInterrupt), self.repo.connection():
            self.repo.upsert_component(
                _event(_ref("second@example.com"), href="/dav/2.ics", etag="v1")
            )
            raise KeyboardInterrupt
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        # First survives, second rolled back.
        self.assertEqual({r.ref.uid for r in rows}, {"first@example.com"})


class CrossThreadAccessTest(unittest.TestCase):
    """The TUI runs sync on a worker thread while the UI thread keeps
    reading from the same repository. sqlite3's `check_same_thread`
    guard blocks that by default; the repository now opens per-thread
    connections so cross-thread access works without corruption."""

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.repo.close)

    def test_writes_from_worker_thread_visible_on_main_thread(self) -> None:
        import threading

        errors: list[BaseException] = []

        def worker() -> None:
            try:
                self.repo.upsert_component(
                    _event(_ref("worker@example.com"), href="/dav/w.ics", etag="v1")
                )
            except BaseException as exc:  # noqa: BLE001 — propagate to main
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(errors, [])
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual({r.ref.uid for r in rows}, {"worker@example.com"})

    def test_close_is_safe_from_a_different_thread(self) -> None:
        import threading

        # Open a connection on a worker thread first; main thread then
        # closes the repo. With `check_same_thread=False` (the new
        # default) close() can walk and close every cached connection
        # regardless of which thread opened it.
        def warm_up() -> None:
            self.repo.list_calendar_components(CalendarRef("personal", "work"))

        thread = threading.Thread(target=warm_up)
        thread.start()
        thread.join()
        # Should not raise, and neither should the second close the
        # cleanup in setUp makes: every connection it handed out is
        # already shut, and nothing is handed a closed one again.
        self.repo.close()


class AlarmRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.db")
        self.addCleanup(self.repo.close)
        # Insert a component to hold FK references.
        self.cal = CalendarRef("personal", "work")
        self.ref = _ref("alarm-uid@example.com")
        self.repo.upsert_component(_event(self.ref, summary="Team standup"))

    def _make_alarm(
        self,
        trigger_at: datetime,
        action: AlarmAction = AlarmAction.DISPLAY,
        description: str | None = "Reminder",
    ) -> AlarmRecord:
        return AlarmRecord(
            db_id=None,
            ref=self.ref,
            summary="Team standup",
            occurrence_start=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            trigger_at=trigger_at,
            action=action,
            description=description,
            fired_at=None,
        )

    def test_set_and_query_pending_alarms(self) -> None:
        trigger = datetime(2026, 5, 1, 8, 45, tzinfo=UTC)
        self.repo.set_alarms(
            self.ref,
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            [self._make_alarm(trigger)],
        )
        window_start = datetime(2026, 5, 1, 8, 30, tzinfo=UTC)
        window_end = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        pending = self.repo.query_pending_alarms(window_start, window_end)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].trigger_at, trigger)
        self.assertEqual(pending[0].summary, "Team standup")
        self.assertIsNotNone(pending[0].db_id)

    def test_due_alarms_announces_each_one_once(self) -> None:
        """One answer to "which reminders are due", for two announcers.

        The calendar on its own raises a toast, rings the bell and emits
        OSC 777; Pony Express publishes through its notification space.
        Each used to query, decide the lookback and mark the alarm
        fired itself, with the lookback written out twice -- so a
        reminder could be shown twice by one half and not at all by the
        other.
        """
        from pony.calendar.alarms import ALARM_LOOKBACK, due_alarms

        occurrence = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        self.repo.set_alarms(
            self.ref,
            occurrence,
            [self._make_alarm(datetime(2026, 5, 1, 8, 45, tzinfo=UTC))],
        )
        now = datetime(2026, 5, 1, 8, 50, tzinfo=UTC)

        first = due_alarms(self.repo, now)
        self.assertEqual([alarm.title for alarm in first], ["Team standup"])
        self.assertIn("Starts", first[0].body)
        # Marked as it was converted, so a second poll says nothing.
        self.assertEqual(due_alarms(self.repo, now), ())

        # And the lookback is the shared one: an alarm older than it has
        # been missed, one inside it has not.  A fresh trigger, because
        # `set_alarms` keeps a `fired_at` for one it has seen before.
        later_trigger = datetime(2026, 5, 1, 10, 45, tzinfo=UTC)
        self.repo.set_alarms(
            self.ref,
            datetime(2026, 5, 1, 11, 0, tzinfo=UTC),
            [self._make_alarm(later_trigger)],
        )
        self.assertEqual(
            due_alarms(
                self.repo, later_trigger + ALARM_LOOKBACK + timedelta(minutes=1)
            ),
            (),
        )
        self.assertEqual(
            len(
                due_alarms(
                    self.repo, later_trigger + ALARM_LOOKBACK - timedelta(minutes=1)
                )
            ),
            1,
        )

    def test_query_outside_window_returns_empty(self) -> None:
        trigger = datetime(2026, 5, 1, 8, 45, tzinfo=UTC)
        self.repo.set_alarms(
            self.ref,
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            [self._make_alarm(trigger)],
        )
        # Window entirely before trigger
        pending = self.repo.query_pending_alarms(
            datetime(2026, 5, 1, 7, 0, tzinfo=UTC),
            datetime(2026, 5, 1, 8, 0, tzinfo=UTC),
        )
        self.assertEqual(len(pending), 0)

    def test_mark_alarm_fired_excludes_from_pending(self) -> None:
        trigger = datetime(2026, 5, 1, 8, 45, tzinfo=UTC)
        occ_start = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        self.repo.set_alarms(self.ref, occ_start, [self._make_alarm(trigger)])
        pending = self.repo.query_pending_alarms(
            datetime(2026, 5, 1, 8, 30, tzinfo=UTC),
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
        alarm_id = pending[0].db_id
        assert alarm_id is not None
        fired_at = datetime(2026, 5, 1, 8, 46, tzinfo=UTC)
        self.repo.mark_alarm_fired(alarm_id, fired_at)
        still_pending = self.repo.query_pending_alarms(
            datetime(2026, 5, 1, 8, 30, tzinfo=UTC),
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
        self.assertEqual(len(still_pending), 0)

    def test_set_alarms_keeps_fired_at_across_rebuild(self) -> None:
        # Rebuilding the alarm cache (e.g. after a sync touched the
        # event) must not make an already-fired alarm fire again.
        occ_start = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        trigger = datetime(2026, 5, 1, 8, 45, tzinfo=UTC)
        window = (
            datetime(2026, 5, 1, 8, 30, tzinfo=UTC),
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
        self.repo.set_alarms(self.ref, occ_start, [self._make_alarm(trigger)])
        alarm_id = self.repo.query_pending_alarms(*window)[0].db_id
        assert alarm_id is not None
        self.repo.mark_alarm_fired(alarm_id, datetime(2026, 5, 1, 8, 46, tzinfo=UTC))

        self.repo.set_alarms(self.ref, occ_start, [self._make_alarm(trigger)])
        self.assertEqual(self.repo.query_pending_alarms(*window), ())

        # A moved trigger is a new alarm and is pending again.
        moved = datetime(2026, 5, 1, 8, 50, tzinfo=UTC)
        self.repo.set_alarms(self.ref, occ_start, [self._make_alarm(moved)])
        self.assertEqual(len(self.repo.query_pending_alarms(*window)), 1)

    def test_set_alarms_replaces_existing(self) -> None:
        occ_start = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        trigger_a = datetime(2026, 5, 1, 8, 45, tzinfo=UTC)
        trigger_b = datetime(2026, 5, 1, 8, 50, tzinfo=UTC)
        self.repo.set_alarms(self.ref, occ_start, [self._make_alarm(trigger_a)])
        # Second call for same occurrence replaces the first.
        self.repo.set_alarms(self.ref, occ_start, [self._make_alarm(trigger_b)])
        pending = self.repo.query_pending_alarms(
            datetime(2026, 5, 1, 8, 30, tzinfo=UTC),
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].trigger_at, trigger_b)

    def test_cascade_delete_removes_alarms(self) -> None:
        occ_start = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        self.repo.set_alarms(
            self.ref,
            occ_start,
            [self._make_alarm(datetime(2026, 5, 1, 8, 45, tzinfo=UTC))],
        )
        self.repo.delete_component(self.ref)
        pending = self.repo.query_pending_alarms(
            datetime(2026, 5, 1, 8, 30, tzinfo=UTC),
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
        self.assertEqual(len(pending), 0)

    def test_set_alarms_no_component_is_noop(self) -> None:
        missing_ref = _ref("nonexistent@example.com")
        # Should not raise even though the component doesn't exist.
        self.repo.set_alarms(
            missing_ref,
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            [self._make_alarm(datetime(2026, 5, 1, 8, 45, tzinfo=UTC))],
        )


class ComponentLookupPlanTest(unittest.TestCase):
    """Every component lookup is keyed on (account, calendar, uid).

    `upsert_component` does two of them -- one to find the row, one to
    invalidate the master's occurrences -- so an unindexed lookup makes
    writing a calendar quadratic in its size: 5,400 upserts/second at
    1,000 rows, 550/second at 4,000.  `get_component`,
    `delete_component` and `set_occurrences` ask the same question.
    """

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.repo.close)

    def _plan(self, sql: str, params: tuple[object, ...]) -> str:
        with self.repo.connection() as conn:
            rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        return "\n".join(str(r[3]) for r in rows)

    def test_lookup_by_account_calendar_uid_searches_rather_than_scans(self) -> None:
        plan = self._plan(
            "SELECT id FROM components "
            "WHERE account_name = ? AND calendar_name = ? AND uid = ? "
            "AND COALESCE(recurrence_id, '') = COALESCE(?, '')",
            ("personal", "work", "e1@example.com", None),
        )
        self.assertIn("ix_components_calendar_uid", plan, plan)
        self.assertNotIn("SCAN", plan, plan)

    def test_the_todo_listing_uses_its_own_partial_index(self) -> None:
        """Otherwise the agenda scans the calendar's events to find tasks.

        The kind is a literal in that statement rather than a bound
        parameter, because SQLite only matches a partial index when the
        query proves the index's ``WHERE``.
        """
        plan = self._plan(
            "SELECT uid FROM components "
            "WHERE account_name = ? AND calendar_name = ? "
            "AND component_kind = 'VTODO' AND local_status = ? "
            "AND COALESCE(due, dtstart) >= ? AND COALESCE(due, dtstart) < ?",
            ("personal", "work", "active", "x", "y"),
        )
        self.assertIn("ix_components_todo", plan, plan)
        self.assertNotIn("SCAN", plan, plan)

    def test_master_invalidation_lookup_searches_rather_than_scans(self) -> None:
        plan = self._plan(
            "SELECT id FROM components "
            "WHERE account_name = ? AND calendar_name = ? AND uid = ? "
            "AND recurrence_id IS NULL",
            ("personal", "work", "e1@example.com"),
        )
        self.assertIn("ix_components_calendar_uid", plan, plan)
        self.assertNotIn("SCAN", plan, plan)


class NarrowComponentQueryTest(unittest.TestCase):
    """`list_components_for_uids` and `list_todos`, which exist so that a
    render or a cache rebuild does not read the whole calendar."""

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.repo.close)
        self.calendar = CalendarRef("personal", "work")

    def test_returns_the_named_uids_with_their_overrides(self) -> None:
        for uid in ("a@example.com", "b@example.com", "c@example.com"):
            self.repo.upsert_component(_event(_ref(uid), href=f"/dav/{uid}.ics"))
        self.repo.upsert_component(
            _event(
                _ref("a@example.com", "2026-05-08T09:00:00+00:00"),
                href="/dav/a-override.ics",
            )
        )
        found = self.repo.list_components_for_uids(
            self.calendar, ["a@example.com", "c@example.com"]
        )
        self.assertEqual(
            [(c.ref.uid, c.ref.recurrence_id) for c in found],
            [
                ("a@example.com", None),
                ("a@example.com", "2026-05-08T09:00:00+00:00"),
                ("c@example.com", None),
            ],
        )

    def test_empty_uid_set_asks_nothing(self) -> None:
        self.repo.upsert_component(_event(_ref("a@example.com"), href="/dav/a.ics"))
        self.assertEqual(self.repo.list_components_for_uids(self.calendar, []), ())

    def test_a_uid_list_longer_than_one_statement_holds(self) -> None:
        """Chunked `IN (…)` keeps its order and misses nothing.

        SQLite caps host parameters per statement, so a caller's list is
        issued in runs; the runs are sorted, so concatenating them is
        still sorted.
        """
        from pony.calendar import index_store

        uids = [f"u{n:04d}@example.com" for n in range(25)]
        for uid in uids:
            self.repo.upsert_component(_event(_ref(uid), href=f"/dav/{uid}.ics"))
        with unittest.mock.patch.object(index_store, "_MAX_SQL_PARAMETERS", 4):
            found = self.repo.list_components_for_uids(self.calendar, uids)
        self.assertEqual([c.ref.uid for c in found], uids)

    def test_list_todos_skips_events_and_inactive_tasks(self) -> None:
        self.repo.upsert_component(_event(_ref("e@example.com"), href="/dav/e.ics"))
        self.repo.upsert_component(_todo(_ref("t1@example.com")))
        self.repo.upsert_component(
            dataclasses.replace(
                _todo(_ref("t2@example.com")),
                local_status=LocalStatus.TRASHED,
            )
        )
        found = self.repo.list_todos(self.calendar)
        self.assertEqual([c.ref.uid for c in found], ["t1@example.com"])

    def test_list_todos_window_filters_on_the_anchor(self) -> None:
        self.repo.upsert_component(
            _todo(_ref("in@example.com"), due=datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
        )
        self.repo.upsert_component(
            _todo(_ref("out@example.com"), due=datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
        )
        found = self.repo.list_todos(
            self.calendar,
            window=(
                datetime(2026, 6, 1, tzinfo=UTC),
                datetime(2026, 6, 2, tzinfo=UTC),
            ),
        )
        self.assertEqual([c.ref.uid for c in found], ["in@example.com"])

    def test_query_occurrences_can_be_restricted_to_uids(self) -> None:
        from pony.calendar.domain import Occurrence

        start = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        for uid in ("a@example.com", "b@example.com"):
            ref = _ref(uid)
            self.repo.upsert_component(_event(ref, href=f"/dav/{uid}.ics"))
            self.repo.set_occurrences(
                ref,
                [
                    Occurrence(
                        ref=ref,
                        start=start,
                        end=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
                        recurrence_id=None,
                        is_override=False,
                    )
                ],
            )
        window = (
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 2, tzinfo=UTC),
        )
        everything = self.repo.query_occurrences(self.calendar, *window)
        restricted = self.repo.query_occurrences(
            self.calendar, *window, uids=["a@example.com"]
        )
        self.assertEqual(len(everything), 2)
        self.assertEqual([o.ref.uid for o in restricted], ["a@example.com"])
        self.assertEqual(
            self.repo.query_occurrences(self.calendar, *window, uids=[]), ()
        )
