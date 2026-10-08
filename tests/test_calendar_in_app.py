"""Mail and calendar as one application.

Covers the three things that make them one program rather than two:
switching between the full-screen subsystems, the single notification
space both announce into, and the line each half shows about the other.

The Pilot-driven flows are plain ``async def`` functions, as everywhere
else in this suite; the pure helpers are ``unittest`` classes.
"""

from __future__ import annotations

import threading
import unittest
import unittest.mock
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from textual.pilot import Pilot
from textual.widgets import Label, Static
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


async def test_f2_is_ignored_inside_a_calendar_dialog() -> None:
    """The agenda is open under its own dialog; neither answer is right."""
    app, *_ = build_pony_app(
        label="f2-dialog",
        calendar=make_calendar_runtime(make_tmp_paths("f2-dialog")),
    )
    async with app.run_test() as pilot:
        await pilot.press("f2")
        await pilot.pause()
        await pilot.press("f1")  # the calendar's help screen
        await pilot.pause()
        depth = len(app.screen_stack)
        await pilot.press("f2")
        await pilot.pause()
        assert len(app.screen_stack) == depth


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


async def test_the_mail_help_panel_lists_the_calendar_keys() -> None:
    """A key nobody can find is a key that does not exist."""
    app, *_ = build_pony_app(
        label="help-mail", calendar=make_calendar_runtime(make_tmp_paths("help-mail"))
    )
    async with app.run_test() as pilot:
        await pilot.press("f1")
        await pilot.pause()
        text = " ".join(str(widget.render()) for widget in app.screen.query(Static))
        assert "F2" in text
        assert "i" in text
        assert "invitation" in text.lower()


async def test_the_calendar_help_panel_lists_the_way_back() -> None:
    app, *_ = build_pony_app(
        label="help-cal", calendar=make_calendar_runtime(make_tmp_paths("help-cal"))
    )
    async with app.run_test() as pilot:
        await pilot.press("f2")
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        text = " ".join(str(widget.render()) for widget in app.screen.query(Static))
        assert "f2" in text.lower()
        assert "Mail" in text


def test_the_calendar_help_takes_its_key_from_the_real_binding() -> None:
    """The help cannot name a key that moved: it is read off BINDINGS."""
    from textual.binding import Binding

    from pony.tui.app import PonyApp

    switch = [
        b
        for b in PonyApp.BINDINGS
        if isinstance(b, Binding) and b.action == "toggle_calendar"
    ]
    assert len(switch) == 1
    assert switch[0].key == "f2"
    # The description serves both footers and both help screens, so it
    # names the pair rather than one destination.
    assert "Mail" in switch[0].description
    assert "Calendar" in switch[0].description


async def test_a_calendarless_app_offers_no_switch_in_the_calendar_help() -> None:
    """Standalone, there is nothing to switch to, so nothing is listed."""
    from chronos.tui.app import TuiServices

    runtime = make_calendar_runtime(make_tmp_paths("help-standalone"))
    services = TuiServices(
        config=runtime.config,
        mirror=runtime.mirror,
        index=runtime.index,
        creds=runtime.credentials,
    )
    assert services.host_bindings == ()
    runtime.close()


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


async def test_the_clock_can_be_pinned() -> None:
    """One injectable clock decides what is next and what is due.

    The documentation screenshots rely on it: without a fixed clock the
    agenda would open on a day with nothing on it, and the next-event
    line would have nothing to name.
    """
    pinned = datetime(2026, 6, 23, 11, 30, tzinfo=UTC)
    uid = "standup@example.com"
    runtime = make_calendar_runtime(
        make_tmp_paths("pinned"),
        events=((uid, _event_ics(uid, "Standup", pinned + timedelta(minutes=30))),),
    )
    app, *_ = build_pony_app(label="pinned", calendar=runtime, now=lambda: pinned)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Relative to the pinned clock, not to the wall clock — against
        # which this event is years in the past.
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


async def _until(pilot: Pilot[None], check: Callable[[], bool], message: str) -> None:
    """Pump the app until `check` holds — the syncs are threads now."""
    for _ in range(200):
        if check():
            return
        await pilot.pause(0.01)
    raise AssertionError(message)


async def test_b_reaches_the_same_contact_browser_from_either_half() -> None:
    """The point of one process: one `B`, one list of people.

    The browser is a mail-side screen and the calendar may not import one,
    so the host hands the calendar an opener. Both keys must land on the
    very same screen class.
    """
    from pony.tui.screens.contact_browser_screen import ContactBrowserScreen

    runtime = make_calendar_runtime(make_tmp_paths("contacts-both"))
    app, *_ = build_pony_app(
        label="contacts-both", calendar=runtime, with_contacts=True
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, MainScreen)
        await pilot.press("B")
        await pilot.pause()
        self_from_mail = type(pilot.app.screen)
        self_from_mail_is_browser = isinstance(pilot.app.screen, ContactBrowserScreen)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(pilot.app.screen, CalendarScreen)
        await pilot.press("B")
        await pilot.pause()
        assert self_from_mail_is_browser, (
            "B did not open the browser in the mail reader"
        )
        assert isinstance(pilot.app.screen, ContactBrowserScreen), (
            "B did not open the browser in the agenda"
        )
        assert type(pilot.app.screen) is self_from_mail


async def test_the_calendar_says_so_when_there_are_no_contacts() -> None:
    """A calendar hosted without a contact store still answers the key."""
    runtime = make_calendar_runtime(make_tmp_paths("contacts-none"))
    app, *_ = build_pony_app(label="contacts-none", calendar=runtime)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        await pilot.press("B")
        await pilot.pause()
        assert any("No contacts store available." in m for m in _toast_messages(app)), (
            _toast_messages(app)
        )


async def test_both_syncs_run_on_threads_the_app_owns() -> None:
    """Neither cadence belongs to a screen, so neither depends on one.

    A Textual timer dies with the screen that armed it, and the agenda is
    unmounted every time F2 goes back to the mail reader. Both syncs are
    threads the application starts instead.
    """
    import dataclasses

    runtime = make_calendar_runtime(make_tmp_paths("bg-sync"))
    app, cfg, *_ = build_pony_app(label="bg-sync", calendar=runtime)
    app._config = dataclasses.replace(cfg, background_sync_enabled=True)

    async with app.run_test() as pilot:
        await pilot.pause()
        mail, calendar = app.mail_sync, app.calendar_services.sync_scheduler
        assert mail is not None and calendar is not None
        assert mail.started, "the mail reader's own sync should be running"
        assert calendar.started, "the calendar's sync should be running"

    # And both are stopped with the application.
    assert not mail.started
    assert not calendar.started


async def test_the_calendar_keeps_its_countdown_across_f2() -> None:
    """Switching halves must not restart the calendar's clock.

    The agenda's timer used to be armed in its `on_mount`, so every visit
    began the wait again — at the hourly default, a sync that never came.
    """
    runtime = make_calendar_runtime(make_tmp_paths("f2-countdown"))
    app, *_ = build_pony_app(label="f2-countdown", calendar=runtime)

    async with app.run_test() as pilot:
        await pilot.pause()
        scheduler = app.calendar_services.sync_scheduler
        assert scheduler is not None
        before = scheduler.next_run_at
        assert before is not None

        await pilot.press("f2")  # into the agenda
        await pilot.pause()
        await pilot.press("f2")  # and back to the mail reader
        await pilot.pause()
        await pilot.press("f2")  # and in again
        await pilot.pause()

        # Same deadline throughout: the thread never noticed.
        assert scheduler.next_run_at == before


async def test_the_mail_sync_runs_while_the_agenda_is_in_front() -> None:
    """Reading the agenda does not stop mail arriving."""
    from pony.sync import ImapSyncService

    runtime = make_calendar_runtime(make_tmp_paths("mail-under-agenda"))
    app, *_ = build_pony_app(label="mail-under-agenda", calendar=runtime)
    synced = threading.Event()

    def _sync(_self: ImapSyncService, **_kwargs: object) -> object:
        synced.set()
        raise RuntimeError("stop here: the sync ran, which is the point")

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(pilot.app.screen, CalendarScreen)

        scheduler = app.mail_sync
        assert scheduler is not None
        with unittest.mock.patch.object(ImapSyncService, "sync", _sync):
            scheduler.start()
            assert scheduler.request_now()
            await _until(pilot, synced.is_set, "mail did not sync under the agenda")


async def test_the_calendar_sync_runs_while_the_mail_reader_is_in_front() -> None:
    """A reminder can only fire for an event the local cache knows about."""
    runtime = make_calendar_runtime(make_tmp_paths("cal-under-mail"))
    app, *_ = build_pony_app(label="cal-under-mail", calendar=runtime)
    ran = threading.Event()

    def _runner(**_kwargs: object) -> tuple[object, ...]:
        ran.set()
        return ()

    with unittest.mock.patch(
        "pony.tui.calendar_host.build_sync_runner", return_value=_runner
    ):
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(pilot.app.screen, MainScreen)
            scheduler = app.calendar_services.sync_scheduler
            assert scheduler is not None
            assert scheduler.request_now()
            await _until(pilot, ran.is_set, "the calendar never synced")


async def test_a_failing_background_sync_does_not_reach_the_user() -> None:
    runtime = make_calendar_runtime(make_tmp_paths("bg-sync-fail"))
    app, *_ = build_pony_app(label="bg-sync-fail", calendar=runtime)
    tried = threading.Event()

    def _explode(**_kwargs: object) -> tuple[object, ...]:
        tried.set()
        raise RuntimeError("the server is down")

    with unittest.mock.patch(
        "pony.tui.calendar_host.build_sync_runner", return_value=_explode
    ):
        async with app.run_test() as pilot:
            await pilot.pause()
            scheduler = app.calendar_services.sync_scheduler
            assert scheduler is not None
            assert scheduler.request_now()
            await _until(pilot, tried.is_set, "the sync never ran")
            await _until(pilot, lambda: not scheduler.running, "the sync never ended")
            for _ in range(5):
                await pilot.pause()
            # Reported to the log, not as a toast: the user did not ask, and
            # the agenda is not even mounted to report it to.
            assert "the server is down" not in " ".join(_toast_messages(app))
            # The slot is free again, so the next run can try.
            assert not scheduler.running


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
