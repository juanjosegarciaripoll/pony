"""Main three-pane screen: folder tree | message list | message view."""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import os
import subprocess
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header
from textual.worker import Worker

from ...domain import (
    AccountConfig,
    AnyAccount,
    AppConfig,
    Contact,
    FolderMessageSummary,
    FolderRef,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
)
from ...message_copy import copy_message_bytes
from ...protocols import (
    ContactRepository,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from ...sync import ImapSyncService, ProgressInfo, SyncPlan, SyncResult
from ..compose_utils import (
    build_forward_body,
    build_reply_all_recipients,
    build_reply_body,
    forward_subject,
    new_compose_body,
    reply_subject,
)
from ..message_renderer import render_message
from ..widgets.folder_panel import FolderPanel
from ..widgets.message_list import MessageListPanel
from ..widgets.message_view import MessageViewPanel


class MainScreen(Screen[None]):
    """Three-pane mutt-style mail reader screen.

    Layout:
      +-------------+---------------------------+
      |             |      MessageListPanel      |
      | FolderPanel +---------------------------+
      |             |      MessageViewPanel      |
      +-------------+---------------------------+
    """

    BINDINGS = [
        Binding("g", "sync", "Get mail"),
        Binding("c", "compose_new", "Compose"),
        Binding("r", "compose_reply", "Reply"),
        Binding("R", "compose_reply_all", "Reply all"),
        Binding("f", "compose_forward", "Forward"),
        Binding("w", "open_browser", "Web view"),
        Binding("u", "mark_unread", "Mark unread"),
        Binding("!", "toggle_flagged", "Flag"),
        Binding("D", "trash", "Trash"),
        Binding("A", "archive", "Archive"),
        Binding("C", "mark_all_read", "Mark all read"),
        Binding("Y", "copy", "Copy"),
        Binding("M", "move", "Move"),
        Binding("N", "new_folder", "New folder"),
        Binding("O", "attachments_open", "Open att.", show=False),
        Binding("S", "attachments_save", "Save att.", show=False),
        Binding("/", "search", "Search", show=False),
        Binding("G", "goto_folder", "Goto folder", show=False),
        Binding("H", "harvest_contacts", "Harvest contacts", show=False),
        Binding("B", "browse_contacts", "Contacts"),
    ]

    CSS = """
    MainScreen {
        layout: horizontal;
    }

    FolderPanel {
        width: 25%;
        height: 100%;
        border: solid $primary;
    }

    FolderPanel:focus {
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
    }

    MessageListPanel:focus {
        background-tint: transparent;
        border-title-color: $accent;
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
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._config = config
        self._index = index
        self._mirrors = mirrors
        self._credentials = credentials
        self._contacts = contacts
        self._current_folder_ref: FolderRef | None = None
        self._sync_service: ImapSyncService | None = None
        self._sync_plan: SyncPlan | None = None

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

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def on_folder_panel_folder_selected(
        self, event: FolderPanel.FolderSelected
    ) -> None:
        event.stop()
        self._current_folder_ref = event.folder_ref
        view = self.query_one(MessageViewPanel)
        view.clear()
        view.display = False
        self.query_one(MessageListPanel).load_folder(event.folder_ref)
        self.query_one(MessageListPanel).focus()

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

    def action_harvest_contact(self, idx: str) -> None:
        if self._contacts is None:
            return
        pair = self.query_one(MessageViewPanel).header_address(int(idx))
        if pair is None:
            return
        from .contact_edit_screen import ContactEditScreen

        display, addr = pair
        addr = addr.lower().strip()

        existing = self._contacts.find_contact_by_email(email_address=addr)
        if existing is not None:
            if not existing.first_name and not existing.last_name and display.strip():
                parts = display.strip().split()
                first = " ".join(parts[:-1]) if len(parts) > 1 else parts[0]
                last = parts[-1] if len(parts) > 1 else ""
                existing = dataclasses.replace(
                    existing, first_name=first, last_name=last
                )
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                ContactEditScreen(existing, self._contacts),
            )
            return

        display = display.strip()
        parts = display.split()
        if not parts:
            first, last = "", ""
        elif len(parts) == 1:
            first, last = parts[0], ""
        else:
            first, last = " ".join(parts[:-1]), parts[-1]
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ContactEditScreen(
                Contact(id=None, first_name=first, last_name=last, emails=(addr,)),
                self._contacts,
            ),
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

    def on_message_list_panel_search_exited(
        self, event: MessageListPanel.SearchExited
    ) -> None:
        event.stop()
        if self._current_folder_ref is not None:
            self.query_one(MessageListPanel).load_folder(self._current_folder_ref)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def action_sync(self) -> None:
        """Run the two-pass sync flow with in-TUI confirmation."""
        if self._credentials is None:
            self.app.notify("No credentials provider.", severity="warning")  # pyright: ignore[reportUnknownMemberType]
            return

        mirrors = self._mirrors
        credentials = self._credentials

        def mirror_factory(acc: AccountConfig) -> MirrorRepository:
            return mirrors[acc.name]

        service = ImapSyncService(
            config=self._config,
            mirror_factory=mirror_factory,
            index=self._index,
            credentials=credentials,
        )
        self._sync_service = service
        self._sync_plan = None

        from .sync_confirm_screen import SyncConfirmScreen

        screen = SyncConfirmScreen.planning(
            on_confirm=self._start_sync_worker,
        )

        def _on_dismiss(result: bool | None) -> None:
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

        def _run() -> SyncResult:
            return service.execute(plan, progress=_progress)

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
            result = worker.result
            parts: list[str] = []
            if result is not None:
                for ar in result.accounts:
                    fetched = sum(f.fetched for f in ar.folders)
                    merged = sum(f.flag_conflicts_merged for f in ar.folders)
                    if fetched or merged:
                        parts.append(
                            f"{ar.account_name}: +{fetched} msgs, {merged} merged"
                        )
            msg = f"Sync complete.  {'  '.join(parts)}" if parts else "Sync complete."
            self.app.notify(msg)  # pyright: ignore[reportUnknownMemberType]
        if isinstance(self.app.screen, SyncConfirmScreen):  # pyright: ignore[reportUnknownMemberType]
            dismiss_result = True if worker.state == worker.state.SUCCESS else None
            self.app.screen.dismiss(dismiss_result)  # pyright: ignore[reportUnknownMemberType]

    def _sync_progress(self, info: ProgressInfo) -> None:
        """Update the sync screen's status (called from worker thread)."""
        from .sync_confirm_screen import SyncConfirmScreen

        if isinstance(self.app.screen, SyncConfirmScreen):  # pyright: ignore[reportUnknownMemberType]
            self.app.screen.update_progress(info)  # pyright: ignore[reportUnknownMemberType]

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
        from ..search_parser import parse_query

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
        self.query_one(FolderPanel).refresh_folders()
        if self._current_folder_ref is not None:
            self.query_one(MessageListPanel).load_folder(self._current_folder_ref)

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

    def _reload_folder(self, folder_ref: FolderRef) -> None:
        msg_list = self.query_one(MessageListPanel)
        prev_row = msg_list.cursor_row
        msg_list.load_folder(folder_ref)
        # DataTable.clear() resets the cursor to row 0; restore (clamped)
        # so archive/trash/move leave the cursor on the row that took the
        # removed one's slot rather than jumping to the top.
        if prev_row >= 0 and msg_list.row_count > 0:
            msg_list.move_cursor(row=min(prev_row, msg_list.row_count - 1))
        self._refresh_view_after_reload()

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
        updated = dataclasses.replace(
            message,
            local_flags=message.local_flags | {MessageFlag.SEEN},
        )
        self._index.upsert_message(message=updated)
        self.query_one(MessageListPanel).update_from_indexed(updated)
        self.query_one(FolderPanel).refresh_folders()

    def _targets(self) -> list[FolderMessageSummary]:
        """Summaries to act on: marked rows if any, else the cursor row."""
        return self.query_one(MessageListPanel).summaries_to_act_on()

    def _resolve_targets(
        self, summaries: list[FolderMessageSummary]
    ) -> list[IndexedMessage]:
        """Fetch full IndexedMessage rows for each summary in *summaries*.

        Any rows that have vanished from the index (e.g. raced with a
        sync delete) are silently dropped.
        """
        resolved: list[IndexedMessage] = []
        for s in summaries:
            msg = self._index.get_message(message_ref=s.message_ref)
            if msg is not None:
                resolved.append(msg)
        return resolved

    def set_flag(self, flag: MessageFlag, *, present: bool) -> None:
        """Add or remove *flag* on every target message and refresh."""
        targets = self._targets()
        if not targets:
            return
        messages = self._resolve_targets(targets)
        if not messages:
            return
        with self._index.connection():
            for msg in messages:
                new_flags = (
                    msg.local_flags | {flag} if present else msg.local_flags - {flag}
                )
                if new_flags == msg.local_flags:
                    continue
                updated = dataclasses.replace(msg, local_flags=new_flags)
                self._index.upsert_message(message=updated)
        self.query_one(MessageListPanel).clear_marks()
        self._reload_folder(self._folder_ref_from_summary(targets[0]))

    def _mark_answered(self, msg: IndexedMessage) -> None:
        if MessageFlag.ANSWERED in msg.local_flags:
            return
        updated = dataclasses.replace(
            msg, local_flags=msg.local_flags | {MessageFlag.ANSWERED}
        )
        with self._index.connection():
            self._index.upsert_message(message=updated)
        folder_ref = FolderRef(
            account_name=msg.message_ref.account_name,
            folder_name=msg.message_ref.folder_name,
        )
        self._reload_folder(folder_ref)

    def toggle_flag(self, flag: MessageFlag) -> None:
        """Toggle *flag* on every target message and refresh the list."""
        targets = self._targets()
        if not targets:
            return
        messages = self._resolve_targets(targets)
        if not messages:
            return
        with self._index.connection():
            for msg in messages:
                if flag in msg.local_flags:
                    new_flags = msg.local_flags - {flag}
                else:
                    new_flags = msg.local_flags | {flag}
                updated = dataclasses.replace(msg, local_flags=new_flags)
                self._index.upsert_message(message=updated)
        self.query_one(MessageListPanel).clear_marks()
        self._reload_folder(self._folder_ref_from_summary(targets[0]))

    def trash_current_message(self) -> None:
        """Mark every target message as trashed in the index."""
        targets = self._targets()
        if not targets:
            return
        messages = self._resolve_targets(targets)
        if not messages:
            return
        with self._index.connection():
            for msg in messages:
                updated = dataclasses.replace(
                    msg,
                    local_status=MessageStatus.TRASHED,
                )
                self._index.upsert_message(message=updated)
        self.query_one(MessageListPanel).clear_marks()
        self._reload_folder(self._folder_ref_from_summary(targets[0]))

    def archive_current_message(self) -> None:
        """Move every target message into the account's archive folder.

        Moves are applied locally (index + mirror) immediately; the next
        sync pushes them to the server via ``UID MOVE``.  Refuses to
        act when the account has no ``archive_folder`` or when the
        source/target folder cannot accept the move.
        """
        targets = self._targets()
        if not targets:
            return
        # All rows in the current view share one account + source folder,
        # so the precondition checks only need to run once.
        sample = targets[0]
        account = next(
            (
                a
                for a in self._config.accounts
                if a.name == sample.message_ref.account_name
                and isinstance(a, AccountConfig)
            ),
            None,
        )
        if account is None:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Archive requires an IMAP account.",
                severity="warning",
            )
            return
        target = account.archive_folder
        if not target:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "No archive_folder configured for this account.",
                severity="error",
            )
            return
        source = sample.message_ref.folder_name
        if source == target:
            return
        if account.folders.is_read_only(source):
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Cannot archive from read-only folder {source!r}.",
                severity="warning",
            )
            return
        if not account.folders.should_sync(target):
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Archive folder {target!r} is excluded from sync.",
                severity="warning",
            )
            return
        if account.folders.is_read_only(target):
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Archive folder {target!r} is read-only.",
                severity="warning",
            )
            return

        mirror = self._mirrors[account.name]
        source_folder = FolderRef(account_name=account.name, folder_name=source)
        messages = self._resolve_targets(targets)
        archived = 0
        with self._index.connection():
            for msg in messages:
                try:
                    new_storage_key = mirror.move_message_to_folder(
                        folder=source_folder,
                        storage_key=msg.storage_key,
                        target_folder=target,
                    )
                except Exception:  # noqa: BLE001
                    self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                        "Failed to move message in local mirror.",
                        severity="error",
                    )
                    continue
                # Update in place: same row, new folder, PENDING_MOVE
                # status with the original (folder, uid) recorded as the
                # server-side source.  Sync executes UID MOVE (or
                # APPEND+EXPUNGE) and clears the source fields.
                updated = dataclasses.replace(
                    msg,
                    message_ref=MessageRef(
                        account_name=account.name,
                        folder_name=target,
                        id=msg.message_ref.id,
                    ),
                    storage_key=new_storage_key,
                    uid=None,
                    server_flags=frozenset(),
                    extra_imap_flags=frozenset(),
                    synced_at=None,
                    local_status=MessageStatus.PENDING_MOVE,
                    source_folder=source,
                    source_uid=msg.uid,
                )
                self._index.update_message(message=updated)
                archived += 1
        self.query_one(MessageListPanel).clear_marks()
        self._reload_folder(source_folder)
        if archived:
            suffix = "" if archived == 1 else f" ({archived} messages)"
            self.app.notify(f"Archived to {target}{suffix}.")  # pyright: ignore[reportUnknownMemberType]

    def action_mark_all_read(self) -> None:
        """Mark every message in the current folder as read."""
        folder_ref = self._current_folder_ref
        if folder_ref is None:
            return
        count = self._index.mark_folder_read(folder=folder_ref)
        self.query_one(MessageListPanel).load_folder(folder_ref)
        self.query_one(FolderPanel).refresh_folders()
        if count:
            suffix = "s" if count != 1 else ""
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Marked {count} message{suffix} as read.",
            )

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
            title=f"Copy to folder (source: {source_ref.folder_name})",
            exclude=source_ref,
        )

        def _on_dismiss(target: FolderRef | None) -> None:
            if target is None:
                return
            self._copy_to_folder(messages, source_ref, target)

        self.app.push_screen(screen, _on_dismiss)  # pyright: ignore[reportUnknownMemberType]

    def _copy_to_folder(
        self,
        targets: list[IndexedMessage],
        source: FolderRef,
        target: FolderRef,
    ) -> None:
        """Copy every *targets* row from *source* into *target*.

        See :meth:`action_copy` for the MID-rewrite rationale.
        """
        source_mirror = self._mirrors.get(source.account_name)
        target_mirror = self._mirrors.get(target.account_name)
        if source_mirror is None or target_mirror is None:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Missing mirror for source or target account.",
                severity="error",
            )
            return

        rewrite_mid = target.account_name == source.account_name
        copied = 0
        with self._index.connection():
            for msg in targets:
                try:
                    raw_source = source_mirror.get_message_bytes(
                        folder=source,
                        storage_key=msg.storage_key,
                    )
                except Exception:  # noqa: BLE001
                    self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                        f"Could not read source message {msg.subject!r}.",
                        severity="error",
                    )
                    continue
                new_raw, new_mid = copy_message_bytes(
                    raw_source,
                    rewrite_message_id=rewrite_mid,
                )
                try:
                    new_key = target_mirror.store_message(
                        folder=target,
                        raw_message=new_raw,
                    )
                except Exception:  # noqa: BLE001
                    self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                        f"Failed to write copy to {target.folder_name}.",
                        severity="error",
                    )
                    continue
                new_row = dataclasses.replace(
                    msg,
                    message_ref=MessageRef(
                        account_name=target.account_name,
                        folder_name=target.folder_name,
                        id=0,
                    ),
                    message_id=new_mid,
                    storage_key=new_key,
                    uid=None,
                    uid_validity=0,
                    base_flags=frozenset(),
                    server_flags=frozenset(),
                    extra_imap_flags=frozenset(),
                    local_status=MessageStatus.ACTIVE,
                    trashed_at=None,
                    synced_at=None,
                    source_folder=None,
                    source_uid=None,
                )
                self._index.insert_message(message=new_row)
                copied += 1
        self.query_one(MessageListPanel).clear_marks()
        if copied:
            suffix = "" if copied == 1 else f" ({copied} messages)"
            where = (
                target.folder_name
                if target.account_name == source.account_name
                else f"{target.account_name}/{target.folder_name}"
            )
            self.app.notify(f"Copied to {where}{suffix}.")  # pyright: ignore[reportUnknownMemberType]

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
            title=f"Move to folder (source: {source_ref.folder_name})",
            exclude=source_ref,
        )

        def _on_dismiss(target: FolderRef | None) -> None:
            if target is None:
                return
            self._move_to_folder(messages, source_ref, target)

        self.app.push_screen(screen, _on_dismiss)  # pyright: ignore[reportUnknownMemberType]

    def _find_account(self, account_name: str) -> AccountConfig | None:
        """Return the IMAP AccountConfig for *account_name*, or None.

        Local accounts (``LocalAccountConfig``) return ``None`` — they
        don't carry the folder-policy fields the move guards consult.
        """
        for acc in self._config.accounts:
            if acc.name == account_name and isinstance(acc, AccountConfig):
                return acc
        return None

    def _move_to_folder(
        self,
        targets: list[IndexedMessage],
        source: FolderRef,
        target: FolderRef,
    ) -> None:
        """Move every *targets* row from *source* into *target*.

        See :meth:`action_move` for the branch rationale.
        """
        if source == target:
            return

        source_mirror = self._mirrors.get(source.account_name)
        target_mirror = self._mirrors.get(target.account_name)
        if source_mirror is None or target_mirror is None:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Missing mirror for source or target account.",
                severity="error",
            )
            return

        source_account = self._find_account(source.account_name)
        target_account = self._find_account(target.account_name)

        # IMAP-policy guards.  Move is destructive on the source side, so
        # we refuse when either end can't reconcile server-side.
        if source_account is not None and source_account.folders.is_read_only(
            source.folder_name,
        ):
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Cannot move from read-only folder {source.folder_name!r}.",
                severity="warning",
            )
            return
        if target_account is not None:
            if not target_account.folders.should_sync(target.folder_name):
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Target folder {target.folder_name!r} is excluded from sync.",
                    severity="warning",
                )
                return
            if target_account.folders.is_read_only(target.folder_name):
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Target folder {target.folder_name!r} is read-only.",
                    severity="warning",
                )
                return

        same_account = source.account_name == target.account_name
        moved = 0
        with self._index.connection():
            for msg in targets:
                if same_account:
                    ok = self._move_same_account(
                        msg=msg,
                        mirror=source_mirror,
                        source=source,
                        target=target,
                    )
                else:
                    ok = self._move_cross_account(
                        msg=msg,
                        source_mirror=source_mirror,
                        target_mirror=target_mirror,
                        source=source,
                        target=target,
                        source_is_imap=source_account is not None,
                    )
                if ok:
                    moved += 1

        self.query_one(MessageListPanel).clear_marks()
        self._reload_folder(source)
        if moved:
            suffix = "" if moved == 1 else f" ({moved} messages)"
            where = (
                target.folder_name
                if same_account
                else f"{target.account_name}/{target.folder_name}"
            )
            self.app.notify(f"Moved to {where}{suffix}.")  # pyright: ignore[reportUnknownMemberType]

    def _move_same_account(
        self,
        *,
        msg: IndexedMessage,
        mirror: MirrorRepository,
        source: FolderRef,
        target: FolderRef,
    ) -> bool:
        """Rename the mirror file in place, swap the index row.  See
        :meth:`action_move` for why Message-ID is preserved here."""
        try:
            new_key = mirror.move_message_to_folder(
                folder=source,
                storage_key=msg.storage_key,
                target_folder=target.folder_name,
            )
        except Exception:  # noqa: BLE001
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Failed to move message in local mirror.",
                severity="error",
            )
            return False
        # Same-account move: keep the row, mark PENDING_MOVE so sync
        # executes UID MOVE on the server.  No delete-then-insert.
        updated = dataclasses.replace(
            msg,
            message_ref=MessageRef(
                account_name=target.account_name,
                folder_name=target.folder_name,
                id=msg.message_ref.id,
            ),
            storage_key=new_key,
            uid=None,
            server_flags=frozenset(),
            extra_imap_flags=frozenset(),
            synced_at=None,
            local_status=MessageStatus.PENDING_MOVE,
            source_folder=source.folder_name,
            source_uid=msg.uid,
        )
        self._index.update_message(message=updated)
        return True

    def _move_cross_account(
        self,
        *,
        msg: IndexedMessage,
        source_mirror: MirrorRepository,
        target_mirror: MirrorRepository,
        source: FolderRef,
        target: FolderRef,
        source_is_imap: bool,
    ) -> bool:
        """Copy bytes to target, then retire the source side."""
        try:
            raw = source_mirror.get_message_bytes(
                folder=source,
                storage_key=msg.storage_key,
            )
        except Exception:  # noqa: BLE001
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Could not read source message {msg.subject!r}.",
                severity="error",
            )
            return False
        # Cross-account: preserve Message-ID.  Distinct accounts are
        # independent identity namespaces, so a true copy keeps IMAP
        # thread integrity intact.
        new_raw, new_mid = copy_message_bytes(raw, rewrite_message_id=False)
        try:
            new_key = target_mirror.store_message(
                folder=target,
                raw_message=new_raw,
            )
        except Exception:  # noqa: BLE001
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Failed to write copy to {target.folder_name}.",
                severity="error",
            )
            return False
        new_row = dataclasses.replace(
            msg,
            message_ref=MessageRef(
                account_name=target.account_name,
                folder_name=target.folder_name,
                id=0,
            ),
            message_id=new_mid,
            storage_key=new_key,
            uid=None,
            uid_validity=0,
            base_flags=frozenset(),
            server_flags=frozenset(),
            extra_imap_flags=frozenset(),
            local_status=MessageStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
            source_folder=None,
            source_uid=None,
        )
        self._index.insert_message(message=new_row)

        # Retire the source: IMAP sources get TRASHED so the next sync
        # expunges them; local sources have no sync to relay the
        # deletion, so we remove them outright (row + mirror file).
        if source_is_imap:
            trashed = dataclasses.replace(msg, local_status=MessageStatus.TRASHED)
            self._index.update_message(message=trashed)
        else:
            # Best-effort delete: the index row is authoritative; a stray
            # file will be picked up by the mirror integrity scan.
            with contextlib.suppress(Exception):
                source_mirror.delete_message(
                    folder=source,
                    storage_key=msg.storage_key,
                )
            self._index.delete_message(message_ref=msg.message_ref)
        return True

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
        mirror = self._mirrors.get(account_name)
        if mirror is None:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Unknown account {account_name!r}.",
                severity="error",
            )
            return
        try:
            mirror.create_folder(
                account_name=account_name,
                folder_name=folder_name,
            )
        except Exception:  # noqa: BLE001
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Failed to create folder {folder_name!r}.",
                severity="error",
            )
            return
        self.query_one(FolderPanel).refresh_folders()
        # Local accounts have no server side: the creation is terminal.
        # IMAP accounts get the folder pushed upstream on the next sync.
        account = next(
            (a for a in self._config.accounts if a.name == account_name),
            None,
        )
        suffix = (
            "; run sync to propagate." if isinstance(account, AccountConfig) else "."
        )
        self.app.notify(  # pyright: ignore[reportUnknownMemberType]
            f"Folder {folder_name!r} created locally{suffix}"
        )

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

    def _sendable_accounts(self) -> list[AnyAccount]:
        """Accounts that can send via SMTP (IMAP or local-with-SMTP)."""
        return [a for a in self._config.accounts if a.can_send]

    def compose_new(self, to: str = "") -> None:
        """Open a blank compose screen, optionally pre-filled with *to*."""
        from .compose_screen import ComposeInitial, ComposeScreen

        accounts = self._sendable_accounts()
        if not accounts:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Composing requires an IMAP account (SMTP is needed to send).",
                severity="warning",
            )
            return
        # Prefer the current message's account when it's sendable; otherwise
        # default to the first IMAP account.
        msg = self.get_current_message()
        account = next(
            (
                a
                for a in accounts
                if msg is not None and a.name == msg.message_ref.account_name
            ),
            accounts[0],
        )
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ComposeScreen(
                self._config,
                accounts,
                self._index,
                self._mirrors,
                ComposeInitial(
                    account_name=account.name,
                    to=to,
                    body=new_compose_body(account.signature),
                    markdown_mode=(
                        account.markdown_compose or self._config.markdown_compose
                    ),
                ),
                contacts=self._contacts,
            )
        )

    def compose_reply(self) -> None:
        """Open compose pre-filled as a reply to the current message."""
        from .compose_screen import ComposeInitial, ComposeScreen

        msg = self.get_current_message()
        if msg is None:
            return
        accounts = self._sendable_accounts()
        if not accounts:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Replying requires an IMAP account (SMTP is needed to send).",
                severity="warning",
            )
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
            self.app.notify("Could not load message for reply.", severity="error")  # pyright: ignore[reportUnknownMemberType]
            return
        rendered = render_message(raw)
        # Source may be a local account; fall back to the first IMAP account
        # when that's the case so the dropdown has a valid default.
        account = next(
            (a for a in accounts if a.name == msg.message_ref.account_name),
            accounts[0],
        )

        def _on_reply_sent(result: bool | None) -> None:
            if result:
                self._mark_answered(msg)

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ComposeScreen(
                self._config,
                accounts,
                self._index,
                self._mirrors,
                ComposeInitial(
                    account_name=account.name,
                    to=rendered.from_,
                    subject=reply_subject(rendered.subject),
                    body=build_reply_body(rendered, signature=account.signature),
                    markdown_mode=(
                        account.markdown_compose or self._config.markdown_compose
                    ),
                ),
                contacts=self._contacts,
            ),
            _on_reply_sent,
        )

    def compose_reply_all(self) -> None:
        """Open compose pre-filled as a reply-all to the current message."""
        from .compose_screen import ComposeInitial, ComposeScreen

        msg = self.get_current_message()
        if msg is None:
            return
        accounts = self._sendable_accounts()
        if not accounts:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Replying requires an IMAP account (SMTP is needed to send).",
                severity="warning",
            )
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
            self.app.notify("Could not load message for reply.", severity="error")  # pyright: ignore[reportUnknownMemberType]
            return
        rendered = render_message(raw)
        account = next(
            (a for a in accounts if a.name == msg.message_ref.account_name),
            accounts[0],
        )
        to, cc = build_reply_all_recipients(
            rendered,
            self_address=account.email_address,
        )

        def _on_reply_all_sent(result: bool | None) -> None:
            if result:
                self._mark_answered(msg)

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ComposeScreen(
                self._config,
                accounts,
                self._index,
                self._mirrors,
                ComposeInitial(
                    account_name=account.name,
                    to=to,
                    cc=cc,
                    subject=reply_subject(rendered.subject),
                    body=build_reply_body(rendered, signature=account.signature),
                    markdown_mode=(
                        account.markdown_compose or self._config.markdown_compose
                    ),
                ),
                contacts=self._contacts,
            ),
            _on_reply_all_sent,
        )

    def compose_forward(self) -> None:
        """Open compose pre-filled as a forward of the current message."""
        from .compose_screen import ComposeInitial, ComposeScreen

        msg = self.get_current_message()
        if msg is None:
            return
        accounts = self._sendable_accounts()
        if not accounts:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Forwarding requires an IMAP account (SMTP is needed to send).",
                severity="warning",
            )
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
            self.app.notify("Could not load message for forward.", severity="error")  # pyright: ignore[reportUnknownMemberType]
            return
        rendered = render_message(raw)
        account = next(
            (a for a in accounts if a.name == msg.message_ref.account_name),
            accounts[0],
        )
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ComposeScreen(
                self._config,
                accounts,
                self._index,
                self._mirrors,
                ComposeInitial(
                    account_name=account.name,
                    subject=forward_subject(rendered.subject),
                    body=build_forward_body(rendered, signature=account.signature),
                    markdown_mode=(
                        account.markdown_compose or self._config.markdown_compose
                    ),
                    forwarded_message=raw,
                ),
                contacts=self._contacts,
            )
        )

    # ------------------------------------------------------------------
    # Browser / attachments
    # ------------------------------------------------------------------

    def action_open_browser(self) -> None:
        self.open_current_in_browser()

    def open_current_in_browser(self) -> None:
        self.query_one(MessageViewPanel).open_in_browser()

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
        count = self.query_one(MessageViewPanel).attachment_count
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
        dest = self._downloads_dir()
        dest.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        missing: list[int] = []
        for idx in indices:
            name = self.save_attachment(idx, dest)
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
        dest = self._downloads_dir()
        dest.mkdir(parents=True, exist_ok=True)
        missing: list[int] = []
        for idx in indices:
            name = self.save_attachment(idx, dest)
            if name:
                self._launch_file(dest / name)
            else:
                missing.append(idx)
        if missing:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Attachment(s) not found: {', '.join(str(i) for i in missing)}",
                severity="warning",
            )

    def save_attachment(self, index: int, dest_dir: Path) -> str | None:
        return self.query_one(MessageViewPanel).save_attachment(index, dest_dir)

    def save_all_attachments(self, dest_dir: Path) -> list[str]:
        return self.query_one(MessageViewPanel).save_all_attachments(dest_dir)

    def action_open_attachment(self, index: str) -> None:
        """Open attachment by 1-based index; 0 means open all."""
        idx = int(index)
        if idx == 0:
            count = self.query_one(MessageViewPanel).attachment_count
            self._open_indices(list(range(1, count + 1)))
        else:
            self._open_indices([idx])

    def action_save_attachment(self, index: str) -> None:
        """Save attachment by 1-based index; 0 means save all."""
        idx = int(index)
        if idx == 0:
            count = self.query_one(MessageViewPanel).attachment_count
            self._save_indices(list(range(1, count + 1)))
        else:
            self._save_indices([idx])

    def _downloads_dir(self) -> Path:
        return self._config.downloads_path or Path.home() / "Downloads"

    @staticmethod
    def _launch_file(path: Path) -> None:
        """Open *path* with the OS default application."""
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":  # pyright: ignore[reportUnreachable]
            subprocess.run(["open", str(path)], check=False)  # noqa: S603 S607
        else:  # pyright: ignore[reportUnreachable]
            subprocess.run(["xdg-open", str(path)], check=False)  # noqa: S603 S607

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
        if self._contacts is None or self._current_folder_ref is None:
            return
        messages = list(
            self._index.list_folder_messages(folder=self._current_folder_ref)
        )
        self._contacts.harvest_contacts(messages)
        self.app.notify(  # pyright: ignore[reportUnknownMemberType]
            f"Harvested contacts from {len(messages)} message(s) "
            f"in {self._current_folder_ref.folder_name}."
        )
