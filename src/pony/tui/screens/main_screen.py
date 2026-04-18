"""Main three-pane screen: folder tree | message list | message view."""

from __future__ import annotations

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
    AppConfig,
    FolderRef,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
)
from ...protocols import (
    ContactRepository,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from ...sync import ImapSyncService, ProgressInfo, SyncPlan, SyncResult
from ..compose_utils import (
    build_forward_body,
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
        Binding("f", "compose_forward", "Forward"),
        Binding("w", "open_browser", "Web view"),
        Binding("R", "mark_read", "Mark read", show=False),
        Binding("u", "mark_unread", "Mark unread"),
        Binding("!", "toggle_flagged", "Flag"),
        Binding("d", "trash", "Trash"),
        Binding("A", "archive", "Archive"),
        Binding("N", "new_folder", "New folder"),
        Binding("ctrl+1", "save_attachment('1')", "Save att. 1", show=False),
        Binding("ctrl+2", "save_attachment('2')", "Save att. 2", show=False),
        Binding("ctrl+3", "save_attachment('3')", "Save att. 3", show=False),
        Binding("ctrl+0", "save_all_attachments", "Save all att.", show=False),
        Binding("1", "open_attachment('1')", "Open att. 1", show=False),
        Binding("2", "open_attachment('2')", "Open att. 2", show=False),
        Binding("3", "open_attachment('3')", "Open att. 3", show=False),
        Binding("0", "open_all_attachments", "Open all att.", show=False),
        Binding("/", "search", "Search", show=False),
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

    #right-pane {
        width: 75%;
        height: 100%;
    }

    MessageListPanel {
        height: 1fr;
        border: solid $primary;
    }

    MessageViewPanel {
        height: 2fr;
        border: solid $primary;
        display: none;
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
        yield FolderPanel(self._config, self._index, id="folder-panel")
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
        mirror = self._mirrors[event.message.message_ref.account_name]
        view = self.query_one(MessageViewPanel)
        view.load_message(event.message, mirror)
        view.display = True
        view.focus()
        self._mark_seen(event.message)
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
        msg = msg_list.move_cursor_to_next_unread()
        if msg is None:
            return
        mirror = self._mirrors[msg.message_ref.account_name]
        view = self.query_one(MessageViewPanel)
        view.load_message(msg, mirror)
        self._mark_seen(msg)

    def on_message_view_panel_prev_requested(
        self, event: MessageViewPanel.PrevRequested
    ) -> None:
        event.stop()
        self._navigate_from_view(delta=-1)

    def _navigate_from_view(self, delta: int) -> None:
        msg_list = self.query_one(MessageListPanel)
        msg = msg_list.move_cursor_by(delta)
        if msg is None:
            return
        mirror = self._mirrors[msg.message_ref.account_name]
        view = self.query_one(MessageViewPanel)
        view.load_message(msg, mirror)
        self._mark_seen(msg)

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
            if not result:
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
                self.app.screen.dismiss(False)  # pyright: ignore[reportUnknownMemberType]
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
                self.app.screen.dismiss(False)  # pyright: ignore[reportUnknownMemberType]
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
                    parts.append(f"{ar.account_name}: +{fetched} msgs, {merged} merged")
            self.app.notify("Sync complete. " + "  ".join(parts))  # pyright: ignore[reportUnknownMemberType]
        if isinstance(self.app.screen, SyncConfirmScreen):  # pyright: ignore[reportUnknownMemberType]
            self.app.screen.dismiss(  # pyright: ignore[reportUnknownMemberType]
                worker.state == worker.state.SUCCESS,
            )
        self.call_after_refresh(self._refresh_after_sync)

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
        return self.query_one(MessageListPanel).get_selected_message()

    def _folder_ref_from_message(self, msg: IndexedMessage) -> FolderRef:
        return FolderRef(
            account_name=msg.message_ref.account_name,
            folder_name=msg.message_ref.folder_name,
        )

    def _reload_current_folder(self, msg: IndexedMessage) -> None:
        self.query_one(MessageListPanel).load_folder(self._folder_ref_from_message(msg))

    def _mark_seen(self, message: IndexedMessage) -> None:
        if MessageFlag.SEEN in message.local_flags:
            return
        updated = dataclasses.replace(
            message,
            local_flags=message.local_flags | {MessageFlag.SEEN},
        )
        self._index.upsert_message(message=updated)
        self.query_one(MessageListPanel).update_message(updated)

    def _targets(self) -> list[IndexedMessage]:
        """Messages to act on: marked rows if any, else the cursor row."""
        return self.query_one(MessageListPanel).messages_to_act_on()

    def set_flag(self, flag: MessageFlag, *, present: bool) -> None:
        """Add or remove *flag* on every target message and refresh."""
        targets = self._targets()
        if not targets:
            return
        with self._index.connection():
            for msg in targets:
                new_flags = (
                    msg.local_flags | {flag} if present
                    else msg.local_flags - {flag}
                )
                if new_flags == msg.local_flags:
                    continue
                updated = dataclasses.replace(msg, local_flags=new_flags)
                self._index.upsert_message(message=updated)
        self.query_one(MessageListPanel).clear_marks()
        self._reload_current_folder(targets[0])

    def toggle_flag(self, flag: MessageFlag) -> None:
        """Toggle *flag* on every target message and refresh the list."""
        targets = self._targets()
        if not targets:
            return
        with self._index.connection():
            for msg in targets:
                if flag in msg.local_flags:
                    new_flags = msg.local_flags - {flag}
                else:
                    new_flags = msg.local_flags | {flag}
                updated = dataclasses.replace(msg, local_flags=new_flags)
                self._index.upsert_message(message=updated)
        self.query_one(MessageListPanel).clear_marks()
        self._reload_current_folder(targets[0])

    def trash_current_message(self) -> None:
        """Mark every target message as trashed in the index."""
        targets = self._targets()
        if not targets:
            return
        with self._index.connection():
            for msg in targets:
                updated = dataclasses.replace(
                    msg, local_status=MessageStatus.TRASHED,
                )
                self._index.upsert_message(message=updated)
        self.query_one(MessageListPanel).clear_marks()
        self._reload_current_folder(targets[0])

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
        archived = 0
        with self._index.connection():
            for msg in targets:
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
                new_row = dataclasses.replace(
                    msg,
                    message_ref=MessageRef(
                        account_name=account.name,
                        folder_name=target,
                        rfc5322_id=msg.message_ref.rfc5322_id,
                    ),
                    storage_key=new_storage_key,
                    uid=None,
                    server_flags=frozenset(),
                    extra_imap_flags=frozenset(),
                    synced_at=None,
                )
                self._index.delete_message(message_ref=msg.message_ref)
                self._index.upsert_message(message=new_row)
                archived += 1
        self.query_one(MessageListPanel).clear_marks()
        self._reload_current_folder(sample)
        if archived:
            suffix = "" if archived == 1 else f" ({archived} messages)"
            self.app.notify(f"Archived to {target}{suffix}.")  # pyright: ignore[reportUnknownMemberType]

    def action_mark_read(self) -> None:
        self.set_flag(MessageFlag.SEEN, present=True)

    def action_mark_unread(self) -> None:
        self.set_flag(MessageFlag.SEEN, present=False)

    def action_toggle_flagged(self) -> None:
        self.toggle_flag(MessageFlag.FLAGGED)

    def action_trash(self) -> None:
        self.trash_current_message()

    def action_archive(self) -> None:
        self.archive_current_message()

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

    def _account_for_new_folder(self) -> AccountConfig | None:
        """Pick the account to create a folder in.

        Uses the currently-open folder's account; falls back to the sole
        IMAP account when exactly one is configured.  Folders can only be
        created on IMAP accounts — local accounts have no server side.
        """
        if self._current_folder_ref is not None:
            name = self._current_folder_ref.account_name
            for a in self._config.accounts:
                if a.name == name and isinstance(a, AccountConfig):
                    return a
        imap = [a for a in self._config.accounts if isinstance(a, AccountConfig)]
        if len(imap) == 1:
            return imap[0]
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
        self.app.notify(  # pyright: ignore[reportUnknownMemberType]
            f"Folder {folder_name!r} created locally; run sync to propagate."
        )

    # ------------------------------------------------------------------
    # Compose entry points
    # ------------------------------------------------------------------

    def action_compose_new(self) -> None:
        self.compose_new()

    def action_compose_reply(self) -> None:
        self.compose_reply()

    def action_compose_forward(self) -> None:
        self.compose_forward()

    def compose_new(self) -> None:
        """Open a blank compose screen."""
        from .compose_screen import ComposeInitial, ComposeScreen

        accounts = list(self._config.accounts)
        msg = self.get_current_message()
        account_name = (
            msg.message_ref.account_name if msg is not None else accounts[0].name
        )
        account = next((a for a in accounts if a.name == account_name), accounts[0])
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ComposeScreen(
                self._config,
                accounts,
                self._index,
                self._mirrors,
                ComposeInitial(
                    account_name=account_name,
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
        accounts = list(self._config.accounts)
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
                    account_name=msg.message_ref.account_name,
                    to=rendered.from_,
                    subject=reply_subject(rendered.subject),
                    body=build_reply_body(rendered, signature=account.signature),
                    markdown_mode=(
                        account.markdown_compose or self._config.markdown_compose
                    ),
                ),
                contacts=self._contacts,
            )
        )

    def compose_forward(self) -> None:
        """Open compose pre-filled as a forward of the current message."""
        from .compose_screen import ComposeInitial, ComposeScreen

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
            self.app.notify("Could not load message for forward.", severity="error")  # pyright: ignore[reportUnknownMemberType]
            return
        rendered = render_message(raw)
        accounts = list(self._config.accounts)
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
                    account_name=msg.message_ref.account_name,
                    subject=forward_subject(rendered.subject),
                    body=build_forward_body(rendered, signature=account.signature),
                    markdown_mode=(
                        account.markdown_compose or self._config.markdown_compose
                    ),
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

    def action_save_attachment(self, index_str: str) -> None:
        try:
            idx = int(index_str)
        except ValueError:
            return
        dest = Path.home() / "Downloads"
        dest.mkdir(parents=True, exist_ok=True)
        name = self.save_attachment(idx, dest)
        if name:
            self.app.notify(f"Saved: {dest / name}")  # pyright: ignore[reportUnknownMemberType]
        else:
            self.app.notify(f"Attachment {idx} not found.", severity="warning")  # pyright: ignore[reportUnknownMemberType]

    def action_save_all_attachments(self) -> None:
        dest = Path.home() / "Downloads"
        dest.mkdir(parents=True, exist_ok=True)
        names = self.save_all_attachments(dest)
        if names:
            self.app.notify(f"Saved {len(names)} attachment(s) to {dest}")  # pyright: ignore[reportUnknownMemberType]
        else:
            self.app.notify("No attachments to save.", severity="warning")  # pyright: ignore[reportUnknownMemberType]

    def action_open_attachment(self, index_str: str) -> None:
        try:
            idx = int(index_str)
        except ValueError:
            return
        dest = Path.home() / "Downloads"
        dest.mkdir(parents=True, exist_ok=True)
        name = self.save_attachment(idx, dest)
        if name:
            self._launch_file(dest / name)
        else:
            self.app.notify(f"Attachment {idx} not found.", severity="warning")  # pyright: ignore[reportUnknownMemberType]

    def action_open_all_attachments(self) -> None:
        dest = Path.home() / "Downloads"
        dest.mkdir(parents=True, exist_ok=True)
        names = self.save_all_attachments(dest)
        if names:
            for name in names:
                self._launch_file(dest / name)
        else:
            self.app.notify("No attachments to save.", severity="warning")  # pyright: ignore[reportUnknownMemberType]

    def save_attachment(self, index: int, dest_dir: Path) -> str | None:
        return self.query_one(MessageViewPanel).save_attachment(index, dest_dir)

    def save_all_attachments(self, dest_dir: Path) -> list[str]:
        return self.query_one(MessageViewPanel).save_all_attachments(dest_dir)

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
