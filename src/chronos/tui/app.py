from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from textual import work
from textual.app import App

from chronos.domain import AlarmRecord, AppConfig, SyncResult
from chronos.protocols import (
    CalendarSyncOutcome,
    CalendarSyncScheduler,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from chronos.scheduler import PeriodicSync
from chronos.tui.bindings import BindingType
from chronos.tui.screens.main_screen import MainScreen
from chronos.tui.terminal import (
    osc777_notification,
    pop_terminal_title,
    push_terminal_title,
    set_terminal_title,
)

logger = logging.getLogger(__name__)

_ALARM_POLL_SECS = 30.0
# Reminders stay on screen long enough to be seen after looking away.
_ALARM_TOAST_SECS = 120.0
_ALARM_LOOKBACK = timedelta(minutes=15)

# Built-in Textual theme chosen when the user has not set one (config /
# --theme). flexoki is near-black-on-near-white, the highest-contrast of
# the bundled themes; override per-user via config or the --theme flag.
DEFAULT_THEME = "flexoki"


class AttendeeCompleter(Protocol):
    """Suggests attendee addresses as the user types one.

    Called with what has been typed so far; returns ready-to-insert
    address strings, best match first. Pony Express answers from the
    contact index its own composer completes from, which is the point of
    the two halves sharing a process: an invitation goes to the people
    already in the user's mail.
    """

    def __call__(self, prefix: str, *, limit: int = 10) -> Sequence[str]: ...


class InvitationSender(Protocol):
    """Mails an invitation for an event that has just been saved.

    Supplied by a host application that can send mail — Pony Express
    does, with the calendar inside it. `None` on `TuiServices` means the
    calendar has no way to post an invitation, and saving an event with
    attendees simply records them.

    Failures are the sender's own to report: saving the event has
    already succeeded by the time this is called, and a bounced
    invitation must not read as a lost event.
    """

    def __call__(
        self,
        *,
        ics: bytes,
        attendees: Sequence[str],
        organizer: str | None,
        summary: str,
        is_update: bool,
    ) -> None: ...


SyncRunner = Callable[..., Sequence[SyncResult]]
"""Runs every configured account's sync.

Called as `runner()` for a one-shot run, or `runner(cancel_event=evt)`
to allow the caller (the TUI worker) to interrupt mid-flight. The
runtime accepts the kwarg unconditionally; the simple test fakes that
ignore it are valid implementations of the Protocol.
"""


@dataclass
class TuiServices:
    """Dependencies the TUI needs.

    Constructed by the CLI entry-point and handed to `ChronosApp`. Tests
    inject fakes for everything except `now`. `sync_runner` is `None`
    when the TUI was launched from a context that has not wired sync
    in (most TUI tests); pressing the sync key in that mode shows a
    notification rather than running anything.
    """

    config: AppConfig
    mirror: MirrorRepository
    index: IndexRepository
    creds: CredentialsProvider
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sync_runner: SyncRunner | None = None
    startup_ics_path: Path | None = None
    # Set by a host that can send mail; see `InvitationSender`.
    invitation_sender: InvitationSender | None = None
    # Completes attendee addresses in the event editor. Set by a host
    # with a contact store — Pony Express passes the same source its own
    # composer completes from. None falls back to a plain text field.
    attendee_completer: AttendeeCompleter | None = None
    # Keys the host binds that are worth listing on the calendar's help
    # screen — the one that switches back to the other half of the
    # program. Empty when the calendar runs on its own and there is
    # nothing to switch to.
    host_bindings: Sequence[BindingType] = ()
    # The periodic sync, owned by whichever application is hosting —
    # `ChronosApp` standalone, `PonyApp` inside the mail client. It runs
    # on its own thread, so it outlives the agenda screen being popped;
    # `MainScreen` only observes its countdown and asks it for a run.
    # None when sync is not wired in (most TUI tests), exactly like
    # `sync_runner`.
    sync_scheduler: CalendarSyncScheduler | None = None


@runtime_checkable
class CalendarHost(Protocol):
    """A Textual app the calendar screens can live on.

    `MainScreen` reads its dependencies off `self.app`, so whatever app
    hosts it has to offer them. `ChronosApp` does when the calendar runs
    on its own, and Pony Express's `PonyApp` does when mail and calendar
    run as one program.
    """

    @property
    def calendar_services(self) -> TuiServices: ...


# The calendar's stylesheet, kept out of the app class so a host
# application can adopt it (`App.CSS` is global, and the calendar
# widgets only ever appear under the calendar's own screens).
CALENDAR_CSS = """
    #main-body { height: 1fr; }
    CalendarPanel { width: 30; border-right: solid $accent; }
    #centre-pane { width: 1fr; }
    #title-row { height: 1; }
    #view-title { width: 1fr; padding: 0 1; color: $text-muted; }
    /* Host-supplied state of the other half of the program; empty and
       therefore invisible when the calendar runs on its own. */
    #companion-status { width: auto; padding: 0 1; color: $text-muted; }
    #sync-status { width: auto; padding: 0 1; color: $text-muted; }
    EventList { height: 2fr; }
    /* The timeline takes the full centre-pane height in Day / Grid
       views — `MainScreen.refresh_view` toggles its `display` along
       with EventList / EventView based on the active view. */
    TimelineGrid { height: 1fr; background: $background; }
    MonthGrid { height: 1fr; background: $background; }
    #detail-pane {
        height: 1fr;
        border-top: solid $accent;
        padding: 1;
    }
    #event-edit, #search-dialog {
        padding: 1;
    }
    .event-edit-title { text-style: bold; margin-bottom: 1; }
    .event-field-row, .event-datetime-row {
        height: 1;
        margin-bottom: 1;
        align-vertical: middle;
    }
    .event-field-label { width: 14; }
    .event-field-control { width: 1fr; }
    .event-date { width: 18; margin-right: 1; }
    .event-time { width: 14; }
    #edit-error { color: $error; }

    /* Modal dialogs: `align: center middle;` on the screen itself is
       Textual's stock idiom for centring a single child container.
       The dialog box then carries an explicit width so the centring
       has something to act on (auto-width inside a flex parent
       expands to fill, defeating the centre rule). */
    SyncConfirmScreen, ConfirmScreen, SyncProgressScreen,
    EventDetailScreen, OAuthProgressScreen, ImportIcsScreen, GotoScreen {
        align: center middle;
    }
    .dialog-box {
        padding: 1 2;
        height: auto;
        border: solid $primary;
    }
    .dialog-box .dialog-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .dialog-box .dialog-actions {
        margin-top: 1;
        align-horizontal: right;
        /* `height: 1` matches the compact single-row buttons below;
           an explicit height keeps the action row from being squashed
           to zero when the dialog body is tall (e.g. a RichLog above)
           under the parent's `height: auto`. */
        height: 1;
    }
    /* Compact, borderless buttons in the Pony style: a single text row
       with the variant colour as background, no chunky Textual border. */
    .dialog-box .dialog-actions Button {
        margin: 0 1;
        height: 1;
        min-width: 0;
        border: none;
        padding: 0 1;
    }
    .dialog-box .dialog-actions Button:focus {
        text-style: reverse bold;
    }
    /* Per-dialog width and height overrides. */
    #event-detail    { width: 80; max-height: 80%; }
    #sync-confirm-box { width: 80; }
    #confirm-box     { width: 60; }
    #goto-box        { width: 60; }
    #goto-error      { color: $error; }
    #import-ics-box  { width: 80; }
    #sync-progress-box { width: 100; max-height: 80%; }
    #oauth-box       { width: 70; }
    #oauth-status    { margin-bottom: 1; }
    /* Scrollable progress log: bordered, scrolls automatically as
       new lines come in via `RichLog.write`. */
    #sync-progress-log {
        height: 18;
        padding: 0 1;
    }
#sync-progress-summary {
    margin-top: 1;
}
"""


class ChronosApp(App[None]):
    """Top-level Textual app for the calendar on its own.

    All real logic lives in `MainScreen`; the app is just a host. We
    push the main screen on mount instead of in `compose` so the
    constructor runs synchronously without touching any I/O.

    Reached with `pony calendar` and with `pony calendar tui`. Inside
    the mail client the same screens run on `PonyApp` instead, so what
    the user sees is one product either way — hence the shared name in
    the header and the terminal title.
    """

    TITLE = "Pony Express"
    SUB_TITLE = "Calendar"

    # Ctrl-P opens Textual's built-in command palette, which includes the
    # "Change theme" picker — the live in-app theme switcher. Kept enabled
    # so users can raise contrast on the fly; the F1 help screen documents
    # it alongside chronos's own keybindings.
    ENABLE_COMMAND_PALETTE = True

    CSS = CALENDAR_CSS

    def __init__(self, services: TuiServices, theme_name: str | None = None) -> None:
        super().__init__()
        self.services = services
        # The CLI resolves a concrete built-in theme (config / --theme /
        # DEFAULT_THEME), so a running app always has an explicit theme.
        # Tests that construct ChronosApp(services) keep Textual's default.
        if theme_name is not None:
            self.theme = theme_name

    @property
    def calendar_services(self) -> TuiServices:
        """Satisfies `CalendarHost` — see that protocol."""
        return self.services

    def on_mount(self) -> None:
        push_terminal_title()
        set_terminal_title(
            f"Pony Express — Calendar {datetime.now().strftime('%d/%m/%Y')}"
        )
        self.push_screen(MainScreen())  # pyright: ignore[reportUnknownMemberType]
        if not self.is_headless:
            self._start_mcp_server()
        self.set_interval(_ALARM_POLL_SECS, self._fire_pending_alarms, name="alarms")
        self._start_sync_scheduler()

    def on_unmount(self) -> None:
        if self.services.sync_scheduler is not None:
            self.services.sync_scheduler.stop()
        pop_terminal_title()

    # Background sync ----------------------------------------------------------

    def _start_sync_scheduler(self) -> None:
        """Give the calendar its periodic sync, on a thread of its own.

        The app owns it, not `MainScreen`: a screen's timer dies when the
        screen is popped, and the agenda is popped whenever the user looks
        at something else. The screen finds it on `TuiServices` and only
        observes it.

        Config-gated exactly as before — `background_sync_enabled = false`
        leaves the thread unstarted, and the sync key starts it.
        """
        runner = self.services.sync_runner
        if runner is None:
            return
        scheduler: CalendarSyncScheduler = PeriodicSync(
            name="calendar-sync",
            interval=self.services.config.background_sync_interval_seconds,
            runner=runner,
            on_started=self._on_sync_started,
            on_finished=self._on_sync_finished,
        )
        self.services.sync_scheduler = scheduler
        if self.services.config.background_sync_enabled:
            scheduler.start()

    def _on_sync_started(self) -> None:
        """Scheduler thread: tell the agenda, if anyone is looking at it."""
        self._on_ui_thread(lambda screen: screen.on_sync_started())

    def _on_sync_finished(self, outcome: CalendarSyncOutcome) -> None:
        """Scheduler thread: hand the outcome to the agenda."""
        self._on_ui_thread(lambda screen: screen.on_sync_finished(outcome))

    def _on_ui_thread(self, action: Callable[[MainScreen], None]) -> None:
        """Run `action` against the agenda screen on the UI thread.

        Called from the scheduler thread, so everything hops through
        `call_from_thread`; a `RuntimeError` means the application is
        already gone, which at shutdown is ordinary.
        """
        with contextlib.suppress(RuntimeError):
            self.call_from_thread(self._apply_to_main_screen, action)

    def _apply_to_main_screen(self, action: Callable[[MainScreen], None]) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen):
                action(screen)
                return

    @work(exclusive=False, name="mcp-server", exit_on_error=False)
    async def _start_mcp_server(self) -> None:
        """Start the MCP TCP server on an ephemeral port.

        Runs for the lifetime of the TUI on the app's own event loop
        (no separate thread, per ai/MCP.md).  Failure is non-fatal:
        the TUI continues normally without MCP.
        """
        from chronos.mcp_server import start_tcp_server

        try:
            await start_tcp_server(
                index=self.services.index,
                mirror=self.services.mirror,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"MCP TCP server stopped: {exc}")

    def _fire_pending_alarms(self) -> None:
        """Announce every alarm that fell due since the last poll.

        Runs every `_ALARM_POLL_SECS`. Alarms are delivered through the
        terminal, so they reach the user wherever the TUI is displayed —
        including over SSH, where a desktop notification would land on
        the remote machine: an in-app toast, the terminal bell, and an
        OSC 777 notification for terminals that turn it into a desktop
        one (terminals without support ignore it).
        """
        now = self.services.now()
        try:
            pending = self.services.index.query_pending_alarms(
                now - _ALARM_LOOKBACK, now
            )
        except Exception:  # noqa: BLE001
            logger.exception("query_pending_alarms failed")
            return
        fired = False
        for alarm in pending:
            if alarm.db_id is None:
                continue
            title = alarm.summary or "Reminder"
            message = alarm_message(alarm, now)
            self.notify(message, title=title, timeout=_ALARM_TOAST_SECS)
            self._write_to_terminal(osc777_notification(title, message))
            try:
                self.services.index.mark_alarm_fired(alarm.db_id, now)
            except Exception:  # noqa: BLE001
                logger.exception("mark_alarm_fired failed for alarm %s", alarm.db_id)
            logger.info("alarm fired: %s (%s)", title, message)
            fired = True
        if fired:
            self.bell()

    def _write_to_terminal(self, data: str) -> None:
        """Send raw control sequences to the terminal, as `App.bell` does."""
        if not self.is_headless and self._driver is not None:
            self._driver.write(data)


# Google fills every VALARM with this DESCRIPTION; repeating it in the
# notification body says nothing the title doesn't.
_BOILERPLATE_ALARM_TEXT = "This is an event reminder"


def alarm_message(alarm: AlarmRecord, now: datetime) -> str:
    """Notification body: when the event starts, then any alarm text."""
    start = alarm.occurrence_start.astimezone()
    local_now = now.astimezone()
    if start.date() == local_now.date():
        when = start.strftime("%H:%M")
    else:
        when = start.strftime("%a %d %b %H:%M")
    verb = "Started" if alarm.occurrence_start <= now else "Starts"
    lines = [f"{verb} {when}"]
    description = (alarm.description or "").strip()
    if description and description != _BOILERPLATE_ALARM_TEXT:
        lines.append(description)
    return "\n".join(lines)


__all__ = [
    "CALENDAR_CSS",
    "CalendarHost",
    "ChronosApp",
    "AttendeeCompleter",
    "InvitationSender",
    "SyncRunner",
    "TuiServices",
    # Re-exported: the alarm poller's companion, now shared with the
    # mail side's notifications (`pony.tui.terminal.notify_terminal`).
    "osc777_notification",
]
