from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from chronos.domain import AlarmAction, ComponentKind
from chronos.ical_parser import (
    IcalParseError,
    extract_alarm_triggers,
    extract_attendees,
    extract_last_modified,
    extract_organizer,
    extract_sequence,
    parse_vcalendar,
)
from tests_calendar import corpus


class ParseSimpleEventTest(unittest.TestCase):
    def test_single_event_fields_extracted(self) -> None:
        components = parse_vcalendar(corpus.simple_event())
        self.assertEqual(len(components), 1)
        comp = components[0]
        self.assertEqual(comp.kind, ComponentKind.VEVENT)
        self.assertEqual(comp.uid, "simple-event-1@example.com")
        self.assertEqual(comp.summary, "Simple event")
        self.assertIsNone(comp.recurrence_id)
        self.assertEqual(comp.dtstart, datetime(2026, 5, 1, 9, 0, tzinfo=UTC))
        self.assertEqual(comp.dtend, datetime(2026, 5, 1, 10, 0, tzinfo=UTC))


class ParseAllDayEventTest(unittest.TestCase):
    def test_date_value_normalised_to_utc_midnight(self) -> None:
        (comp,) = parse_vcalendar(corpus.all_day_event())
        self.assertEqual(comp.dtstart, datetime(2026, 5, 1, 0, 0, tzinfo=UTC))
        self.assertEqual(comp.dtend, datetime(2026, 5, 2, 0, 0, tzinfo=UTC))


class ParseTimedWithTzTest(unittest.TestCase):
    def test_tzid_datetime_converted_to_utc(self) -> None:
        (comp,) = parse_vcalendar(corpus.timed_event_with_tz())
        # Madrid DST May => UTC+2, so 11:00 local == 09:00 UTC.
        self.assertIsNotNone(comp.dtstart)
        assert comp.dtstart is not None
        self.assertEqual(comp.dtstart.tzinfo, UTC)
        self.assertEqual(
            comp.dtstart.astimezone(UTC),
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )


class ExtractSequenceAndLastModifiedTest(unittest.TestCase):
    """What the both-sides-changed tie-break reads off each version."""

    _RAW = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        b"UID:tie@example.com\r\nSUMMARY:S\r\nDTSTART:20261016T090000Z\r\n"
        b"SEQUENCE:7\r\nLAST-MODIFIED:20261007T101500Z\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    def test_both_are_read_from_the_master(self) -> None:
        self.assertEqual(extract_sequence(self._RAW, "tie@example.com"), 7)
        self.assertEqual(
            extract_last_modified(self._RAW, "tie@example.com"),
            datetime(2026, 10, 7, 10, 15, tzinfo=UTC),
        )

    def test_absent_properties_are_not_invented(self) -> None:
        bare = self._RAW.replace(b"SEQUENCE:7\r\n", b"").replace(
            b"LAST-MODIFIED:20261007T101500Z\r\n", b""
        )
        self.assertEqual(extract_sequence(bare, "tie@example.com"), 0)
        self.assertIsNone(extract_last_modified(bare, "tie@example.com"))

    def test_an_unknown_uid_yields_nothing(self) -> None:
        self.assertEqual(extract_sequence(self._RAW, "other@example.com"), 0)
        self.assertIsNone(extract_last_modified(self._RAW, "other@example.com"))


class ParseDurationTest(unittest.TestCase):
    """DTEND and DURATION are alternatives; both have to be read.

    Reading only DTEND left a DURATION event with no end: the agenda
    showed a start and an empty duration, and the timeline drew no bar at
    all, because a span ending where it begins covers no slot.
    """

    def test_duration_gives_the_event_an_end(self) -> None:
        comp = parse_vcalendar(corpus.event_with_duration())[0]
        self.assertEqual(comp.dtstart, datetime(2026, 10, 16, 9, 0, tzinfo=UTC))
        self.assertEqual(comp.dtend, datetime(2026, 10, 16, 10, 0, tzinfo=UTC))

    def test_duration_gives_a_todo_its_due(self) -> None:
        comp = parse_vcalendar(corpus.todo_with_duration())[0]
        self.assertEqual(comp.kind, ComponentKind.VTODO)
        self.assertEqual(comp.due, datetime(2026, 10, 16, 11, 30, tzinfo=UTC))

    def test_dtend_wins_when_both_are_present(self) -> None:
        raw = corpus.event_with_duration().replace(
            b"DURATION:PT1H", b"DTEND:20261016T113000Z\r\nDURATION:PT1H"
        )
        comp = parse_vcalendar(raw)[0]
        self.assertEqual(comp.dtend, datetime(2026, 10, 16, 11, 30, tzinfo=UTC))

    def test_duration_without_a_start_is_ignored(self) -> None:
        raw = corpus.event_with_duration().replace(b"DTSTART:20261016T090000Z\r\n", b"")
        comp = parse_vcalendar(raw)[0]
        self.assertIsNone(comp.dtstart)
        self.assertIsNone(comp.dtend)

    def test_a_duration_that_is_not_one_is_ignored(self) -> None:
        raw = corpus.event_with_duration().replace(b"DURATION:PT1H", b"DURATION:banana")
        comp = parse_vcalendar(raw)[0]
        self.assertEqual(comp.dtstart, datetime(2026, 10, 16, 9, 0, tzinfo=UTC))
        self.assertIsNone(comp.dtend)


class ParseRecurringWithExceptionsTest(unittest.TestCase):
    def test_master_and_override_both_returned(self) -> None:
        components = parse_vcalendar(corpus.recurring_with_exceptions())
        self.assertEqual(len(components), 2)
        by_rid = {c.recurrence_id: c for c in components}
        master = by_rid[None]
        self.assertIsNone(master.recurrence_id)
        self.assertEqual(master.summary, "Weekly meeting with exceptions")
        override_keys = [k for k in by_rid if k is not None]
        self.assertEqual(len(override_keys), 1)
        override = by_rid[override_keys[0]]
        self.assertEqual(override.summary, "Weekly meeting (rescheduled)")


class ParseTodoTest(unittest.TestCase):
    def test_vtodo_due_is_populated_dtend_is_none(self) -> None:
        (comp,) = parse_vcalendar(corpus.simple_todo())
        self.assertEqual(comp.kind, ComponentKind.VTODO)
        self.assertEqual(comp.due, datetime(2026, 5, 5, 17, 0, tzinfo=UTC))
        self.assertIsNone(comp.dtend)
        self.assertEqual(comp.status, "NEEDS-ACTION")

    def test_completed_todo_has_completed_status(self) -> None:
        (comp,) = parse_vcalendar(corpus.completed_todo())
        self.assertEqual(comp.status, "COMPLETED")


class ParseMalformedTest(unittest.TestCase):
    def test_missing_uid_returns_none_uid(self) -> None:
        (comp,) = parse_vcalendar(corpus.malformed_missing_uid())
        self.assertIsNone(comp.uid)
        self.assertEqual(comp.summary, "No UID present")

    def test_garbage_input_raises(self) -> None:
        with self.assertRaises(IcalParseError):
            parse_vcalendar(b"not an iCalendar document at all")


class ParseEveryCorpusFixtureTest(unittest.TestCase):
    def test_every_single_fixture_yields_at_least_one_component(self) -> None:
        for name, data in corpus.ALL_SINGLE_FIXTURES:
            with self.subTest(fixture=name):
                components = parse_vcalendar(data)
                self.assertGreaterEqual(len(components), 1, msg=name)


class ExtractAlarmTriggersTest(unittest.TestCase):
    def test_display_alarm_relative_to_start(self) -> None:
        raw = corpus.event_with_display_alarm(-15)
        alarms = extract_alarm_triggers(raw, "alarm-display-1@example.com")
        self.assertEqual(len(alarms), 1)
        a = alarms[0]
        self.assertEqual(a.action, AlarmAction.DISPLAY)
        self.assertIsInstance(a.trigger_offset, timedelta)
        self.assertEqual(a.trigger_offset, timedelta(minutes=-15))
        self.assertEqual(a.trigger_related, "START")
        self.assertEqual(a.description, "Time to go")

    def test_audio_alarm_kept(self) -> None:
        raw = corpus.event_with_audio_alarm()
        alarms = extract_alarm_triggers(raw, "alarm-audio-1@example.com")
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0].action, AlarmAction.AUDIO)

    def test_email_alarm_skipped(self) -> None:
        raw = corpus.event_with_email_alarm()
        alarms = extract_alarm_triggers(raw, "alarm-email-1@example.com")
        self.assertEqual(alarms, [])

    def test_end_related_alarm(self) -> None:
        raw = corpus.event_with_end_related_alarm()
        alarms = extract_alarm_triggers(raw, "alarm-end-1@example.com")
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0].trigger_related, "END")
        self.assertEqual(alarms[0].trigger_offset, timedelta(minutes=-5))

    def test_absolute_alarm(self) -> None:
        raw = corpus.event_with_absolute_alarm()
        alarms = extract_alarm_triggers(raw, "alarm-abs-1@example.com")
        self.assertEqual(len(alarms), 1)
        self.assertIsInstance(alarms[0].trigger_offset, datetime)
        expected = datetime(2026, 5, 1, 8, 30, tzinfo=UTC)
        self.assertEqual(alarms[0].trigger_offset, expected)

    def test_no_alarms_returns_empty(self) -> None:
        raw = corpus.simple_event()
        alarms = extract_alarm_triggers(raw, "simple-event-1@example.com")
        self.assertEqual(alarms, [])

    def test_wrong_uid_returns_empty(self) -> None:
        raw = corpus.event_with_display_alarm()
        alarms = extract_alarm_triggers(raw, "no-such-uid@example.com")
        self.assertEqual(alarms, [])

    def test_invalid_ics_returns_empty(self) -> None:
        alarms = extract_alarm_triggers(b"NOT ICS", "any-uid@example.com")
        self.assertEqual(alarms, [])


class ExtractAttendeesTest(unittest.TestCase):
    def test_attendee_emails_extracted_from_event(self) -> None:
        raw = corpus.event_with_attendees()
        attendees = extract_attendees(raw, "attendees-1@example.com")
        self.assertEqual(attendees, ("alice@example.com", "bob@example.com"))

    def test_organizer_email_extracted_from_event(self) -> None:
        raw = corpus.event_with_attendees()
        self.assertEqual(
            extract_organizer(raw, "attendees-1@example.com"), "host@example.com"
        )

    def test_valarm_attendee_is_not_treated_as_event_attendee(self) -> None:
        raw = corpus.event_with_email_alarm()
        self.assertEqual(extract_attendees(raw, "alarm-email-1@example.com"), ())
