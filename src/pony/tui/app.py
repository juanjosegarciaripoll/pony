"""Top-level Textual application for Pony Express."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen

from chronos.protocols import CalendarSyncOutcome, CalendarSyncScheduler
from chronos.scheduler import PeriodicSync
from chronos.tui.app import CALENDAR_CSS
from chronos.tui.app import TuiServices as CalendarServices
from chronos.tui.screens.main_screen import MainScreen as CalendarScreen

from ..accounts import resolve_smtp_password
from ..calendar import CalendarRuntime
from ..composer import DraftSpec
from ..domain import AnyAccount, AppConfig, ViewerRule
from ..notifications import Notification, NotificationCenter, NotificationSource
from ..paths import AppPaths
from ..protocols import (
    ContactRepository,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from ..sync import MailSyncOutcome, MailSyncScheduler, build_sync_service
from .calendar_host import (
    AlarmPoller,
    build_calendar_services,
    send_event_invitations,
)
from .screens.main_screen import MainScreen
from .terminal import (
    format_terminal_title,
    notify_terminal,
    pop_terminal_title,
    push_terminal_title,
    set_terminal_title,
)
from .widgets.folder_panel import has_inbox_mail

# How often reminders are checked and the two status lines refreshed.
# Matches the calendar's own alarm poll when it runs standalone.
_COMPANION_TICK_SECONDS = 30.0

# A reminder stays on screen long enough to be read after looking away.
_REMINDER_TOAST_SECONDS = 120.0


@runtime_checkable
class MailSyncHost(Protocol):
    """The application the mail reader lives on, as the screen sees it.

    `MainScreen` reads the periodic sync off `self.app`, so whatever hosts
    it has to offer one — `PonyApp` does. Spelled as a protocol for the
    same reason the calendar has `CalendarHost`: the screen cannot import
    the application module, which imports the screen.
    """

    @property
    def mail_sync(self) -> MailSyncScheduler | None: ...


class PonyApp(App[None]):
    """Pony Express — terminal mail client with its calendar.

    One application, two full-screen subsystems.  The mail reader is
    what opens; ++f2++ puts the agenda in front of it and ++f2++ again
    brings the mail back.  Both halves announce through one
    :class:`~pony.notifications.NotificationCenter`, so a reminder
    reaches the user in the mail reader and newly arrived mail reaches
    them in the agenda.
    """

    TITLE = "Pony Express"
    SUB_TITLE = "Mail"

    # The Textual built-in command palette (``ctrl+p``) shows a
    # system-style side panel with actions like Quit and Show Keys.
    # Disable it: our own centered help dialog on ``F1`` is the
    # intended keyboard-shortcut discovery path.
    ENABLE_COMMAND_PALETTE = False

    # ``App.CSS`` is global, and the calendar's rules are the stylesheet
    # its screens were written against.  They select calendar widgets,
    # which only ever exist under the calendar's own screens.
    CSS = CALENDAR_CSS

    BINDINGS = [
        Binding("Q", "quit", "Quit", priority=True),
        Binding("f1", "show_help", "Help"),
        # One description serves both footers and both help screens,
        # so it names the pair rather than the destination: "Calendar"
        # reads wrong from inside the calendar.
        Binding("f2", "toggle_calendar", "Mail / Calendar"),
    ]

    def action_show_help(self) -> None:
        """Push the centered help dialog (keybinding reference)."""
        from .screens.help_screen import HelpScreen

        # If the help screen is already on top, F1 from the screen
        # itself dismisses it — this path is only for app-level F1.
        self.push_screen(HelpScreen())

    def __init__(
        self,
        config: AppConfig,
        index: IndexRepository,
        mirrors: dict[str, MirrorRepository],
        credentials: CredentialsProvider,
        contacts: ContactRepository | None = None,
        config_path: Path | None = None,
        theme_name: str | None = None,
        ui_state_path: Path | None = None,
        calendar: CalendarRuntime | None = None,
        now: Callable[[], datetime] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._config = config
        self._index = index
        self._mirrors = mirrors
        self._credentials = credentials
        self._contacts = contacts
        self._config_path = config_path
        self._ui_state_path = ui_state_path
        self._mcp_tcp_task: asyncio.Task[None] | None = None
        self._mcp_state_file: Path | None = None
        self._calendar = calendar
        # Built on mount: the OAuth flow it carries needs a running
        # application to push its screen onto.
        self._calendar_services: CalendarServices | None = None
        self._alarm_poller: AlarmPoller | None = None
        # Both periodic syncs, each on a thread of its own, started on
        # mount and stopped on unmount. They belong to the application
        # rather than to a screen because a screen's timer dies when the
        # screen is popped, and the user switches halves with F2 all day.
        self._mail_sync: MailSyncScheduler | None = None
        self._calendar_sync: CalendarSyncScheduler | None = None
        # One clock for everything time-dependent on this side: which
        # reminders are due, and which event comes next. Injectable so a
        # screenshot run or a test can pin it.
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self.notifications = NotificationCenter()
        self.notifications.subscribe(self._announce)
        if theme_name is not None:
            self.theme = theme_name

    def compose(self) -> ComposeResult:
        # Screens are pushed via on_mount; compose yields nothing at app level.
        return iter([])

    async def on_mount(self) -> None:
        push_terminal_title()
        set_terminal_title(
            format_terminal_title(
                "Pony Express",
                has_inbox_mail=self._has_inbox_mail(),
            )
        )
        self.push_screen(
            MainScreen(
                self._config,
                self._index,
                self._mirrors,
                credentials=self._credentials,
                contacts=self._contacts,
                ui_state_path=self._ui_state_path,
                notifications=self.notifications,
                calendar=self._calendar,
            )
        )
        if self._calendar is not None:
            self._calendar_services = build_calendar_services(
                self._calendar,
                host=self,
                invitation_sender=self._post_invitation,
                contacts=self._contacts,
                # Read off this class's own bindings rather than spelled
                # out again, so the calendar's help can never name a key
                # that moved.
                now=self._now,
                host_bindings=[
                    binding
                    for binding in self.BINDINGS
                    if isinstance(binding, Binding)
                    and binding.action == "toggle_calendar"
                ],
            )
            self._alarm_poller = AlarmPoller(self._calendar.index)
            self.set_interval(
                _COMPANION_TICK_SECONDS, self._companion_tick, name="companion"
            )
            self._companion_tick()
        self._start_schedulers()
        await self._start_mcp_tcp_server()

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------

    @property
    def calendar_services(self) -> CalendarServices:
        """The calendar screens' dependencies — see `chronos.tui.app.CalendarHost`."""
        if self._calendar_services is None:
            raise RuntimeError("the calendar is not configured")
        return self._calendar_services

    @property
    def calendar_available(self) -> bool:
        return self._calendar_services is not None

    def action_toggle_calendar(self) -> None:
        """Swap between the mail reader and the agenda.

        The agenda is pushed over the mail screen rather than replacing
        it, so coming back finds the message list exactly as it was —
        same folder, same cursor row, same reader scroll position.
        """
        if not self.calendar_available:
            self.notify(
                "No calendar configured — add a [calendar] table to config.toml.",
                severity="warning",
            )
            return
        if isinstance(self.screen, CalendarScreen):
            self.pop_screen()
            self._refresh_companion_status()
            return
        if any(isinstance(s, CalendarScreen) for s in self.screen_stack):
            # The agenda is open underneath one of its own dialogs — an
            # event being edited, a sync being confirmed. Stacking a
            # second agenda on top of that would be nonsense, and
            # popping back to the mail side would throw the dialog away
            # with whatever is half-typed in it.
            return
        self.push_screen(CalendarScreen())
        # The header is app-wide and the mail screen has written the
        # open folder into it; name the subsystem now in front instead.
        # Coming back, `_refresh_companion_status` restores the folder.
        self.sub_title = "Calendar"
        self._refresh_companion_status()

    def _post_invitation(
        self,
        *,
        ics: bytes,
        attendees: Sequence[str],
        organizer: str | None,
        summary: str,
        is_update: bool,
    ) -> None:
        """Mail an event the calendar just saved to its attendees.

        Satisfies `chronos.tui.app.InvitationSender`.  The event is
        already stored by the time this runs, so every failure here is
        reported as a failure to *post* the invitation, never as a lost
        event.
        """
        account = next((a for a in self._config.accounts if a.can_send), None)
        if account is None:
            self.notify(
                "Event saved, but no account is configured for sending invitations.",
                severity="warning",
            )
            return
        try:
            password = resolve_smtp_password(account, self._credentials)
        except Exception as exc:  # noqa: BLE001 — any backend may fail
            self.notify(f"Could not get password: {exc}", severity="error")
            return
        if password is None:
            self.notify(
                f"No password available for account {account.name!r}.",
                severity="error",
            )
            return
        self._deliver_invitation(
            ics=ics,
            attendees=tuple(attendees),
            organizer=organizer,
            summary=summary,
            is_update=is_update,
            account=account,
            password=password,
        )

    @work(exclusive=False, group="invitation-send")
    async def _deliver_invitation(
        self,
        *,
        ics: bytes,
        attendees: Sequence[str],
        organizer: str | None,
        summary: str,
        is_update: bool,
        account: AnyAccount,
        password: str,
    ) -> None:
        """Run the invitation's SMTP conversation off the event loop."""
        delivery = await asyncio.to_thread(
            send_event_invitations,
            ics=ics,
            attendees=attendees,
            organizer=organizer,
            summary=summary,
            is_update=is_update,
            account=account,
            password=password,
            connect_timeout=self._config.smtp_connect_timeout_seconds,
        )
        if not delivery.ok:
            self.notify(
                f"Event saved, but the invitation was not sent: {delivery.error}",
                severity="error",
                timeout=15,
            )
            return
        if delivery.sent_to:
            count = len(delivery.sent_to)
            plural = "" if count == 1 else "s"
            self.notify(f"Invitation sent to {count} attendee{plural}.")

    def _start_schedulers(self) -> None:
        """Start both periodic syncs, each on its own thread.

        Neither depends on what is on screen. The calendar's used to be
        the mail reader's problem — the agenda only syncs while it is
        mounted, and a reminder can only fire for an event the local
        alarm cache knows about — and the mail reader's used to live on
        its own screen. Now one thread each keeps both halves current
        whichever one the user is looking at, and the screens read the
        countdown off them.

        Each is config-gated as before: with background sync disabled the
        thread is not started, and the manual key still works.
        """
        self._start_mail_scheduler()
        self._start_calendar_scheduler()

    def _start_mail_scheduler(self) -> None:
        service = build_sync_service(
            config=self._config,
            index=self._index,
            mirrors=self._mirrors,
            credentials=self._credentials,
        )
        scheduler: MailSyncScheduler = PeriodicSync(
            name="mail-sync",
            # A mail sync has no cancellation point, so the keyword is
            # swallowed here rather than threaded into the engine.
            runner=lambda **_kwargs: service.sync(),
            interval=self._config.background_sync_interval_seconds,
            on_started=self._on_mail_sync_started,
            on_finished=self._on_mail_sync_finished,
        )
        self._mail_sync = scheduler
        if self._config.background_sync_enabled:
            scheduler.start()

    def _start_calendar_scheduler(self) -> None:
        services = self._calendar_services
        if self._calendar is None or services is None:
            return
        runner = services.sync_runner
        if runner is None:
            return
        scheduler: CalendarSyncScheduler = PeriodicSync(
            name="calendar-sync",
            runner=runner,
            interval=self._calendar.config.background_sync_interval_seconds,
            on_started=self._on_calendar_sync_started,
            on_finished=self._on_calendar_sync_finished,
        )
        self._calendar_sync = scheduler
        services.sync_scheduler = scheduler
        if self._calendar.config.background_sync_enabled:
            scheduler.start()

    def _stop_schedulers(self) -> None:
        for scheduler in (self._mail_sync, self._calendar_sync):
            if scheduler is not None:
                scheduler.stop()

    @property
    def mail_sync(self) -> MailSyncScheduler | None:
        """The mail reader's periodic sync, for the screen that shows it."""
        return self._mail_sync

    # -- scheduler callbacks, all arriving on a scheduler thread ----------

    def _on_mail_sync_started(self) -> None:
        self._on_ui_thread(self._mail_screen_started)

    def _on_mail_sync_finished(self, outcome: MailSyncOutcome) -> None:
        self._on_ui_thread(lambda: self._mail_screen_finished(outcome))

    def _on_calendar_sync_started(self) -> None:
        self._on_ui_thread(self._calendar_screen_started)

    def _on_calendar_sync_finished(self, outcome: CalendarSyncOutcome) -> None:
        self._on_ui_thread(lambda: self._calendar_screen_finished(outcome))

    def _on_ui_thread(self, action: Callable[[], None]) -> None:
        """Hop onto the UI thread, forgiving an application already gone.

        A `RuntimeError` here means the user quit while a sync was in
        flight, which is ordinary and not worth reporting.
        """
        with contextlib.suppress(RuntimeError):
            self.call_from_thread(action)

    def _mail_screen_started(self) -> None:
        screen = self._find_screen(MainScreen)
        if screen is not None:
            screen.on_sync_started()

    def _mail_screen_finished(self, outcome: MailSyncOutcome) -> None:
        screen = self._find_screen(MainScreen)
        if screen is not None:
            screen.on_sync_finished(outcome)
        elif outcome.error is not None:
            # Nobody is looking at the mail reader; the log is where a
            # sync that has been failing all day can be found.
            self.log.warning(f"background mail sync failed: {outcome.error}")
        self._refresh_companion_status()

    def _calendar_screen_started(self) -> None:
        screen = self._find_screen(CalendarScreen)
        if screen is not None:
            screen.on_sync_started()

    def _calendar_screen_finished(self, outcome: CalendarSyncOutcome) -> None:
        screen = self._find_screen(CalendarScreen)
        if screen is not None:
            screen.on_sync_finished(outcome)
        elif outcome.error is not None:
            self.log.warning(f"background calendar sync failed: {outcome.error}")
        self._refresh_companion_status()

    def _find_screen[ScreenT: Screen[None]](
        self, kind: type[ScreenT]
    ) -> ScreenT | None:
        """The screen of `kind` on the stack, visible or suspended.

        A sync that finishes while the user is in the other half still
        has a screen to report to — it is simply not the one in front, so
        the toast it raises is the application's to route.
        """
        for screen in self.screen_stack:
            if isinstance(screen, kind):
                return screen
        return None

    def _companion_tick(self) -> None:
        """Fire due reminders and refresh what each half says about the other."""
        self._fire_due_reminders()
        self._refresh_companion_status()

    def _fire_due_reminders(self) -> None:
        if self._alarm_poller is None:
            return
        try:
            due = self._alarm_poller.due(self._now())
        except Exception:  # noqa: BLE001 — a failed poll must not kill the app
            self.log.warning("calendar alarm poll failed")
            return
        for notification in due:
            self.notifications.publish(notification)

    def _refresh_companion_status(self) -> None:
        """Put the calendar's next event in the mail reader, and vice versa."""
        from .calendar_host import mail_status, next_event_status

        if self._calendar_services is None:
            return
        screen = self.screen
        if isinstance(screen, CalendarScreen):
            screen.set_companion_status(
                mail_status(self._index, self._config, use_utf8=self._config.use_utf8)
            )
            return
        if isinstance(screen, MainScreen):
            try:
                status = next_event_status(
                    self._calendar_services,
                    now=self._now(),
                    use_utf8=self._config.use_utf8,
                )
            except Exception:  # noqa: BLE001 — a status line is not worth a crash
                self.log.warning("next-event lookup failed")
                return
            screen.set_companion_status(status)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _announce(self, notification: Notification) -> None:
        """Render one announcement from the shared notification centre.

        A reminder is shown wherever the user is.  Anything else is
        shown only when the subsystem it came from is *not* the one on
        screen: that subsystem reports its own news itself, and a second
        toast saying the same thing is noise.
        """
        if not notification.urgent and notification.source is self._visible_source():
            return
        timeout = _REMINDER_TOAST_SECONDS if notification.urgent else None
        self.notify(
            notification.body or notification.title,
            title=notification.title,
            severity=notification.severity,
            timeout=timeout,
        )
        notify_terminal(notification.title, notification.body)
        self.bell()

    def _visible_source(self) -> NotificationSource | None:
        """Which subsystem the user is looking at, if either."""
        if not self.screen_stack:
            return None
        if isinstance(self.screen, CalendarScreen):
            return NotificationSource.CALENDAR
        if isinstance(self.screen, MainScreen):
            return NotificationSource.MAIL
        return None

    def _has_inbox_mail(self) -> bool:
        """True when any configured account has unread INBOX mail."""
        return any(
            has_inbox_mail(
                self._index.unread_counts_by_folder(account_name=account.name)
            )
            for account in self._config.accounts
        )

    async def _start_mcp_tcp_server(self) -> None:
        from ..config import ConfigError
        from ..mcp_server import start_tcp_mcp_server

        paths = AppPaths.default()
        state_file = paths.mcp_state_file
        try:
            task, _ = await start_tcp_mcp_server(self._config_path, state_file)
        except (ConfigError, OSError):
            return
        self._mcp_tcp_task = task
        self._mcp_state_file = state_file

    async def on_unmount(self) -> None:
        from ..mcp_server import clear_mcp_state

        self._stop_schedulers()

        if self._mcp_tcp_task is not None:
            self._mcp_tcp_task.cancel()
            await asyncio.gather(self._mcp_tcp_task, return_exceptions=True)
        if self._mcp_state_file is not None:
            clear_mcp_state(self._mcp_state_file)
        pop_terminal_title()


class ComposeApp(App[None]):
    """Minimal Textual app that opens the composer directly.

    Used by ``pony compose`` when the user wants to write a new message
    without entering the full mail-reader TUI.
    """

    TITLE = "Pony Express — Compose"
    INHERIT_BINDINGS = False

    BINDINGS = [
        Binding("Q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        config: AppConfig,
        account: AnyAccount,
        index: IndexRepository,
        mirrors: dict[str, MirrorRepository],
        contacts: ContactRepository | None = None,
        credentials: CredentialsProvider | None = None,
        to: str = "",
        cc: str = "",
        bcc: str = "",
        subject: str = "",
        body: str = "",
        markdown_mode: bool = False,
        theme_name: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._config = config
        self._account = account
        self._index = index
        self._mirrors = mirrors
        self._contacts = contacts
        self._credentials = credentials
        self._to = to
        self._cc = cc
        self._bcc = bcc
        self._subject = subject
        self._body = body
        self._markdown_mode = markdown_mode
        if theme_name is not None:
            self.theme = theme_name

    def on_mount(self) -> None:
        push_terminal_title()
        set_terminal_title("Pony Express — Compose")
        from .screens.compose_screen import ComposeScreen

        def _on_done(sent: bool | None) -> None:
            if sent:
                self.notify("Message sent.", timeout=2)
                self.set_timer(2, self.exit)
            else:
                self.exit()

        # ComposeScreen only accepts accounts that can send; ``pony
        # compose`` at the CLI refuses to launch without at least one
        # sendable account, so this filter is primarily defensive.
        accounts = [a for a in self._config.accounts if a.can_send]
        self.push_screen(
            ComposeScreen(
                self._config,
                accounts,
                self._index,
                self._mirrors,
                DraftSpec(
                    account_name=self._account.name,
                    to=self._to,
                    cc=self._cc,
                    bcc=self._bcc,
                    subject=self._subject,
                    body=self._body,
                    markdown_mode=self._markdown_mode,
                ),
                contacts=self._contacts,
                credentials=self._credentials,
            ),
            _on_done,
        )

    def on_unmount(self) -> None:
        pop_terminal_title()


class EmlViewerApp(App[None]):
    """Minimal Textual app for viewing a single .eml file.

    Used by ``pony view <file>`` and when Pony is invoked with a filename
    argument.  Nested email attachments open additional ``EmlViewerScreen``
    instances on this app's screen stack.
    """

    TITLE = "Pony Express — Viewer"
    INHERIT_BINDINGS = False

    BINDINGS = [
        Binding("Q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        raw_bytes: bytes,
        theme_name: str | None = None,
        viewers: Sequence[ViewerRule] = (),
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._raw_bytes = raw_bytes
        self._viewers = viewers
        if theme_name is not None:
            self.theme = theme_name

    def on_mount(self) -> None:
        push_terminal_title()
        set_terminal_title("Pony Express — Viewer")
        from .screens.eml_viewer_screen import EmlViewerScreen

        self.push_screen(EmlViewerScreen(self._raw_bytes, viewers=self._viewers))

    def on_unmount(self) -> None:
        pop_terminal_title()

    def on_screen_resume(self) -> None:
        if len(self.screen_stack) <= 1:
            self.exit()


class ContactsApp(App[None]):
    """Minimal Textual app for the standalone contacts browser.

    Used by ``pony contacts browse`` to open the contacts browser
    without the full mail-reader TUI.
    """

    TITLE = "Pony Express — Contacts"

    def __init__(
        self,
        contacts: ContactRepository,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._contacts = contacts

    def on_mount(self) -> None:
        push_terminal_title()
        set_terminal_title("Pony Express — Contacts")
        from .screens.contact_browser_screen import ContactBrowserScreen

        self.push_screen(ContactBrowserScreen(self._contacts))

    def on_unmount(self) -> None:
        pop_terminal_title()

    def on_screen_resume(self) -> None:
        # Exit when the browser screen is dismissed.
        if len(self.screen_stack) <= 1:
            self.exit()
