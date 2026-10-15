"""The event operations, exercised without a user interface.

Saving, rescheduling, trashing and importing used to be written out
inside the calendar's ``MainScreen``, so reaching the recurrence-cache
rebuild that makes a saved event visible meant starting a Textual app.
They live in :mod:`pony.calendar.event_actions` now, over the stores, and
these tests go straight at them, against real SQLite and a real vdir.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pony.calendar.domain import (
    AccountConfig,
    AppConfig,
    CalendarRef,
    ComponentRef,
    LocalStatus,
    PlaintextCredential,
    ResourceRef,
    VEvent,
    VTodo,
)
from pony.calendar.event_actions import (
    Calendars,
    EditDraft,
    import_ics_file,
    organizer_for,
    reschedule_event,
    save_event,
    trash_event,
)
from pony.calendar.index_store import SqliteIndexRepository
from pony.calendar.storage import VdirMirrorRepository
from pony.scope import SyncScope

_CALENDAR = CalendarRef(account_name="personal", calendar_name="work")
_NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def _account(username: str = "user@example.com") -> AccountConfig:
    return AccountConfig(
        name="personal",
        url="https://caldav.example.com/dav/",
        username=username,
        credential=PlaintextCredential(password="s3cret"),
        mirror_path=Path("/unused"),
        trash_retention_days=30,
        scope=SyncScope(),
    )


def _vevent_ics(uid: str, summary: str = "Standup") -> bytes:
    lines = (
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "DTSTAMP:20260422T120000Z",
        "DTSTART:20260501T090000Z",
        "DTEND:20260501T100000Z",
        f"SUMMARY:{summary}",
        "END:VEVENT",
        "END:VCALENDAR",
    )
    return ("\r\n".join(lines) + "\r\n").encode()


class EventActionsTest(unittest.TestCase):
    """Real stores in a temp directory, no app."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.mirror = VdirMirrorRepository(root / "mirror")
        self.index = SqliteIndexRepository(root / "index.sqlite3")
        self.calendars = Calendars(
            config=AppConfig(
                config_version=1,
                use_utf8=False,
                editor=None,
                accounts=(_account(),),
            ),
            mirror=self.mirror,
            index=self.index,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _draft(self, **overrides: object) -> EditDraft:
        fields: dict[str, object] = {
            "target": _CALENDAR,
            "summary": "Standup",
            "dtstart": datetime(2026, 5, 4, 9, 0, tzinfo=UTC),
            "dtend": datetime(2026, 5, 4, 9, 30, tzinfo=UTC),
            "location": "Room 2",
            "description": "Daily",
            "attendees": (),
            "alarms": (),
            "existing": None,
        }
        fields.update(overrides)
        return EditDraft(**fields)  # type: ignore[arg-type]

    # -- saving --------------------------------------------------------

    def test_save_writes_the_mirror_the_index_and_the_caches(self) -> None:
        saved = save_event(self.calendars, self._draft(), now=_NOW)

        self.assertTrue(saved.created)
        self.assertEqual(
            [notice.text for notice in saved.outcome.notices],
            ["Created 'Standup'"],
        )
        # The mirror holds the text, byte for byte what was stored.
        self.assertEqual(self.mirror.read(saved.component.ref.resource), saved.ics)
        stored = self.index.get_component(saved.component.ref)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.summary, "Standup")
        # A new event has no server identity yet: that is what tells the
        # next sync to create it.
        self.assertIsNone(stored.href)
        self.assertIsNone(stored.etag)
        # And it is in the occurrence cache, which is what the agenda,
        # day and grid views actually read.
        occurrences = self.index.query_occurrences(
            _CALENDAR,
            datetime(2026, 5, 4, tzinfo=UTC),
            datetime(2026, 5, 5, tzinfo=UTC),
        )
        self.assertEqual([o.ref.uid for o in occurrences], [stored.ref.uid])

    def test_saving_an_edit_keeps_the_server_identity_and_marks_it_edited(self) -> None:
        saved = save_event(self.calendars, self._draft(), now=_NOW)
        # Pretend a sync has been and gone: the row now has a server
        # identity, which is what an edit must not throw away.
        synced = replace(
            saved.component,
            href="/dav/work/event.ics",
            etag='"abc"',
            local_flags=frozenset(),
            synced_at=_NOW,
        )
        self.index.upsert_component(synced)

        again = save_event(
            self.calendars,
            self._draft(summary="Standup (moved)", existing=synced),
            now=_NOW + timedelta(minutes=5),
        )

        self.assertFalse(again.created)
        self.assertEqual(
            [notice.text for notice in again.outcome.notices],
            ["Updated 'Standup (moved)'"],
        )
        stored = self.index.get_component(synced.ref)
        assert stored is not None
        self.assertEqual(stored.summary, "Standup (moved)")
        # The href and etag survive, so sync still knows which resource
        # this is, and the row is marked as locally edited.
        self.assertEqual(stored.href, "/dav/work/event.ics")
        self.assertEqual(stored.etag, '"abc"')
        self.assertTrue(stored.local_flags)
        # One row, not a second event beside the first.
        self.assertEqual(len(self.index.list_calendar_components(_CALENDAR)), 1)

    def test_save_puts_the_account_address_in_organizer(self) -> None:
        saved = save_event(
            self.calendars,
            self._draft(attendees=("guest@example.com",)),
            now=_NOW,
        )

        self.assertEqual(saved.organizer, "user@example.com")
        self.assertIn(b"ORGANIZER", saved.ics)

    def test_an_edit_keeps_the_organizer_the_event_already_names(self) -> None:
        """Re-deriving it from the account would hijack someone's event."""
        ref = ComponentRef("personal", "work", "invited-1")
        ics = _vevent_ics("invited-1").replace(
            b"BEGIN:VEVENT\r\n",
            b"BEGIN:VEVENT\r\nORGANIZER:mailto:chair@example.org\r\n",
        )
        self.mirror.write(ResourceRef("personal", "work", "invited-1"), ics)
        existing = VEvent(
            ref=ref,
            href="/dav/work/invited-1.ics",
            etag='"1"',
            raw_ics=ics,
            summary="Board meeting",
            description=None,
            location=None,
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=_NOW,
        )
        self.index.upsert_component(existing)

        saved = save_event(
            self.calendars,
            self._draft(summary="Board meeting", existing=existing),
            now=_NOW,
        )

        self.assertEqual(saved.organizer, "chair@example.org")

    # -- rescheduling --------------------------------------------------

    def test_reschedule_shifts_both_ends_and_keeps_the_stored_text(self) -> None:
        saved = save_event(self.calendars, self._draft(), now=_NOW)

        outcome = reschedule_event(
            self.calendars,
            saved.component,
            timedelta(hours=2),
            now=_NOW,
        )

        self.assertEqual(
            [notice.text for notice in outcome.notices],
            ["Moved 'Standup'"],
        )
        stored = self.index.get_component(saved.component.ref)
        assert stored is not None
        assert stored.dtstart is not None
        assert saved.component.dtstart is not None
        self.assertEqual(stored.dtstart, saved.component.dtstart + timedelta(hours=2))
        # The description was never re-entered, so it can only have
        # survived because the text was rewritten rather than rebuilt.
        self.assertIn(b"Daily", stored.raw_ics)

    def test_reschedule_declines_anything_that_is_not_a_timed_event(self) -> None:
        todo = VTodo(
            ref=ComponentRef("personal", "work", "todo-1"),
            href=None,
            etag=None,
            raw_ics=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
            summary="Pay the invoice",
            description=None,
            location=None,
            dtstart=None,
            due=datetime(2026, 5, 5, 17, 0, tzinfo=UTC),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )

        outcome = reschedule_event(self.calendars, todo, timedelta(hours=1), now=_NOW)

        self.assertEqual(outcome.notices, ())
        self.assertFalse(outcome.acted)

    def test_reschedule_reports_text_it_cannot_rewrite(self) -> None:
        broken = VEvent(
            ref=ComponentRef("personal", "work", "broken-1"),
            href=None,
            etag=None,
            raw_ics=b"not an icalendar document",
            summary="Broken",
            description=None,
            location=None,
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=None,
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )

        outcome = reschedule_event(self.calendars, broken, timedelta(hours=1), now=_NOW)

        self.assertFalse(outcome.acted)
        self.assertEqual(len(outcome.notices), 1)

    # -- trashing ------------------------------------------------------

    def test_trash_marks_the_row_for_the_next_sync_to_delete(self) -> None:
        saved = save_event(self.calendars, self._draft(), now=_NOW)

        outcome = trash_event(self.calendars, saved.component, now=_NOW)

        self.assertEqual(
            [notice.text for notice in outcome.notices],
            ["Trashed 'Standup'"],
        )
        stored = self.index.get_component(saved.component.ref)
        assert stored is not None
        self.assertIs(stored.local_status, LocalStatus.TRASHED)
        self.assertEqual(stored.trashed_at, _NOW)

    def test_trash_falls_back_to_the_uid_when_there_is_no_summary(self) -> None:
        saved = save_event(self.calendars, self._draft(summary=""), now=_NOW)

        outcome = trash_event(self.calendars, saved.component, now=_NOW)

        self.assertIn(saved.component.ref.uid, outcome.notices[0].text)

    # -- importing -----------------------------------------------------

    def test_import_files_a_calendar_and_counts_what_it_did(self) -> None:
        path = Path(self._tmp.name) / "invite.ics"
        path.write_bytes(_vevent_ics("imported-1", summary="Conference"))

        outcome = import_ics_file(self.calendars, _CALENDAR, path)

        self.assertEqual(
            [notice.text for notice in outcome.notices],
            ["Imported 1, updated 0, cancelled 0 (skipped 0)"],
        )
        self.assertTrue(outcome.acted)
        self.assertFalse(outcome.failed)
        stored = self.index.get_component(
            ComponentRef("personal", "work", "imported-1"),
        )
        self.assertIsNotNone(stored)

    def test_import_reports_a_file_it_cannot_read(self) -> None:
        missing = Path(self._tmp.name) / "nothing.ics"

        outcome = import_ics_file(self.calendars, _CALENDAR, missing)

        self.assertTrue(outcome.failed)
        self.assertFalse(outcome.acted)
        self.assertTrue(outcome.notices[0].text.startswith("Import failed:"))

    # -- organizer lookup ----------------------------------------------

    def test_organizer_is_only_offered_when_it_is_an_address(self) -> None:
        self.assertEqual(
            organizer_for(_CALENDAR, self.calendars.config), "user@example.com"
        )

        bare = AppConfig(
            config_version=1,
            use_utf8=False,
            editor=None,
            accounts=(_account(username="personal"),),
        )
        self.assertIsNone(organizer_for(_CALENDAR, bare))

        elsewhere = CalendarRef(account_name="other", calendar_name="work")
        self.assertIsNone(organizer_for(elsewhere, self.calendars.config))


if __name__ == "__main__":
    unittest.main()


class NewEventDefaultsTest(unittest.TestCase):
    """What a blank event form starts with, decided once.

    The agenda's screen and the browser's blank form each chose the
    duration and the calendar: an hour written out twice, and
    "the calendar in view, or the first one" asked twice.
    """

    def _refs(self) -> tuple[CalendarRef, ...]:
        return (CalendarRef("personal", "work"), CalendarRef("personal", "home"))

    def test_the_span_is_the_default_start_plus_the_default_duration(self) -> None:
        from pony.calendar.event_actions import (
            DEFAULT_EVENT_DURATION,
            default_event_start,
            new_event_defaults,
        )

        day = date(2026, 5, 20)
        now = datetime(2026, 5, 19, 11, 17, tzinfo=UTC)
        defaults = new_event_defaults(self._refs(), day=day, now=now)

        assert defaults is not None
        self.assertEqual(defaults.dtstart, default_event_start(day, now))
        self.assertEqual(defaults.dtend - defaults.dtstart, DEFAULT_EVENT_DURATION)

    def test_the_calendar_in_view_is_the_one_it_goes_in(self) -> None:
        from pony.calendar.event_actions import new_event_defaults

        refs = self._refs()
        defaults = new_event_defaults(
            refs,
            day=date(2026, 5, 20),
            now=datetime(2026, 5, 19, 11, 17, tzinfo=UTC),
            preferred=refs[1],
        )

        assert defaults is not None
        self.assertEqual(defaults.calendar, refs[1])

    def test_a_calendar_not_in_view_falls_back_to_the_first(self) -> None:
        from pony.calendar.event_actions import new_event_defaults

        refs = self._refs()
        defaults = new_event_defaults(
            refs,
            day=date(2026, 5, 20),
            now=datetime(2026, 5, 19, 11, 17, tzinfo=UTC),
            preferred=CalendarRef("other", "shared"),
        )

        assert defaults is not None
        self.assertEqual(defaults.calendar, refs[0])

    def test_no_calendars_means_no_defaults(self) -> None:
        from pony.calendar.event_actions import new_event_defaults

        self.assertIsNone(
            new_event_defaults(
                (), day=date(2026, 5, 20), now=datetime(2026, 5, 19, tzinfo=UTC)
            )
        )
