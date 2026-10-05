"""Mail and calendar as one application.

Covers the three things that make them one program rather than two:
switching between the full-screen subsystems, the single notification
space both announce into, and the line each half shows about the other.

The Pilot-driven flows are plain ``async def`` functions, as everywhere
else in this suite; the pure helpers are ``unittest`` classes.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from textual.widgets import Label
from tui_helpers import build_pony_app, make_calendar_runtime, make_tmp_paths

from chronos.tui.app import TuiServices
from chronos.tui.screens.main_screen import MainScreen as CalendarScreen
from pony.calendar import CalendarRuntime
from pony.domain import FolderRef
from pony.notifications import NotificationSource
from pony.tui.calendar_host import (
    AlarmPoller,
    build_calendar_services,
    mail_arrival_notification,
    mail_status,
    next_event_status,
)
from pony.tui.screens.main_screen import MainScreen

_NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)

_UNREAD = (
    b"From: her@example.com\r\nTo: me@example.com\r\nSubject: Hello\r\n\r\nBody\r\n"
)


def _event_ics(
    uid: str,
    summary: str,
    start: datetime,
    *,
    minutes: int = 30,
    alarm_minutes_before: int | None = None,
) -> bytes:
    """One VEVENT, optionally with a VALARM relative to its start."""
    end = start + timedelta(minutes=minutes)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//tests//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{start.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{summary}",
    ]
    if alarm_minutes_before is not None:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"TRIGGER:-PT{alarm_minutes_before}M",
            f"DESCRIPTION:{summary} soon",
            "END:VALARM",
        ]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _one_event(label: str, summary: str, start: datetime) -> CalendarRuntime:
    uid = f"{summary.lower()}@example.com"
    return make_calendar_runtime(
        make_tmp_paths(label), events=((uid, _event_ics(uid, summary, start)),)
    )


def _services_for(runtime: CalendarRuntime) -> TuiServices:
    """A services bundle for the status helpers, with no app hosting it."""
    return TuiServices(
        config=runtime.config,
        mirror=runtime.mirror,
        index=runtime.index,
        creds=runtime.credentials,
    )


def _toast_titles(app: object) -> list[str]:
    """Titles of the toasts Textual is currently showing."""
    return [n.title for n in app._notifications]  # type: ignore[attr-defined] # noqa: SLF001


def _toast_messages(app: object) -> list[str]:
    return [n.message for n in app._notifications]  # type: ignore[attr-defined] # noqa: SLF001


# ---------------------------------------------------------------------------
# Switching between the two subsystems
# ---------------------------------------------------------------------------


async def test_f2_opens_the_agenda_and_f2_returns_to_mail() -> None:
    app, *_ = build_pony_app(
        label="switch", calendar=make_calendar_runtime(make_tmp_paths("switch"))
    )
    async with app.run_test() as pilot:
        assert isinstance(app.screen, MainScreen)
        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(app.screen, CalendarScreen)
        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_the_header_names_the_subsystem_in_front() -> None:
    app, *_ = build_pony_app(
        label="subtitle", calendar=make_calendar_runtime(make_tmp_paths("subtitle"))
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.sub_title == "acct/INBOX"
        await pilot.press("f2")
        await pilot.pause()
        assert app.sub_title == "Calendar"
        await pilot.press("f2")
        await pilot.pause()
        assert app.sub_title == "acct/INBOX"


async def test_the_mail_screen_survives_the_round_trip() -> None:
    """The agenda is pushed over the mail screen, not in place of it.

    Coming back therefore finds the same screen instance — same folder,
    same cursor row, same reader scroll position.
    """
    app, *_ = build_pony_app(
        label="survive", calendar=make_calendar_runtime(make_tmp_paths("survive"))
    )
    async with app.run_test() as pilot:
        before = app.screen
        await pilot.press("f2")
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        assert before is app.screen


async def test_without_a_calendar_f2_says_so() -> None:
    app, *_ = build_pony_app(label="nocal")
    async with app.run_test() as pilot:
        assert not app.calendar_available
        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        messages = _toast_messages(app)
        assert any("No calendar configured" in m for m in messages), messages


async def test_asking_for_calendar_services_without_one_is_an_error() -> None:
    app, *_ = build_pony_app(label="noservices")
    async with app.run_test():
        try:
            _ = app.calendar_services
        except RuntimeError:
            return
        raise AssertionError("expected RuntimeError")


# ---------------------------------------------------------------------------
# What each half says about the other
# ---------------------------------------------------------------------------


async def test_the_agenda_shows_unread_mail() -> None:
    app, *_ = build_pony_app(
        label="agenda-status",
        seed=((FolderRef("acct", "INBOX"), _UNREAD),),
        calendar=make_calendar_runtime(make_tmp_paths("agenda-status")),
    )
    async with app.run_test() as pilot:
        await pilot.press("f2")
        await pilot.pause()
        label = app.screen.query_one("#companion-status", Label)
        assert "1 unread" in str(label.render())


async def test_the_mail_reader_shows_the_next_event() -> None:
    soon = datetime.now(UTC) + timedelta(minutes=45)
    app, *_ = build_pony_app(
        label="mail-status", calendar=_one_event("mail-status", "Standup", soon)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # Beside the open folder, not instead of it.
        assert "acct/INBOX" in app.sub_title
        assert "Standup" in app.sub_title


async def test_an_empty_calendar_leaves_the_folder_context_alone() -> None:
    """With nothing scheduled the mail reader shows only its folder."""
    app, *_ = build_pony_app(
        label="mail-plain", calendar=make_calendar_runtime(make_tmp_paths("mail-plain"))
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.sub_title == "acct/INBOX"


# ---------------------------------------------------------------------------
# One notification space
# ---------------------------------------------------------------------------


async def test_a_reminder_is_shown_while_reading_mail() -> None:
    app, *_ = build_pony_app(
        label="reminder-mail",
        calendar=make_calendar_runtime(make_tmp_paths("reminder-mail")),
    )
    async with app.run_test() as pilot:
        app.notifications.announce(
            NotificationSource.CALENDAR,
            "Standup",
            "Starts 09:45",
            at=_NOW,
            urgent=True,
        )
        await pilot.pause()
        assert "Standup" in _toast_titles(app)


async def test_a_reminder_is_shown_in_the_agenda_too() -> None:
    """Urgent announcements ignore which subsystem is in front."""
    app, *_ = build_pony_app(
        label="reminder-agenda",
        calendar=make_calendar_runtime(make_tmp_paths("reminder-agenda")),
    )
    async with app.run_test() as pilot:
        await pilot.press("f2")
        await pilot.pause()
        app.notifications.announce(
            NotificationSource.CALENDAR,
            "Standup",
            "Starts 09:45",
            at=_NOW,
            urgent=True,
        )
        await pilot.pause()
        assert "Standup" in _toast_titles(app)


async def test_new_mail_is_not_toasted_while_reading_mail() -> None:
    """The mail screen reports its own sync results; a second toast is noise."""
    app, *_ = build_pony_app(
        label="mail-in-mail",
        calendar=make_calendar_runtime(make_tmp_paths("mail-in-mail")),
    )
    async with app.run_test() as pilot:
        app.notifications.announce(
            NotificationSource.MAIL, "2 new messages", "work: 2", at=_NOW
        )
        await pilot.pause()
        assert "2 new messages" not in _toast_titles(app)
        # Recorded even though it was not shown.
        latest = app.notifications.latest(source=NotificationSource.MAIL)
        assert latest is not None
        assert latest.title == "2 new messages"


async def test_new_mail_is_toasted_while_in_the_agenda() -> None:
    app, *_ = build_pony_app(
        label="mail-in-agenda",
        calendar=make_calendar_runtime(make_tmp_paths("mail-in-agenda")),
    )
    async with app.run_test() as pilot:
        await pilot.press("f2")
        await pilot.pause()
        app.notifications.announce(
            NotificationSource.MAIL, "2 new messages", "work: 2", at=_NOW
        )
        await pilot.pause()
        assert "2 new messages" in _toast_titles(app)


async def test_a_due_reminder_is_announced_by_the_poller_on_the_app() -> None:
    """The app's own tick is what turns a due alarm into a toast."""
    start = datetime.now(UTC) + timedelta(minutes=5)
    runtime = make_calendar_runtime(
        make_tmp_paths("tick"),
        events=(
            (
                "standup@example.com",
                _event_ics(
                    "standup@example.com", "Standup", start, alarm_minutes_before=10
                ),
            ),
        ),
    )
    app, *_ = build_pony_app(label="tick", calendar=runtime)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Standup" in _toast_titles(app)


async def test_the_calendar_syncs_while_the_mail_reader_is_in_front() -> None:
    """A reminder can only fire for an event the local cache knows about."""
    runtime = make_calendar_runtime(make_tmp_paths("bg-sync"))
    app, *_ = build_pony_app(label="bg-sync", calendar=runtime)
    runs: list[int] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        services = app.calendar_services
        services.sync_runner = lambda **_kwargs: runs.append(1) or ()  # type: ignore[assignment,func-returns-value]
        app._calendar_sync_tick()  # noqa: SLF001
        await pilot.pause()
        await pilot.pause()
        assert runs, "the mail reader should keep the calendar synced"


async def test_the_agenda_syncs_itself_while_it_is_open() -> None:
    """Two periodic syncs would only contend for the calendar's lockfile."""
    runtime = make_calendar_runtime(make_tmp_paths("bg-sync-agenda"))
    app, *_ = build_pony_app(label="bg-sync-agenda", calendar=runtime)
    runs: list[int] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        services = app.calendar_services
        services.sync_runner = lambda **_kwargs: runs.append(1) or ()  # type: ignore[assignment,func-returns-value]
        await pilot.press("f2")
        await pilot.pause()
        app._calendar_sync_tick()  # noqa: SLF001
        await pilot.pause()
        assert not runs


async def test_a_failing_background_sync_does_not_reach_the_user() -> None:
    runtime = make_calendar_runtime(make_tmp_paths("bg-sync-fail"))
    app, *_ = build_pony_app(label="bg-sync-fail", calendar=runtime)

    def _explode(**_kwargs: object) -> tuple[object, ...]:
        raise RuntimeError("the server is down")

    async with app.run_test() as pilot:
        await pilot.pause()
        app.calendar_services.sync_runner = _explode  # type: ignore[assignment]
        app._calendar_sync_tick()  # noqa: SLF001
        await pilot.pause()
        await pilot.pause()
        # Reported to the log, not as a toast: the user did not ask.
        assert "the server is down" not in " ".join(_toast_messages(app))
        # And the guard is released, so the next tick can try again.
        assert not app._calendar_syncing  # noqa: SLF001


async def test_the_bundle_carries_the_runtime_and_a_sync_runner() -> None:
    runtime = make_calendar_runtime(make_tmp_paths("services"))
    app, *_ = build_pony_app(label="services")
    async with app.run_test():
        services = build_calendar_services(runtime, host=app)
        assert services.config is runtime.config
        assert services.index is runtime.index
        assert services.mirror is runtime.mirror
        assert services.sync_runner is not None
        # A fresh credentials provider, not the runtime's: this one can
        # push an OAuth screen onto the running application.
        assert services.creds is not runtime.credentials
    runtime.close()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class AlarmPollerTest(unittest.TestCase):
    def _runtime_with_alarm(self, label: str) -> CalendarRuntime:
        start = datetime.now(UTC) + timedelta(minutes=5)
        runtime = make_calendar_runtime(
            make_tmp_paths(label),
            events=(
                (
                    "standup@example.com",
                    _event_ics(
                        "standup@example.com",
                        "Standup",
                        start,
                        alarm_minutes_before=10,
                    ),
                ),
            ),
        )
        self.addCleanup(runtime.close)
        return runtime

    def test_a_due_alarm_becomes_an_urgent_announcement(self) -> None:
        poller = AlarmPoller(self._runtime_with_alarm("alarms").index)
        due = poller.due(datetime.now(UTC))
        self.assertEqual(1, len(due))
        self.assertEqual("Standup", due[0].title)
        self.assertTrue(due[0].urgent)
        self.assertIs(NotificationSource.CALENDAR, due[0].source)

    def test_an_alarm_is_announced_only_once(self) -> None:
        poller = AlarmPoller(self._runtime_with_alarm("alarms-once").index)
        now = datetime.now(UTC)
        self.assertEqual(1, len(poller.due(now)))
        self.assertEqual((), poller.due(now))

    def test_no_alarms_means_no_announcements(self) -> None:
        runtime = make_calendar_runtime(make_tmp_paths("alarms-none"))
        self.addCleanup(runtime.close)
        self.assertEqual((), AlarmPoller(runtime.index).due(datetime.now(UTC)))


class NextEventStatusTest(unittest.TestCase):
    def test_empty_with_nothing_scheduled(self) -> None:
        runtime = make_calendar_runtime(make_tmp_paths("next-empty"))
        self.addCleanup(runtime.close)
        self.assertEqual(
            "", next_event_status(_services_for(runtime), now=datetime.now(UTC))
        )

    def test_names_the_summary_and_the_time(self) -> None:
        soon = datetime.now(UTC) + timedelta(hours=2)
        runtime = _one_event("next-one", "Review", soon)
        self.addCleanup(runtime.close)
        status = next_event_status(_services_for(runtime), now=datetime.now(UTC))
        self.assertIn("Review", status)
        self.assertIn(soon.astimezone().strftime("%H:%M"), status)
        self.assertTrue(status.startswith("Next:"))

    def test_uses_a_glyph_when_utf8_is_allowed(self) -> None:
        soon = datetime.now(UTC) + timedelta(hours=2)
        runtime = _one_event("next-utf8", "Review", soon)
        self.addCleanup(runtime.close)
        status = next_event_status(
            _services_for(runtime), now=datetime.now(UTC), use_utf8=True
        )
        self.assertTrue(status.startswith("◷"), status)

    def test_an_event_already_over_is_not_the_next_one(self) -> None:
        runtime = _one_event("next-past", "Old", datetime.now(UTC) - timedelta(hours=3))
        self.addCleanup(runtime.close)
        self.assertEqual(
            "", next_event_status(_services_for(runtime), now=datetime.now(UTC))
        )

    def test_an_event_in_progress_wins_over_a_later_one(self) -> None:
        now = datetime.now(UTC)
        runtime = make_calendar_runtime(
            make_tmp_paths("next-running"),
            events=(
                (
                    "running@example.com",
                    _event_ics(
                        "running@example.com", "Running", now - timedelta(minutes=5)
                    ),
                ),
                (
                    "later@example.com",
                    _event_ics("later@example.com", "Later", now + timedelta(hours=4)),
                ),
            ),
        )
        self.addCleanup(runtime.close)
        self.assertIn("Running", next_event_status(_services_for(runtime), now=now))


class MailStatusTest(unittest.TestCase):
    def test_empty_with_nothing_unread(self) -> None:
        _app, config, _paths, index, _mirrors = build_pony_app(label="mail-none")
        self.assertEqual("", mail_status(index, config))

    def test_counts_unread_messages(self) -> None:
        _app, config, _paths, index, _mirrors = build_pony_app(
            label="mail-one", seed=((FolderRef("acct", "INBOX"), _UNREAD),)
        )
        self.assertEqual("Mail: 1 unread", mail_status(index, config))
        self.assertTrue(mail_status(index, config, use_utf8=True).startswith("✉"))


class MailArrivalNotificationTest(unittest.TestCase):
    def test_nothing_fetched_means_no_announcement(self) -> None:
        self.assertIsNone(mail_arrival_notification([("work", 0)], now=_NOW))
        self.assertIsNone(mail_arrival_notification([], now=_NOW))

    def test_one_message_is_singular(self) -> None:
        note = mail_arrival_notification([("work", 1)], now=_NOW)
        assert note is not None
        self.assertEqual("1 new message", note.title)
        self.assertEqual("work: 1", note.body)
        self.assertFalse(note.urgent)

    def test_counts_are_totalled_across_accounts(self) -> None:
        note = mail_arrival_notification(
            [("work", 2), ("home", 0), ("list", 3)], now=_NOW
        )
        assert note is not None
        self.assertEqual("5 new messages", note.title)
        self.assertEqual("work: 2, list: 3", note.body)
        self.assertIs(NotificationSource.MAIL, note.source)


if __name__ == "__main__":
    unittest.main()
