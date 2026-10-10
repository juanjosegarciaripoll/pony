"""Main three-pane screen: folder tree | message list | message view."""

from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header
from textual.worker import Worker

from chronos.outcome import Notice, Outcome

from ... import mailbox_actions
from ...composer import (
    DraftSpec,
    forward_attachment_name,
    forward_draft,
    new_draft,
    reply_draft,
    resumed_draft,
)
from ...contact_actions import contact_for_address, harvest_folder
from ...domain import (
    AnyAccount,
    AppConfig,
    FolderMessageSummary,
    FolderRef,
    IndexedMessage,
    MessageFlag,
)
from ...folder_utils import holds_drafts, holds_sent_mail
from ...invitation import Invitation
from ...invitation_actions import (
    InvitationChoice,
    file_invitation,
    reply_to_invitation,
    replying_account,
)
from ...keymap import MAIL_KEYMAP, Surface, for_surface
from ...mailbox_actions import Mailboxes
from ...message_renderer import (
    RenderedMessage,
    render_message,
    save_every_attachment,
    save_one_attachment,
)
from ...notifications import NotificationCenter
from ...protocols import (
    ContactRepository,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from ...sync import (
    ImapSyncService,
    MailSyncOutcome,
    MailSyncScheduler,
    ProgressInfo,
    SyncPlan,
    SyncResult,
    build_sync_service,
    describe_sync_result,
)
from ...views import has_inbox_mail
from ..bindings import to_textual
from ..terminal import (
    format_terminal_title,
    launch_file,
    resolve_viewer_command,
    set_terminal_title,
    suspend_for_external_program,
)
from ..ui_state import RESIZE_STEP, load_pane_sizes, save_pane_sizes
from ..widgets.edge_drag import PaneDragged
from ..widgets.folder_panel import FolderPanel
from ..widgets.message_list import MessageListPanel
from ..widgets.message_view import MessageViewPanel

if TYPE_CHECKING:
    from ...calendar import CalendarRuntime
    from ..app import MailSyncHost


class MainScreen(Screen[None]):
    """Three-pane mutt-style mail reader screen.

    Layout:
      +-------------+---------------------------+
      |             |      MessageListPanel      |
      | FolderPanel +---------------------------+
      |             |      MessageViewPanel      |
      +-------------+---------------------------+
    """

    # Every key the reader answers to, from the one keymap both front
    # ends read (see `pony.keymap`).
    BINDINGS = to_textual(
        for_surface(MAIL_KEYMAP, Surface.READER, Surface.LIST),
    )

    CSS = """
    MainScreen {
        layout: horizontal;
    }

    FolderPanel {
        width: 25%;
        height: 100%;
        border: solid $primary;
        background: $background;
    }

    FolderPanel:focus,
    MessageListPanel:focus {
        background-tint: transparent;
        border-title-color: $accent;
    }

    #right-pane {
        width: 75%;
        height: 100%;
    }

    MessageListPanel {
        height: 1fr;
        border: solid $primary;
        background: $background;
    }

    MessageViewPanel {
        height: 2fr;
        border: solid $primary;
        display: none;
    }

    MessageViewPanel:focus {
        border-title-color: $accent;
    }
    """

    def __init__(
        self,
        config: AppConfig,
        index: IndexRepository,
        mirrors: dict[str, MirrorRepository],
        credentials: CredentialsProvider | None = None,
        contacts: ContactRepository | None = None,
        ui_state_path: Path | None = None,
        notifications: NotificationCenter | None = None,
        calendar: CalendarRuntime | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._config = config
        self._index = index
        self._mirrors = mirrors
        self._credentials = credentials
        self._contacts = contacts
        # Shared with the calendar when one is configured, so mail that
        # arrives while the agenda is open is still announced.  None
        # when this screen is driven on its own (several tests).
        self._notifications = notifications
        # The two halves of the sub-title: which folder is open, and
        # what the calendar has to say (see `set_companion_status`).
        self._folder_context = ""
        self._companion_status = ""
        # Present when a calendar is configured; what `i` files an
        # invitation into.
        self._calendar = calendar
        self._current_folder_ref: FolderRef | None = None
        self._sync_service: ImapSyncService | None = None
        self._sync_plan: SyncPlan | None = None
        # True while the foreground flow holds the scheduler's slot, so
        # the periodic thread defers instead of opening a second session.
        self._holds_sync_slot = False
        self._ui_state_path = ui_state_path
        self._pane_sizes = load_pane_sizes(ui_state_path)

    def compose(self) -> ComposeResult:
        yield Header()
        yield FolderPanel(
            self._config,
            self._index,
            self._mirrors,
            id="folder-panel",
        )
        with Vertical(id="right-pane"):
            yield MessageListPanel(self._index, id="message-list")
            yield MessageViewPanel(id="message-view")
        yield Footer()

    def on_mount(self) -> None:
        """Show when the next automatic sync falls due, if one is scheduled.

        The sync itself belongs to the application, on a thread that does
        not care which screen is in front; this screen only reports its
        schedule. No sync runs at startup — the first automatic run fires
        one interval after the application mounted.
        """
        self._apply_pane_sizes()
        self._show_next_background_sync()

    # ------------------------------------------------------------------
    # Resizable panes
    # ------------------------------------------------------------------

    def _apply_pane_sizes(self) -> None:
        """Push the stored boundary positions onto the widgets.

        The folder/right split is a percentage of the screen.  The
        list/reader split uses ``fr`` units rather than percentages
        because the reader is ``display: none`` until a message is
        opened — with ``fr`` the list expands to fill the pane on its
        own, where a percentage would leave the reader's share blank.
        """
        sizes = self._pane_sizes
        self.query_one(FolderPanel).styles.width = f"{sizes.folder_width_pct}%"
        self.query_one(
            "#right-pane", Vertical
        ).styles.width = f"{100 - sizes.folder_width_pct}%"
        self.query_one(MessageListPanel).styles.height = f"{sizes.list_share}fr"
        self.query_one(MessageViewPanel).styles.height = f"{100 - sizes.list_share}fr"

    def _resize_panes(self, *, folder_delta: int = 0, list_delta: int = 0) -> None:
        """Move a boundary by the given deltas, clamp, apply and persist."""
        proposed = dataclasses.replace(
            self._pane_sizes,
            folder_width_pct=self._pane_sizes.folder_width_pct + folder_delta,
            list_share=self._pane_sizes.list_share + list_delta,
        ).clamped()
        if proposed == self._pane_sizes:
            return  # Already at the limit — nothing to redraw or write.
        self._pane_sizes = proposed
        self._apply_pane_sizes()
        save_pane_sizes(self._ui_state_path, proposed)

    def on_pane_dragged(self, event: PaneDragged) -> None:
        """Apply a border drag, persisting only once the mouse is released.

        The drag reports a size in cells; the layout is stored as a share
        of the available space so it survives a terminal resize.
        """
        event.stop()
        if event.edge == "right":
            total = max(1, self.size.width)
            proposed = dataclasses.replace(
                self._pane_sizes,
                folder_width_pct=round(event.size * 100 / total),
            ).clamped()
        else:
            total = max(1, self.query_one("#right-pane", Vertical).size.height)
            proposed = dataclasses.replace(
                self._pane_sizes,
                list_share=round(event.size * 100 / total),
            ).clamped()

        if proposed != self._pane_sizes:
            self._pane_sizes = proposed
            self._apply_pane_sizes()
        if event.final:
            save_pane_sizes(self._ui_state_path, self._pane_sizes)

    def action_narrow_folders(self) -> None:
        self._resize_panes(folder_delta=-RESIZE_STEP)

    def action_widen_folders(self) -> None:
        self._resize_panes(folder_delta=RESIZE_STEP)

    def action_shrink_list(self) -> None:
        self._resize_panes(list_delta=-RESIZE_STEP)

    def action_grow_list(self) -> None:
        self._resize_panes(list_delta=RESIZE_STEP)

    def _show_next_background_sync(self) -> None:
        """Tell the folder panel when the next automatic sync falls due.

        The deadline is the scheduler's own, read off it rather than
        recomputed from the interval, so the countdown stays honest even
        when a run was deferred or triggered by hand.
        """
        scheduler = self._scheduler()
        deadline = None if scheduler is None else scheduler.next_run_at
        if deadline is not None:
            self.query_one(FolderPanel).set_next_sync(deadline)

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def on_folder_panel_folder_selected(
        self, event: FolderPanel.FolderSelected
    ) -> None:
        event.stop()
        self._current_folder_ref = event.folder_ref
        self._folder_context = (
            f"{event.folder_ref.account_name}/{event.folder_ref.folder_name}"
        )
        self._refresh_sub_title()
        self._set_reader_terminal_title()
        view = self.query_one(MessageViewPanel)
        view.clear()
        view.display = False
        self.query_one(MessageListPanel).load_folder(
            event.folder_ref,
            show_recipients=self._in_sent_folder(event.folder_ref),
        )
        self.query_one(MessageListPanel).focus()

    def _has_inbox_mail(self) -> bool:
        """True when any configured account has unread INBOX mail."""
        return any(
            has_inbox_mail(
                self._index.unread_counts_by_folder(account_name=account.name)
            )
            for account in self._config.accounts
        )

    def _set_reader_terminal_title(self) -> None:
        """Refresh the host terminal title for the current reader context."""
        if self._current_folder_ref is None:
            title = "Pony Express"
        else:
            context = (
                f"{self._current_folder_ref.account_name}/"
                f"{self._current_folder_ref.folder_name}"
            )
            title = f"Pony Express — {context}"
        set_terminal_title(
            format_terminal_title(title, has_inbox_mail=self._has_inbox_mail())
        )

    def _refresh_folder_indicators(self) -> None:
        """Refresh folder/account unread indicators and the terminal title."""
        self.query_one(FolderPanel).refresh_folders()
        self._set_reader_terminal_title()

    def on_message_list_panel_message_selected(
        self, event: MessageListPanel.MessageSelected
    ) -> None:
        event.stop()
        summary = event.summary
        mirror = self._mirrors[summary.message_ref.account_name]
        view = self.query_one(MessageViewPanel)
        view.load_message(summary, mirror)
        view.display = True
        view.focus()
        self._mark_seen(summary)
        # Recenter the list on the selected row now that the view panel
        # has taken 2/3 of the right pane, shrinking the visible list area.
        msg_list = self.query_one(MessageListPanel)
        msg_list.move_cursor(row=msg_list.cursor_row)

    def on_message_view_panel_close_requested(
        self, event: MessageViewPanel.CloseRequested
    ) -> None:
        event.stop()
        self._close_reader()

    def _close_reader(self) -> None:
        view = self.query_one(MessageViewPanel)
        view.display = False
        view.clear()
        self.query_one(MessageListPanel).focus()

    def on_message_view_panel_next_requested(
        self, event: MessageViewPanel.NextRequested
    ) -> None:
        event.stop()
        self._navigate_from_view(delta=1)

    def on_message_view_panel_next_unread_requested(
        self, event: MessageViewPanel.NextUnreadRequested
    ) -> None:
        event.stop()
        msg_list = self.query_one(MessageListPanel)
        summary = msg_list.move_cursor_to_next_unread()
        if summary is None:
            return
        mirror = self._mirrors[summary.message_ref.account_name]
        view = self.query_one(MessageViewPanel)
        view.load_message(summary, mirror)
        self._mark_seen(summary)

    def on_message_view_panel_prev_requested(
        self, event: MessageViewPanel.PrevRequested
    ) -> None:
        event.stop()
        self._navigate_from_view(delta=-1)

    def action_compose_address(self, idx: str) -> None:
        pair = self.query_one(MessageViewPanel).header_address(int(idx))
        if pair is None:
            return
        display, addr = pair
        self.compose_new(to=f"{display} <{addr}>" if display else addr)

    def action_activate_link(self, idx: str) -> None:
        pair = self.query_one(MessageViewPanel).body_link(int(idx))
        if pair is None or pair[0] != "web":
            return
        from .link_action_screen import LinkActionScreen

        self.app.push_screen(LinkActionScreen(pair[1]))  # pyright: ignore[reportUnknownMemberType]

    def action_compose_link(self, idx: str) -> None:
        pair = self.query_one(MessageViewPanel).body_link(int(idx))
        if pair is None or pair[0] != "mail":
            return
        self.compose_new(to=pair[1])

    def action_harvest_contact(self, idx: str) -> None:
        if self._contacts is None:
            return
        pair = self.query_one(MessageViewPanel).header_address(int(idx))
        if pair is None:
            return
        from .contact_edit_screen import ContactEditScreen

        display, address = pair
        contact = contact_for_address(self._contacts, display, address)
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ContactEditScreen(contact, self._contacts),
        )

    def _navigate_from_view(self, delta: int) -> None:
        msg_list = self.query_one(MessageListPanel)
        summary = msg_list.move_cursor_by(delta)
        if summary is None:
            return
        mirror = self._mirrors[summary.message_ref.account_name]
        view = self.query_one(MessageViewPanel)
        view.load_message(summary, mirror)
        self._mark_seen(summary)

    def on_message_list_panel_search_exit_requested(
        self, event: MessageListPanel.SearchExitRequested
    ) -> None:
        """Close the reader if one is open, otherwise leave search.

        Focus can sit on the list while a search hit is open in the
        reader, and leaving search there would strand the reader with no
        way to close it short of refocusing it.
        """
        event.stop()
        if self.query_one(MessageViewPanel).display:
            self._close_reader()
            return
        msg_list = self.query_one(MessageListPanel)
        msg_list.exit_search()
        if self._current_folder_ref is not None:
            msg_list.load_folder(
                self._current_folder_ref,
                show_recipients=self._in_sent_folder(self._current_folder_ref),
            )

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _build_sync_service(self) -> ImapSyncService | None:
        """Construct an ``ImapSyncService`` for the current accounts.

        Returns ``None`` (and notifies) when no credentials provider is
        configured — matching the guard at the top of ``action_sync``.
        The engine itself is assembled by `build_sync_service`, which the
        application's periodic scheduler uses too: it cannot reach into a
        screen for one.
        """
        if self._credentials is None:
            self.app.notify("No credentials provider.", severity="warning")  # pyright: ignore[reportUnknownMemberType]
            return None
        return build_sync_service(
            config=self._config,
            index=self._index,
            mirrors=self._mirrors,
            credentials=self._credentials,
        )

    def _claim_sync_slot(self) -> bool:
        """Take the scheduler's slot for the foreground sync flow.

        Held from here until the confirmation dialog is dismissed — the
        user's thinking time included — because during that window a
        periodic sync would open a second IMAP session on the same
        account. The scheduler defers its turn rather than losing it.
        """
        scheduler = self._scheduler()
        if scheduler is None:
            return True
        if not scheduler.try_claim():
            return False
        self._holds_sync_slot = True
        return True

    def _release_sync_slot(self) -> None:
        scheduler = self._scheduler()
        if self._holds_sync_slot and scheduler is not None:
            scheduler.release()
        self._holds_sync_slot = False
        self._show_next_background_sync()

    def action_sync(self) -> None:
        """Run the two-pass sync flow with in-TUI confirmation."""
        if self._sync_in_progress():
            self.app.notify("Sync already running.")  # pyright: ignore[reportUnknownMemberType]
            return
        service = self._build_sync_service()
        if service is None:
            return
        if not self._claim_sync_slot():
            self.app.notify("Sync already running.")  # pyright: ignore[reportUnknownMemberType]
            return
        self._sync_service = service
        self._sync_plan = None

        from .sync_confirm_screen import SyncConfirmScreen

        screen = SyncConfirmScreen.planning(
            on_confirm=self._start_sync_worker,
        )

        def _on_dismiss(result: bool | None) -> None:
            # Every way out of the foreground flow ends with this dialog
            # being dismissed — cancelled, nothing to sync, planning
            # failed, or executed — so this is where the slot goes back.
            self._release_sync_slot()
            if result is False:
                self.app.notify("Sync cancelled.")  # pyright: ignore[reportUnknownMemberType]
            self.call_after_refresh(self._refresh_after_sync)

        self.app.push_screen(screen, _on_dismiss)  # pyright: ignore[reportUnknownMemberType]

        def _plan_progress(info: ProgressInfo) -> None:
            self.app.call_from_thread(self._sync_progress, info)  # pyright: ignore[reportUnknownMemberType]

        def _run_plan() -> SyncPlan:
            return service.plan(progress=_plan_progress)

        self.run_worker(_run_plan, name="sync-plan", thread=True)

    def _start_sync_worker(self) -> None:
        """Called by SyncConfirmScreen (via callback) when the user confirms."""
        service = self._sync_service
        plan = self._sync_plan
        if service is None or plan is None:
            return

        def _progress(info: ProgressInfo) -> None:
            self.app.call_from_thread(self._sync_progress, info)  # pyright: ignore[reportUnknownMemberType]

        confirmed = plan.folders_needing_confirmation()

        def _run() -> SyncResult:
            return service.execute(
                plan, confirmed_folders=confirmed, progress=_progress
            )

        self.run_worker(_run, name="sync-exec", thread=True)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:  # pyright: ignore[reportUnknownParameterType]
        """Handle plan and sync worker completion."""
        worker = event.worker  # pyright: ignore[reportUnknownMemberType]
        if worker.state not in (worker.state.SUCCESS, worker.state.ERROR):
            return

        if worker.name == "sync-plan":
            self._on_plan_complete(worker)  # pyright: ignore[reportUnknownArgumentType]
        elif worker.name == "sync-exec":
            self._on_exec_complete(worker)  # pyright: ignore[reportUnknownArgumentType]

    def _on_plan_complete(self, worker: Worker[SyncPlan]) -> None:
        """Planning finished — show the confirm screen or report error."""
        from .sync_confirm_screen import SyncConfirmScreen

        if worker.state == worker.state.ERROR:
            if isinstance(self.app.screen, SyncConfirmScreen):  # pyright: ignore[reportUnknownMemberType]
                self.app.screen.dismiss(None)  # pyright: ignore[reportUnknownMemberType]
            err = worker.error
            msg = str(err) if err else "unknown error"
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Sync planning failed: {msg}",
                severity="error",
            )
            return

        plan: SyncPlan | None = worker.result
        if plan is None or plan.is_empty():
            if isinstance(self.app.screen, SyncConfirmScreen):  # pyright: ignore[reportUnknownMemberType]
                self.app.screen.dismiss(None)  # pyright: ignore[reportUnknownMemberType]
            self.app.notify("Nothing to sync.")  # pyright: ignore[reportUnknownMemberType]
            return

        self._sync_plan = plan
        if isinstance(self.app.screen, SyncConfirmScreen):  # pyright: ignore[reportUnknownMemberType]
            self.app.screen.show_plan(plan)  # pyright: ignore[reportUnknownMemberType]

    def _on_exec_complete(self, worker: Worker[SyncResult]) -> None:
        """Execution finished — dismiss screen and refresh."""
        from .sync_confirm_screen import SyncConfirmScreen

        if worker.state == worker.state.ERROR:
            err = worker.error
            msg = str(err) if err else "unknown error"
            self.app.notify(f"Sync failed: {msg}", severity="error")  # pyright: ignore[reportUnknownMemberType]
        else:
            self.app.notify(describe_sync_result(worker.result))  # pyright: ignore[reportUnknownMemberType]
            self._announce_new_mail(worker.result)
        if isinstance(self.app.screen, SyncConfirmScreen):  # pyright: ignore[reportUnknownMemberType]
            dismiss_result = True if worker.state == worker.state.SUCCESS else None
            self.app.screen.dismiss(dismiss_result)  # pyright: ignore[reportUnknownMemberType]

    def set_companion_status(self, text: str) -> None:
        """Show *text* beside the open folder, or drop it when empty.

        The slot belongs to whatever hosts this screen: inside Pony
        Express it carries the next calendar event, which has no screen
        of its own while the mail reader is open.  Nothing sets it when
        the mail client runs without a calendar.
        """
        self._companion_status = text
        self._refresh_sub_title()

    def _refresh_sub_title(self) -> None:
        parts = [
            part for part in (self._folder_context, self._companion_status) if part
        ]
        self.app.sub_title = "  ".join(parts) or (self.app.SUB_TITLE or "")

    def _announce_new_mail(self, result: SyncResult | None) -> None:
        """Put what a sync fetched into the shared notification space.

        The toast on this screen is this screen's own feedback; the
        announcement is what reaches the user when the agenda is the
        screen in front of them instead.  The application decides which
        of the two the user actually sees.
        """
        if self._notifications is None or result is None:
            return
        from ..calendar_host import mail_arrival_notification

        fetched = [
            (ar.account_name, sum(f.fetched for f in ar.folders))
            for ar in result.accounts
        ]
        notification = mail_arrival_notification(fetched, now=datetime.now(UTC))
        if notification is not None:
            self._notifications.publish(notification)

    def _sync_progress(self, info: ProgressInfo) -> None:
        """Update the sync screen's status (called from worker thread)."""
        from .sync_confirm_screen import SyncConfirmScreen

        if isinstance(self.app.screen, SyncConfirmScreen):  # pyright: ignore[reportUnknownMemberType]
            self.app.screen.update_progress(info)  # pyright: ignore[reportUnknownMemberType]

    def _scheduler(self) -> MailSyncScheduler | None:
        """The periodic sync, which the application owns.

        It runs on its own thread, so it keeps going while the agenda is
        in front of this screen and survives anything that happens to the
        screen itself. All this screen does is show when the next run is
        due and ask for one when the user presses the key.
        """
        # `self.app` is typed as App[Any]; cast it to the host protocol
        # so the lookup is statically checked. Imported only under
        # TYPE_CHECKING — `app.py` imports this screen, so a runtime
        # import would cycle.
        return cast("MailSyncHost", self.app).mail_sync

    def _sync_in_progress(self) -> bool:
        """True when a sync is active or awaiting its confirmation.

        Covers the periodic thread, the running workers (plan/exec) and
        the gap in the foreground flow where the ``SyncConfirmScreen``
        modal is open waiting for the user to Proceed — during that window
        no worker runs, but starting another sync would overlap IMAP
        sessions on the same account.
        """
        from .sync_confirm_screen import SyncConfirmScreen

        scheduler = self._scheduler()
        if scheduler is not None and scheduler.running:
            return True
        active = {"sync-plan", "sync-exec"}
        if any(
            w.name in active and w.is_running
            for w in self.workers  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        ):
            return True
        return any(
            isinstance(screen, SyncConfirmScreen)
            for screen in self.app.screen_stack  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        )

    def action_background_sync(self) -> None:
        """Run a non-blocking, auto-confirm-everything sync (``ctrl+g``).

        Unlike ``action_sync`` it shows no modal and assumes "yes" to every
        confirmation, including the mass-deletion guard — ``sync()`` confirms
        all folders internally.  A spinner on the FolderPanel border title is
        the only visual clue.

        The manual trigger also starts the periodic cadence and counts the
        next run from here, which is how ``ctrl+g`` has always behaved even
        when ``background_sync_enabled`` is off.
        """
        scheduler = self._scheduler()
        if scheduler is None:
            self.app.notify("Background sync is not wired in this build.")  # pyright: ignore[reportUnknownMemberType]
            return
        if self._sync_in_progress():
            self.app.notify("Sync already running.")  # pyright: ignore[reportUnknownMemberType]
            return
        scheduler.start()
        if not scheduler.request_now():
            self.app.notify("Sync already running.")  # pyright: ignore[reportUnknownMemberType]
            return
        self._show_next_background_sync()

    def on_sync_started(self) -> None:
        """The scheduler began a run; called on the UI thread by the app."""
        self.query_one(FolderPanel).set_syncing(True)

    def on_sync_finished(self, outcome: MailSyncOutcome) -> None:
        """The scheduler finished a run; called on the UI thread by the app."""
        self.query_one(FolderPanel).set_syncing(False)
        self._show_next_background_sync()
        if outcome.error is not None:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Background sync failed: {outcome.error}", severity="error"
            )
            return
        if outcome.result is None:
            return
        self.app.notify(describe_sync_result(outcome.result))  # pyright: ignore[reportUnknownMemberType]
        self._announce_new_mail(outcome.result)
        self.call_after_refresh(self._refresh_after_sync)

    # ------------------------------------------------------------------
    # Invitations
    # ------------------------------------------------------------------

    def action_respond_invitation(self) -> None:
        """Answer the meeting invitation on the open message.

        The dialog settles which calendar the event goes in and what the
        organizer is told, because those are one decision for the user
        even though they are two operations underneath.
        """
        from chronos.views import all_calendar_refs

        from .invitation_screen import InvitationScreen

        invitation = self.query_one(MessageViewPanel).invitation
        if invitation is None:
            self.app.notify("This message carries no invitation.")
            return
        if self._calendar is None:
            self.app.notify(
                "No calendar configured — add a [calendar] table to config.toml.",
                severity="warning",
            )
            return
        calendars = all_calendar_refs(self._calendar.config, self._calendar.mirror)
        if not calendars:
            self.app.notify(
                "No calendars yet — sync the calendar first.", severity="warning"
            )
            return

        def _chosen(choice: InvitationChoice | None) -> None:
            if choice is not None:
                self._apply_invitation(invitation, choice)

        self.app.push_screen(
            InvitationScreen(
                invitation,
                calendars,
                can_reply=bool(invitation.organizer) and invitation.is_request,
            ),
            _chosen,
        )

    def _apply_invitation(
        self, invitation: Invitation, choice: InvitationChoice
    ) -> None:
        """File *invitation* in the chosen calendar, then answer the organizer.

        Filing comes first and is reported on its own: an accepted
        invitation that reached the calendar but whose reply bounced is a
        very different situation from one that never landed, and saying
        "accepted" for both would hide it.  Both halves are
        :mod:`pony.invitation_actions`; what is left here is the toast.
        """
        assert self._calendar is not None
        outcome = file_invitation(
            invitation,
            calendar=self._calendar,
            target=choice.calendar,
        )
        self._report(outcome)
        if outcome.failed or invitation.is_cancellation or choice.partstat is None:
            return
        self._send_invitation_reply(invitation, choice.partstat)

    def _send_invitation_reply(self, invitation: Invitation, partstat: str) -> None:
        """Answer the organizer from the account the invitation arrived on."""
        summary = self.get_current_message()
        found = replying_account(
            self._config,
            arrived_on=(None if summary is None else summary.message_ref.account_name),
        )
        if isinstance(found, Notice):
            self.app.notify(found.text, severity=found.severity)  # pyright: ignore[reportUnknownMemberType]
            return
        self._deliver_invitation_reply(found, invitation, partstat)

    @work(exclusive=True, group="invitation-reply")
    async def _deliver_invitation_reply(
        self,
        account: AnyAccount,
        invitation: Invitation,
        partstat: str,
    ) -> None:
        """Run the reply's SMTP conversation off the event loop."""
        outcome = await asyncio.to_thread(
            reply_to_invitation,
            invitation,
            partstat=partstat,
            account=account,
            config=self._config,
            credentials=self._credentials,
        )
        self._report(outcome)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def action_search(self) -> None:
        from .search_dialog_screen import SearchDialogScreen

        def _on_query(raw: str | None) -> None:
            if raw:
                self._run_search(raw)

        self.app.push_screen(SearchDialogScreen(), _on_query)  # pyright: ignore[reportUnknownMemberType]

    def _run_search(self, raw: str) -> None:
        from ...search_parser import parse_query

        folder_panel = self.query_one(FolderPanel)
        account_name, folder_ref = folder_panel.get_search_scope()
        if account_name is None:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Select a folder or account first.", severity="warning"
            )
            return

        query = parse_query(raw)
        results = list(self._index.search(query=query, account_name=account_name))
        if folder_ref is not None:
            results = [
                m
                for m in results
                if m.message_ref.folder_name == folder_ref.folder_name
            ]

        msg_list = self.query_one(MessageListPanel)
        msg_list.load_search_results(results, raw)
        msg_list.focus()

    # ------------------------------------------------------------------
    # Goto folder
    # ------------------------------------------------------------------

    def action_goto_folder(self) -> None:
        from .goto_folder_screen import GotoFolderScreen

        folders: list[FolderRef] = []
        for account in self._config.accounts:
            mirror = self._mirrors.get(account.name)
            if mirror is not None:
                folders.extend(mirror.list_folders(account_name=account.name))

        def _on_folder(ref: FolderRef | None) -> None:
            if ref is not None:
                self.query_one(FolderPanel).select_folder_ref(ref)

        self.app.push_screen(GotoFolderScreen(folders), _on_folder)  # pyright: ignore[reportUnknownMemberType]

    # ------------------------------------------------------------------
    # Post-sync refresh
    # ------------------------------------------------------------------

    def refresh_after_sync(self) -> None:
        """Rebuild the folder tree and reload the current message list."""
        self._refresh_after_sync()

    def _refresh_after_sync(self) -> None:
        self._refresh_folder_indicators()
        if self._current_folder_ref is not None:
            # A background sync can land at any moment; it must not move
            # the cursor out from under someone reading their mail.
            self._reload_keeping_cursor(self._current_folder_ref)

    # ------------------------------------------------------------------
    # Contextual action gating
    # ------------------------------------------------------------------

    def _in_sent_folder(self, folder_ref: FolderRef) -> bool:
        return holds_sent_mail(self._config, folder_ref)

    def _in_drafts_folder(self) -> bool:
        ref = self._current_folder_ref
        return ref is not None and holds_drafts(self._config, ref)

    def check_action(
        self,
        action: str,
        parameters: tuple[object, ...],
    ) -> bool | None:
        if action == "edit_draft":
            return self._in_drafts_folder()
        return super().check_action(action, parameters)

    # ------------------------------------------------------------------
    # Flag and message actions
    # ------------------------------------------------------------------

    def get_current_message(self) -> IndexedMessage | None:
        """Return the full IndexedMessage for the highlighted row.

        The panel holds only summaries; for reply/forward/etc. we
        re-fetch the full row from the index on demand.
        """
        summary = self.query_one(MessageListPanel).get_selected_summary()
        if summary is None:
            return None
        return self._index.get_message(message_ref=summary.message_ref)

    def _folder_ref_from_summary(self, summary: FolderMessageSummary) -> FolderRef:
        return FolderRef(
            account_name=summary.message_ref.account_name,
            folder_name=summary.message_ref.folder_name,
        )

    def _reload_keeping_cursor(self, folder_ref: FolderRef) -> None:
        """Reload the message list without throwing the cursor to the top.

        ``clear()`` resets the cursor and the replacement rows arrive
        from a worker afterwards, so the panel is told where to land
        rather than being moved once this returns — at that point there
        is nothing to move it to.  The message under the cursor is named
        so it can be followed when it survives (a sync may have pushed it
        down the list) and the row number carries the case where it did
        not (trash, archive, move) by selecting whatever took its place.
        """
        msg_list = self.query_one(MessageListPanel)
        summary = msg_list.get_selected_summary()
        msg_list.load_folder(
            folder_ref,
            restore_row=msg_list.effective_cursor_row,
            restore_key=None if summary is None else str(summary.message_ref.id),
            show_recipients=self._in_sent_folder(folder_ref),
        )

    def _reload_folder(self, folder_ref: FolderRef) -> None:
        self._reload_keeping_cursor(folder_ref)
        self._refresh_view_after_reload()
        self._refresh_folder_indicators()

    def _refresh_view_after_reload(self) -> None:
        """Sync the message view to the current cursor row.

        After ``_reload_folder`` the row that was open may have been
        removed (archive/trash/move) or its content may have shifted.
        Without this the view pane keeps showing the stale message.
        """
        view = self.query_one(MessageViewPanel)
        if not view.display:
            return
        msg_list = self.query_one(MessageListPanel)
        summary = msg_list.get_selected_summary()
        if summary is None:
            view.clear()
            view.display = False
            return
        mirror = self._mirrors[summary.message_ref.account_name]
        view.load_message(summary, mirror)

    def _mark_seen(self, summary: FolderMessageSummary) -> None:
        if MessageFlag.SEEN in summary.local_flags:
            return
        message = self._index.get_message(message_ref=summary.message_ref)
        if message is None:
            return
        updated = mailbox_actions.add_flag(self._mailboxes, message, MessageFlag.SEEN)
        if updated is None:
            return
        self.query_one(MessageListPanel).update_from_indexed(updated)
        self._refresh_folder_indicators()

    @property
    def _mailboxes(self) -> Mailboxes:
        """The stores every mailbox action works on."""
        return Mailboxes(self._config, self._index, self._mirrors)

    def _report(self, outcome: Outcome) -> None:
        """Say everything an action had to say, in the order it said it."""
        for notice in outcome.notices:
            self.app.notify(notice.text, severity=notice.severity)  # pyright: ignore[reportUnknownMemberType]

    def _targets(self) -> list[FolderMessageSummary]:
        """Summaries to act on: marked rows if any, else the cursor row."""
        return self.query_one(MessageListPanel).summaries_to_act_on()

    def _resolve_targets(
        self, summaries: list[FolderMessageSummary]
    ) -> list[IndexedMessage]:
        """Fetch full IndexedMessage rows for each summary in *summaries*."""
        return mailbox_actions.resolve_messages(
            self._mailboxes,
            [s.message_ref for s in summaries],
        )

    def _act_on_targets(
        self,
        act: collections.abc.Callable[[list[IndexedMessage], FolderRef], Outcome],
    ) -> None:
        """Run *act* over the marked rows, then report and repaint.

        The shape every list action shares: resolve what the cursor and
        the marks point at, hand it to the model, say what came back and
        reload the folder it happened in.
        """
        targets = self._targets()
        if not targets:
            return
        messages = self._resolve_targets(targets)
        if not messages:
            return
        source = self._folder_ref_from_summary(targets[0])
        outcome = act(messages, source)
        self.query_one(MessageListPanel).clear_marks()
        self._reload_folder(source)
        self._report(outcome)

    def set_flag(self, flag: MessageFlag, *, present: bool) -> None:
        """Add or remove *flag* on every target message and refresh."""
        self._act_on_targets(
            lambda messages, _source: mailbox_actions.set_flag(
                self._mailboxes,
                messages,
                flag,
                present=present,
            ),
        )

    def _mark_answered(self, msg: IndexedMessage) -> None:
        if mailbox_actions.add_flag(self._mailboxes, msg, MessageFlag.ANSWERED) is None:
            return
        self._reload_folder(
            FolderRef(
                account_name=msg.message_ref.account_name,
                folder_name=msg.message_ref.folder_name,
            ),
        )

    def toggle_flag(self, flag: MessageFlag) -> None:
        """Toggle *flag* on every target message and refresh the list."""
        self._act_on_targets(
            lambda messages, _source: mailbox_actions.toggle_flag(
                self._mailboxes,
                messages,
                flag,
            ),
        )

    def trash_current_message(self) -> None:
        """Mark every target message as trashed in the index."""
        self._act_on_targets(
            lambda messages, _source: mailbox_actions.trash_messages(
                self._mailboxes,
                messages,
                now=datetime.now(tz=UTC),
            ),
        )

    def archive_current_message(self) -> None:
        """Move every target message into the account's archive folder."""
        self._act_on_targets(
            lambda messages, source: mailbox_actions.archive_messages(
                self._mailboxes,
                messages,
                source,
            ),
        )

    def action_mark_all_read(self) -> None:
        """Mark every message in the current folder as read."""
        folder_ref = self._current_folder_ref
        if folder_ref is None:
            return
        outcome = mailbox_actions.mark_folder_read(self._mailboxes, folder_ref)
        self._reload_keeping_cursor(folder_ref)
        self._refresh_folder_indicators()
        self._report(outcome)

    def action_mark_unread(self) -> None:
        self.set_flag(MessageFlag.SEEN, present=False)

    def action_toggle_flagged(self) -> None:
        self.toggle_flag(MessageFlag.FLAGGED)

    def action_trash(self) -> None:
        self.trash_current_message()

    def action_archive(self) -> None:
        self.archive_current_message()

    def action_copy(self) -> None:
        """Prompt for a target folder and copy every target message there.

        Copies are applied locally (index + mirror) immediately; the next
        sync pushes them to the server via ``APPEND``.  Within a single
        account the copy is given a synthetic Message-ID so the sync
        planner doesn't mistake the duplicate for a move (the planner
        keys cross-folder identity on Message-ID, and multi-folder
        identity is a deferred feature).  Across accounts the original
        Message-ID is preserved — accounts are independent identity
        namespaces and a true copy keeps IMAP thread integrity intact.
        """
        self._prompt_folder_picker(verb="Copy", on_chosen=self._copy_to_folder)

    def _prompt_folder_picker(
        self,
        *,
        verb: str,
        on_chosen: collections.abc.Callable[
            [list[IndexedMessage], FolderRef, FolderRef], None
        ],
    ) -> None:
        """Resolve the marked messages and prompt for a destination folder.

        Shared skeleton for :meth:`action_copy` / :meth:`action_move`: gathers
        the targets, opens :class:`PickFolderScreen` titled with *verb*, and on
        a non-cancel dismissal invokes ``on_chosen(messages, source, target)``.
        """
        summaries = self._targets()
        if not summaries:
            return
        messages = self._resolve_targets(summaries)
        if not messages:
            return
        source_ref = self._folder_ref_from_summary(summaries[0])

        from .pick_folder_screen import PickFolderScreen

        screen = PickFolderScreen(
            config=self._config,
            mirrors=self._mirrors,
            title=f"{verb} to folder (source: {source_ref.folder_name})",
            exclude=source_ref,
        )

        def _on_dismiss(target: FolderRef | None) -> None:
            if target is None:
                return
            on_chosen(messages, source_ref, target)

        self.app.push_screen(screen, _on_dismiss)  # type: ignore[arg-type] # pyright: ignore[reportUnknownMemberType]

    def _copy_to_folder(
        self,
        targets: list[IndexedMessage],
        source: FolderRef,
        target: FolderRef,
    ) -> None:
        """Copy every *targets* row from *source* into *target*."""
        outcome = mailbox_actions.copy_messages(
            self._mailboxes, targets, source, target
        )
        self.query_one(MessageListPanel).clear_marks()
        self._report(outcome)

    def action_move(self) -> None:
        """Prompt for a target folder and move every target message there.

        Two execution paths, chosen by comparing source and target
        accounts:

        - **Same account** — mirrors the archive flow with a
          user-chosen target.  The mirror file is renamed in-place via
          ``move_message_to_folder``; the index row is removed from the
          source folder and re-inserted in the target with ``uid=NULL``.
          Message-ID is preserved — on next sync, the source folder's
          Step 2 emits ``PushMoveOp`` (``UID MOVE`` server-side).

        - **Different accounts** — no atomic cross-server IMAP move
          exists, so this decomposes into cross-account copy + trash
          source.  Bytes are written to the target mirror with the
          original Message-ID preserved (distinct accounts are
          independent identity namespaces), a ``uid=NULL`` row is
          inserted on the target side, and the source is marked
          ``TRASHED`` for IMAP sources (so the next sync EXPUNGEs it)
          or deleted outright for local sources (which have no sync to
          relay the deletion).  Target-first ordering means an
          interruption leaves a duplicate, not a loss.
        """
        self._prompt_folder_picker(verb="Move", on_chosen=self._move_to_folder)

    def _move_to_folder(
        self,
        targets: list[IndexedMessage],
        source: FolderRef,
        target: FolderRef,
    ) -> None:
        """Move every *targets* row from *source* into *target*."""
        outcome = mailbox_actions.move_messages(
            self._mailboxes, targets, source, target
        )
        self.query_one(MessageListPanel).clear_marks()
        self._reload_folder(source)
        self._report(outcome)

    # ------------------------------------------------------------------
    # Folder management
    # ------------------------------------------------------------------

    def action_new_folder(self) -> None:
        """Prompt for a name and create the folder in the local mirror."""
        account = self._account_for_new_folder()
        if account is None:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Select a folder or account first.",
                severity="warning",
            )
            return
        from .new_folder_screen import NewFolderScreen

        def _on_name(name: str | None) -> None:
            if not name:
                return
            self._create_folder(account.name, name)

        self.app.push_screen(NewFolderScreen(), _on_name)  # pyright: ignore[reportUnknownMemberType]

    def _account_for_new_folder(self) -> AnyAccount | None:
        """Pick the account to create a folder in.

        Uses the currently-open folder's account; falls back to the sole
        configured account when exactly one exists.  Works for both IMAP
        and local accounts — the mirror backends implement ``create_folder``
        either way; on IMAP accounts the folder is pushed to the server on
        the next sync, on local accounts it is terminal state.
        """
        if self._current_folder_ref is not None:
            name = self._current_folder_ref.account_name
            for a in self._config.accounts:
                if a.name == name:
                    return a
        if len(self._config.accounts) == 1:
            return self._config.accounts[0]
        return None

    def _create_folder(self, account_name: str, folder_name: str) -> None:
        """Create *folder_name* in the account's local mirror and refresh."""
        outcome = mailbox_actions.create_folder(
            self._mailboxes, account_name, folder_name
        )
        if outcome.acted:
            self._refresh_folder_indicators()
        self._report(outcome)

    # ------------------------------------------------------------------
    # Compose entry points
    # ------------------------------------------------------------------

    def action_compose_new(self) -> None:
        self.compose_new()

    def action_compose_reply(self) -> None:
        self.compose_reply()

    def action_compose_reply_all(self) -> None:
        self.compose_reply_all()

    def action_compose_forward(self) -> None:
        self.compose_forward()

    def action_edit_draft(self) -> None:
        self.compose_from_draft()

    def _sendable_accounts(self) -> list[AnyAccount]:
        """Accounts that can send via SMTP (IMAP or local-with-SMTP)."""
        return [a for a in self._config.accounts if a.can_send]

    def _sendable_or_notify(self, verb: str) -> list[AnyAccount] | None:
        """Return sendable accounts, or warn (tagged with *verb*) and None.

        *verb* is the present participle of the action ("Composing",
        "Replying", "Forwarding") that opens the notice.
        """
        accounts = self._sendable_accounts()
        if not accounts:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"{verb} requires an IMAP account (SMTP is needed to send).",
                severity="warning",
            )
            return None
        return accounts

    def _current_message_bytes(
        self, *, noun: str
    ) -> tuple[IndexedMessage, bytes] | None:
        """The highlighted message and its bytes, or None after notifying.

        Every message action resolves its target here.  The reader pane
        holds whatever was last opened with Enter, which is not
        necessarily what the cursor is on — reading bytes off the panel
        made ``w``/``O``/``S`` act on a different message than
        ``s``/``ctrl+p``, silently.
        """
        msg = self.get_current_message()
        if msg is None:
            return None
        raw = self._load_current_raw_or_notify(msg, noun=noun)
        if raw is None:
            return None
        return msg, raw

    def _load_current_raw_or_notify(
        self, msg: IndexedMessage, *, noun: str
    ) -> bytes | None:
        """Read *msg*'s raw bytes from its mirror for a reply/forward.

        On a read failure, notify "Could not load message for {noun}" and
        return None.  Indexes ``_mirrors`` directly: the current message's
        account always has a mirror.
        """
        mirror = self._mirrors[msg.message_ref.account_name]
        try:
            return mirror.get_message_bytes(
                folder=FolderRef(
                    account_name=msg.message_ref.account_name,
                    folder_name=msg.message_ref.folder_name,
                ),
                storage_key=msg.storage_key,
            )
        except Exception:  # noqa: BLE001
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Could not load message for {noun}.", severity="error"
            )
            return None

    def _open_composer(
        self,
        spec: DraftSpec,
        accounts: list[AnyAccount],
        on_done: collections.abc.Callable[[bool | None], None] | None = None,
    ) -> None:
        """Push a ComposeScreen for *spec*.

        Every compose entry point ends here, so the wiring the composer
        needs — index, mirrors, contacts, credentials — is stated once.
        """
        from .compose_screen import ComposeScreen

        screen = ComposeScreen(
            self._config,
            accounts,
            self._index,
            self._mirrors,
            spec,
            contacts=self._contacts,
            credentials=self._credentials,
        )
        if on_done is None:
            self.app.push_screen(screen)  # pyright: ignore[reportUnknownMemberType]
        else:
            self.app.push_screen(screen, on_done)  # pyright: ignore[reportUnknownMemberType]

    def _reply_context(
        self, *, verb: str, noun: str
    ) -> tuple[IndexedMessage, RenderedMessage, list[AnyAccount]] | None:
        """Resolve the message, its rendering and the sendable accounts.

        Reply, reply-all and forward all need exactly this much before
        they can build anything, and each failure is silent-or-notify in
        the same way.
        """
        msg = self.get_current_message()
        if msg is None:
            return None
        accounts = self._sendable_or_notify(verb)
        if accounts is None:
            return None
        raw = self._load_current_raw_or_notify(msg, noun=noun)
        if raw is None:
            return None
        return msg, render_message(raw), accounts

    def compose_new(self, to: str = "") -> None:
        """Open a blank compose screen, optionally pre-filled with *to*."""
        accounts = self._sendable_or_notify("Composing")
        if accounts is None:
            return
        msg = self.get_current_message()
        self._open_composer(
            new_draft(
                accounts=accounts,
                config=self._config,
                prefer_name=None if msg is None else msg.message_ref.account_name,
                to=to,
            ),
            accounts,
        )

    def compose_reply(self) -> None:
        """Open compose pre-filled as a reply to the current message."""
        self._compose_reply(reply_all=False)

    def compose_reply_all(self) -> None:
        """Open compose pre-filled as a reply-all to the current message."""
        self._compose_reply(reply_all=True)

    def _compose_reply(self, *, reply_all: bool) -> None:
        context = self._reply_context(verb="Replying", noun="reply")
        if context is None:
            return
        msg, rendered, accounts = context

        def _on_sent(result: bool | None) -> None:
            if result:
                self._mark_answered(msg)

        self._open_composer(
            reply_draft(
                accounts=accounts,
                config=self._config,
                rendered=rendered,
                prefer_name=msg.message_ref.account_name,
                reply_all=reply_all,
            ),
            accounts,
            _on_sent,
        )

    def compose_forward(self) -> None:
        """Open compose pre-filled as a forward of the current message."""
        context = self._reply_context(verb="Forwarding", noun="forward")
        if context is None:
            return
        msg, rendered, accounts = context
        # The forwarded copy has to exist as a file because attachments
        # are read from disk, but it is scratch space, not something the
        # user chose: it goes in a private directory that the composer
        # deletes when it closes, handed over as an owned path so that
        # cancelling a forward does not strand it.
        tmp_dir = Path(tempfile.mkdtemp(prefix="pony-forward-"))
        attachment = tmp_dir / forward_attachment_name(rendered.subject)
        attachment.write_bytes(rendered.raw_bytes)
        self._open_composer(
            forward_draft(
                accounts=accounts,
                config=self._config,
                rendered=rendered,
                attachment=attachment,
                prefer_name=msg.message_ref.account_name,
            ),
            accounts,
        )

    def compose_from_draft(self) -> None:
        """Open a draft message for editing in ComposeScreen."""
        msg = self.get_current_message()
        if msg is None:
            return
        accounts = self._sendable_or_notify("Composing")
        if accounts is None:
            return
        mirror = self._mirrors.get(msg.message_ref.account_name)
        if mirror is None:
            return
        folder_ref = FolderRef(
            account_name=msg.message_ref.account_name,
            folder_name=msg.message_ref.folder_name,
        )
        try:
            raw = mirror.get_message_bytes(
                folder=folder_ref,
                storage_key=msg.storage_key,
            )
        except Exception:  # noqa: BLE001
            self.app.notify("Could not load draft.", severity="error")  # pyright: ignore[reportUnknownMemberType]
            return

        def _on_draft_done(result: bool | None) -> None:
            if result is None:
                return
            # The draft was either saved (False) or sent (True) — retire the
            # original.  IMAP messages are marked TRASHED so the sync planner
            # expunges them on the server; local-only messages are deleted
            # outright since there is nothing to relay to the server.
            mailbox_actions.retire_message(
                self._mailboxes,
                msg,
                folder_ref,
                # A row with no server UID was never on the server, so
                # there is no expunge to relay: it goes outright.
                expunge_on_server=msg.uid is not None,
            )
            self._reload_folder(folder_ref)

        from .compose_screen import ComposeScreen

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ComposeScreen(
                self._config,
                accounts,
                self._index,
                self._mirrors,
                resumed_draft(
                    accounts=accounts,
                    config=self._config,
                    raw=raw,
                    prefer_name=msg.message_ref.account_name,
                ),
                contacts=self._contacts,
                credentials=self._credentials,
                source_draft=msg,
            ),
            _on_draft_done,
        )

    # ------------------------------------------------------------------
    # Browser / attachments
    # ------------------------------------------------------------------

    def action_open_browser(self) -> None:
        self.open_current_in_browser()

    def open_current_in_browser(self) -> None:
        import tempfile
        import webbrowser

        from ...message_renderer import build_browser_html

        current = self._current_message_bytes(noun="browser view")
        if current is None:
            return
        _msg, raw = current
        with tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w", encoding="utf-8"
        ) as handle:
            handle.write(build_browser_html(raw))
            path = handle.name
        # A browser may itself be a terminal application.  Suspend Textual
        # so it restores terminal modes (including mouse reporting) after.
        with suspend_for_external_program(self.app):
            webbrowser.open(Path(path).as_uri())

    def action_print_pdf(self) -> None:
        """Render the current message to a PDF in a folder the user picks."""
        from ...message_renderer import build_browser_html
        from ..pdf_export import export_pdf_in_thread
        from .save_folder_picker_screen import SaveFolderPickerScreen
        from .save_message_screen import _proposed_body_filename

        msg = self.get_current_message()
        if msg is None:
            return
        mirror = self._mirrors[msg.message_ref.account_name]
        try:
            raw = mirror.get_message_bytes(
                folder=FolderRef(
                    account_name=msg.message_ref.account_name,
                    folder_name=msg.message_ref.folder_name,
                ),
                storage_key=msg.storage_key,
            )
        except Exception:  # noqa: BLE001
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Could not read message from mirror.", severity="error"
            )
            return
        rendered = render_message(raw)
        html = build_browser_html(raw)
        filename = Path(_proposed_body_filename(rendered)).with_suffix(".pdf").name

        def _on_folder(dest: Path | None) -> None:
            if dest is None:
                return
            out = (dest / filename).resolve()
            if not out.is_relative_to(dest.resolve()):
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    "Invalid destination.", severity="error"
                )
                return
            self.run_worker(
                lambda: export_pdf_in_thread(self.app, html, out, dest),
                name="print-pdf",
                thread=True,
            )

        self.app.push_screen(SaveFolderPickerScreen(), _on_folder)  # pyright: ignore[reportUnknownMemberType]

    def action_attachments_open(self) -> None:
        self._prompt_attachments(action_label="Open", then=self._open_indices)

    def action_attachments_save(self) -> None:
        self._prompt_attachments(action_label="Save", then=self._save_indices)

    def _prompt_attachments(
        self,
        *,
        action_label: str,
        then: collections.abc.Callable[[list[int]], None],
    ) -> None:
        current = self._current_message_bytes(noun="attachments")
        if current is None:
            return
        _msg, raw = current
        count = len(render_message(raw).attachments)
        if count == 0:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "No attachments on this message.",
                severity="warning",
            )
            return
        from .attachment_picker_screen import AttachmentPickerScreen

        def _on_selection(indices: list[int] | None) -> None:
            if indices is None:
                return
            then(indices)

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            AttachmentPickerScreen(
                action_label=action_label,
                attachment_count=count,
            ),
            _on_selection,
        )

    def _save_indices(self, indices: list[int]) -> None:
        current = self._current_message_bytes(noun="attachments")
        if current is None:
            return
        _msg, raw = current
        self._save_indices_from(raw, indices)

    def _save_indices_from(self, raw: bytes, indices: list[int]) -> None:
        dest = self._downloads_dir()
        dest.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        missing: list[int] = []
        for idx in indices:
            try:
                name = save_one_attachment(raw, idx, dest)
            except OSError as exc:
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Could not save attachment {idx}: {exc}",
                    severity="error",
                )
                continue
            if name:
                saved.append(name)
            else:
                missing.append(idx)
        if saved:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Saved {len(saved)} attachment(s) to {dest}",
            )
        if missing:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Attachment(s) not found: {', '.join(str(i) for i in missing)}",
                severity="warning",
            )

    def _open_indices(self, indices: list[int]) -> None:
        current = self._current_message_bytes(noun="attachments")
        if current is None:
            return
        _msg, raw = current
        self._open_indices_from(raw, indices)

    def _open_indices_from(self, raw: bytes, indices: list[int]) -> None:
        from ...message_renderer import extract_attachment as _extract_attachment
        from .eml_viewer_screen import EmlViewerScreen

        dest = self._downloads_dir()
        dest.mkdir(parents=True, exist_ok=True)
        missing: list[int] = []
        viewers = self._config.viewers
        for idx in indices:
            command: tuple[str, ...] | None = None
            if raw is not None:
                payload = _extract_attachment(raw, idx)
                if payload is not None and payload.content_type == "message/rfc822":
                    self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                        EmlViewerScreen(payload.data, dest, viewers=viewers)
                    )
                    continue
                if payload is not None:
                    command = resolve_viewer_command(viewers, payload.content_type)
            try:
                name = save_one_attachment(raw, idx, dest)
            except OSError as exc:
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Could not save attachment {idx}: {exc}",
                    severity="error",
                )
                continue
            if name:
                try:
                    # The default viewer may run in this terminal and alter
                    # input modes.  Textual's suspend context restores them.
                    with suspend_for_external_program(self.app):
                        launch_file(dest / name, command)
                except OSError as exc:
                    self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                        f"Could not open {name}: {exc}",
                        severity="error",
                    )
            else:
                missing.append(idx)
        if missing:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Attachment(s) not found: {', '.join(str(i) for i in missing)}",
                severity="warning",
            )

    def action_save_message(self) -> None:
        """Save the current message (body as Markdown + attachments) to disk.

        Opens the item-picker modal first; after the user confirms, opens
        the folder-picker.  Files are written only when both dialogs are
        confirmed.
        """
        from ...message_renderer import extract_attachment, render_message_markdown
        from .save_folder_picker_screen import SaveFolderPickerScreen
        from .save_message_screen import SaveItem, SaveMessageScreen

        msg = self.get_current_message()
        if msg is None:
            return
        mirror = self._mirrors[msg.message_ref.account_name]
        try:
            raw = mirror.get_message_bytes(
                folder=FolderRef(
                    account_name=msg.message_ref.account_name,
                    folder_name=msg.message_ref.folder_name,
                ),
                storage_key=msg.storage_key,
            )
        except Exception:  # noqa: BLE001
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Could not read message from mirror.", severity="error"
            )
            return
        rendered = render_message(raw)

        pending_items: list[SaveItem] = []

        def _on_folder(dest: Path | None) -> None:
            if dest is None:
                return
            dest_resolved = dest.resolve()
            saved = failed = 0
            for item in pending_items:
                try:
                    out = (dest / item.filename).resolve()
                    if not out.is_relative_to(dest_resolved):
                        failed += 1
                        continue
                    if item.kind == "body":
                        md = render_message_markdown(rendered)
                        out.write_text(md, encoding="utf-8")
                    else:
                        idx = int(item.kind.split(":")[1])
                        payload = extract_attachment(raw, idx)
                        if payload:
                            out.write_bytes(payload.data)
                    saved += 1
                except Exception:  # noqa: BLE001
                    failed += 1
            note = f"Saved {saved} file(s) to {dest}"
            if failed:
                note += f" ({failed} failed)"
            self.app.notify(note)  # pyright: ignore[reportUnknownMemberType]

        def _on_items(items: list[SaveItem] | None) -> None:
            if not items:
                return
            pending_items.extend(items)
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                SaveFolderPickerScreen(), _on_folder
            )

        self.app.push_screen(SaveMessageScreen(rendered), _on_items)  # pyright: ignore[reportUnknownMemberType]

    def save_attachment(self, index: int, dest_dir: Path) -> str | None:
        """Save one attachment of the highlighted message."""
        current = self._current_message_bytes(noun="attachments")
        if current is None:
            return None
        _msg, raw = current
        return save_one_attachment(raw, index, dest_dir)

    def save_all_attachments(self, dest_dir: Path) -> list[str]:
        """Save every attachment of the highlighted message."""
        current = self._current_message_bytes(noun="attachments")
        if current is None:
            return []
        _msg, raw = current
        return save_every_attachment(raw, dest_dir)

    def _indices_for(self, raw: bytes, index: str) -> list[int]:
        """``0`` means every attachment; anything else is that one."""
        idx = int(index)
        if idx != 0:
            return [idx]
        return list(range(1, len(render_message(raw).attachments) + 1))

    def action_open_attachment(self, index: str) -> None:
        """Open attachment by 1-based index; 0 means open all."""
        current = self._current_message_bytes(noun="attachments")
        if current is None:
            return
        _msg, raw = current
        self._open_indices_from(raw, self._indices_for(raw, index))

    def action_save_attachment(self, index: str) -> None:
        """Save attachment by 1-based index; 0 means save all."""
        current = self._current_message_bytes(noun="attachments")
        if current is None:
            return
        _msg, raw = current
        self._save_indices_from(raw, self._indices_for(raw, index))

    def _downloads_dir(self) -> Path:
        return self._config.downloads_path or Path.home() / "Downloads"

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def action_harvest_contacts(self) -> None:
        self.harvest_folder_contacts()

    def action_browse_contacts(self) -> None:
        if self._contacts is None:
            self.app.notify("No contacts store available.", severity="warning")  # pyright: ignore[reportUnknownMemberType]
            return
        from .contact_browser_screen import ContactBrowserScreen

        self.app.push_screen(ContactBrowserScreen(self._contacts))  # pyright: ignore[reportUnknownMemberType]

    def harvest_folder_contacts(self) -> None:
        """Harvest To/Cc contacts from every message in the current folder."""
        if self._current_folder_ref is None:
            return
        self._report(
            harvest_folder(
                self._contacts,
                self._index.list_folder_messages(folder=self._current_folder_ref),
                self._current_folder_ref.folder_name,
            ),
        )
