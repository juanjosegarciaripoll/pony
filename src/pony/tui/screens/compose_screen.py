"""Compose screen — full-screen email editor."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Paste
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    Input,
    Label,
    Select,
    Static,
    TextArea,
)

from ...domain import (
    AnyAccount,
    AppConfig,
    Contact,
    FolderRef,
    IndexedMessage,
    MessageRef,
)
from ...folder_utils import find_folder
from ...message_projection import project_rfc822_message
from ...protocols import ContactRepository, IndexRepository, MirrorRepository
from ...smtp_sender import SMTPError
from ...smtp_sender import send_message as smtp_send
from ..compose_utils import build_email_message

_log = logging.getLogger(__name__)


class AttachmentsBar(Static):
    can_focus = True

    class FilesDropped(Message):
        def __init__(self, paths: list[Path]) -> None:
            super().__init__()
            self.paths = paths

    def on_paste(self, event: Paste) -> None:
        from urllib.parse import unquote

        candidates: list[Path] = []
        for raw_line in event.text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line = line.strip("'\"")
            if line.startswith("file://"):
                line = unquote(line[len("file://") :])
            p = Path(line)
            if not p.is_file():
                self.notify(f"Not a file: {line}", severity="warning")
                return
            candidates.append(p)
        if candidates:
            event.stop()
            self.post_message(self.FilesDropped(candidates))


def _split_addresses(addresses: str) -> list[str]:
    """Split comma-separated addresses into a list, always non-empty."""
    parts = [a.strip() for a in addresses.split(",") if a.strip()]
    return parts if parts else [""]


class _AddrRow(Horizontal):
    """One address input row: [Input 1fr] [× button]."""

    DEFAULT_CSS = ""

    def __init__(
        self, value: str = "", *, suggester: object = None, **kwargs: object
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._addr_value = value
        self._suggester = suggester

    def compose(self) -> ComposeResult:
        yield Input(
            self._addr_value,
            placeholder="(optional)",
            suggester=self._suggester,  # type: ignore[arg-type]
            classes="addr-input field-input",
        )
        yield Button("×", classes="addr-remove-btn")
        yield Button("+", classes="addr-add-btn")


@dataclass
class ComposeInitial:
    """Pre-filled values passed to the compose form."""

    account_name: str
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body: str = ""
    markdown_mode: bool = False
    forwarded_message: bytes | None = None


class ComposeScreen(Screen[bool]):
    """Full-screen email composer.

    Ctrl+S sends the message.  Escape prompts to save a draft.
    Ctrl+E opens the body in the configured external editor.
    Ctrl+A adds a file attachment.
    """

    BORDER_TITLE = "Compose"
    INHERIT_BINDINGS = False  # App-level mail keybindings must not leak in here

    CSS = """
    ComposeScreen {
        layout: vertical;
    }

    /* ── Header fields block ─────────────────────────────── */

    #header-fields {
        height: auto;
        background: $boost;
        border-bottom: solid $primary;
        padding: 0 1;
    }

    .field-row {
        height: 1;
    }

    .field-label {
        width: 10;
        color: $text-muted;
        content-align: right middle;
    }

    .field-input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0 1;
        background: $boost;
    }

    /* Make Select compact: override its inner SelectCurrent component */
    #from-select {
        width: 1fr;
        height: 1;
        border: none;
        background: $boost;
    }

    #from-select > SelectCurrent {
        height: 1;
        border: none;
        padding: 0 1;
        background: $boost;
    }

    #attachments-bar {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    #attachments-bar:focus {
        color: $text;
        background: $accent 20%;
    }

    /* ── Dynamic address rows (Cc / Bcc) ────────────────── */

    .addr-group {
        height: auto;
    }

    .addr-group > .field-label {
        content-align: right top;
    }

    .addr-container {
        width: 1fr;
        height: auto;
    }

    .addr-row {
        height: 1;
    }

    .addr-remove-btn {
        width: 3;
        height: 1;
        min-width: 3;
        border: none;
        background: $boost;
        color: $text-muted;
    }

    .addr-add-btn {
        display: none;
        width: auto;
        height: 1;
        border: none;
        background: $boost;
        color: $accent;
        padding: 0 1;
    }

    /* ── Footer bar ─────────────────────────────────────── */

    #compose-footer {
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }

    /* ── Body area ───────────────────────────────────────── */

    #body-area {
        height: 1fr;
        border: none;
        padding: 1;
    }

    """

    # ctrl+x is the prefix key for all composer-specific actions.
    # It avoids conflicts with Textual's built-in TextArea shortcuts
    # (ctrl+a = select-all, ctrl+e = cursor-to-EOL, etc.).
    # ctrl+s and escape are kept as direct shortcuts for send/cancel since
    # neither is claimed by Textual widgets.
    BINDINGS = [
        Binding("ctrl+s", "send", "Send", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+x", "prefix", "ctrl+x …", show=False, priority=True),
    ]

    def __init__(
        self,
        config: AppConfig,
        accounts: list[AnyAccount],
        index: IndexRepository,
        mirrors: dict[str, MirrorRepository],
        initial: ComposeInitial,
        contacts: ContactRepository | None = None,
        **kwargs: object,
    ) -> None:
        # ``accounts`` must all satisfy ``account.can_send``; MainScreen
        # filters the list before constructing this screen.  We therefore
        # know that every listed account has ``smtp`` and ``username``
        # set, and ``action_send`` asserts that contract at use time.
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._config = config
        self._accounts = accounts
        self._index = index
        self._mirrors = mirrors
        self._initial = initial
        self._contacts = contacts
        self._attachment_paths: list[Path] = []
        self._forwarded_message: bytes | None = initial.forwarded_message
        self._prefix_active: bool = False
        self._focus_before_prefix: object = None
        self._markdown_mode: bool = initial.markdown_mode

    def compose(self) -> ComposeResult:
        from ..widgets.contact_suggester import ContactSuggester

        suggester = ContactSuggester(self._contacts) if self._contacts else None

        from_options: list[tuple[str, str]] = [
            (f"{a.name} <{a.email_address}>", a.name) for a in self._accounts
        ]
        # ── Header block (auto height, one row per field) ──
        with Vertical(id="header-fields"):
            with Horizontal(classes="field-row"):
                yield Label("From:", classes="field-label")
                yield Select(
                    from_options,
                    value=self._initial.account_name,
                    id="from-select",
                    allow_blank=False,
                )
            with Horizontal(classes="field-row"):
                yield Label("To:", classes="field-label")
                yield Input(
                    self._initial.to,
                    placeholder="recipient@example.com",
                    suggester=suggester,
                    id="to-input",
                    classes="field-input",
                )
            with Horizontal(classes="addr-group"):
                yield Label("Cc:", classes="field-label")
                with Vertical(id="cc-container", classes="addr-container"):
                    for addr in _split_addresses(self._initial.cc):
                        yield _AddrRow(addr, suggester=suggester, classes="addr-row")
            with Horizontal(classes="addr-group"):
                yield Label("Bcc:", classes="field-label")
                with Vertical(id="bcc-container", classes="addr-container"):
                    for addr in _split_addresses(self._initial.bcc):
                        yield _AddrRow(addr, suggester=suggester, classes="addr-row")
            with Horizontal(classes="field-row"):
                yield Label("Subject:", classes="field-label")
                yield Input(
                    self._initial.subject,
                    id="subject-input",
                    classes="field-input",
                )
            yield AttachmentsBar("", id="attachments-bar")
        # ── Body fills remaining screen space ──
        yield TextArea(self._initial.body, id="body-area")
        yield Static("", id="compose-footer")

    def on_mount(self) -> None:
        self._refresh_attachments_bar()
        self._refresh_body_title()
        self._refresh_add_buttons(self.query_one("#cc-container", Vertical))
        self._refresh_add_buttons(self.query_one("#bcc-container", Vertical))
        # Focus the To field for new mail, body for reply/forward.
        if self._initial.to:
            self.query_one("#body-area", TextArea).focus()
        else:
            self.query_one("#to-input", Input).focus()

    # ------------------------------------------------------------------
    # Button handler
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button
        if "addr-add-btn" in btn.classes:
            row = btn.parent
            if isinstance(row, _AddrRow):
                container = row.parent
                if isinstance(container, Vertical) and container.id:
                    self._add_addr_row(container.id)
            event.stop()
        elif "addr-remove-btn" in btn.classes:
            self._remove_addr_row(btn)
            event.stop()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_send(self) -> None:
        """Validate fields, send via SMTP, save copy to Sent folder."""
        to = self.query_one("#to-input", Input).value.strip()
        if not to:
            self.notify("'To' field is required.", severity="error")
            self.query_one("#to-input", Input).focus()
            return

        account = self._get_account()
        if account is None:
            self.notify("Could not determine sending account.", severity="error")
            return

        cc = self._collect_field("cc-container")
        bcc = self._collect_field("bcc-container")
        msg = build_email_message(
            from_address=account.email_address,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=self.query_one("#subject-input", Input).value.strip(),
            body=self.query_one("#body-area", TextArea).text,
            attachment_paths=self._attachment_paths,
            markdown_mode=self._markdown_mode,
            forwarded_message=self._forwarded_message,
        )

        # The dropdown only shows ``can_send`` accounts (see
        # MainScreen._sendable_accounts), so ``smtp`` / ``username`` /
        # ``password`` are always set here.  Assert for the type checker.
        assert account.smtp is not None
        assert account.username is not None
        if not account.password:
            self.notify(
                "Send requires a 'password' configured on this account.",
                severity="error",
            )
            return

        raw = msg.as_bytes()

        # Save to Drafts before attempting SMTP so the message survives a
        # connection failure.  On success we remove this draft entry and store
        # the message in Sent instead.
        draft_entry = self._save_to_folder(
            raw,
            account,
            folder_hint="Drafts",
            override=account.drafts_folder,
            silent=True,
        )

        try:
            smtp_send(
                smtp=account.smtp,
                username=account.username,
                password=account.password,
                msg=msg,
            )
        except (SMTPError, ValueError) as exc:
            suffix = " (message saved to Drafts)" if draft_entry is not None else ""
            self.notify(f"Send failed: {exc}{suffix}", severity="error")
            _log.error("SMTP send failed: %s", exc)
            return

        # SMTP succeeded — remove the draft and record the sent copy.
        if draft_entry is not None:
            self._delete_local_message(account, draft_entry)
        self._save_to_folder(
            raw,
            account,
            folder_hint="Sent",
            override=account.sent_folder,
        )
        self._harvest_outgoing(to, cc)
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Prompt to save draft if the form has any content."""
        has_content = bool(
            self.query_one("#to-input", Input).value.strip()
            or self.query_one("#subject-input", Input).value.strip()
            or self.query_one("#body-area", TextArea).text.strip()
        )
        if not has_content:
            self.dismiss()
            return

        def _on_save(save: bool | None) -> None:
            if save:
                account = self._get_account()
                if account is not None:
                    msg = build_email_message(
                        from_address=account.email_address,
                        to=self.query_one("#to-input", Input).value.strip(),
                        cc=self._collect_field("cc-container"),
                        bcc=self._collect_field("bcc-container"),
                        subject=self.query_one("#subject-input", Input).value.strip(),
                        body=self.query_one("#body-area", TextArea).text,
                        attachment_paths=self._attachment_paths,
                        markdown_mode=self._markdown_mode,
                    )
                    self._save_to_folder(
                        msg.as_bytes(),
                        account,
                        folder_hint="Drafts",
                        override=account.drafts_folder,
                    )
            self.dismiss(False)

        from .save_draft_screen import SaveDraftScreen

        self.app.push_screen(SaveDraftScreen(), _on_save)  # pyright: ignore[reportUnknownMemberType]

    def action_prefix(self) -> None:
        """Activate the ctrl+x prefix.

        Steals focus from the current input widget so the next keypress is
        delivered directly to the screen (no widget to consume it first).
        """
        self._prefix_active = True
        self._focus_before_prefix = self.focused
        self.set_focus(None)
        self.notify(
            "ctrl+x — s=send  a=attach  e=editor  m=markdown  c=cancel", timeout=3
        )

    def on_key(self, event: object) -> None:
        """Handle the second key of a ctrl+x chord."""
        from textual.events import Key

        if not isinstance(event, Key):
            return
        if not self._prefix_active:
            return
        self._prefix_active = False
        # Restore focus to whichever field the user was in.
        from textual.widget import Widget

        if isinstance(self._focus_before_prefix, Widget):
            self.set_focus(self._focus_before_prefix)
        self._focus_before_prefix = None
        event.prevent_default()
        event.stop()
        key = event.key
        if key == "s":
            self.action_send()
        elif key == "a":
            self._do_add_attachment()
        elif key == "e":
            self._do_edit_external()
        elif key == "m":
            self._toggle_markdown()
        elif key == "c":
            self.action_cancel()
        else:
            self.notify(f"ctrl+x {key} — unknown command", severity="warning")

    def _harvest_outgoing(self, to: str, cc: str) -> None:
        """Upsert To and Cc addresses into the contacts store after a send."""
        if self._contacts is None:
            return
        for display_name, addr in getaddresses([to, cc]):
            addr = addr.lower().strip()
            if not addr:
                continue
            existing = self._contacts.find_contact_by_email(
                email_address=addr,
            )
            if existing is not None:
                continue
            parts = display_name.strip().split()
            if len(parts) > 1:
                first = " ".join(parts[:-1])
                last = parts[-1]
            else:
                first = parts[0] if parts else ""
                last = ""
            self._contacts.upsert_contact(
                contact=Contact(
                    id=None,
                    first_name=first,
                    last_name=last,
                    emails=(addr,),
                )
            )

    def _toggle_markdown(self) -> None:
        self._markdown_mode = not self._markdown_mode
        self._refresh_body_title()
        state = "ON" if self._markdown_mode else "OFF"
        self.notify(f"Markdown {state}", timeout=2)

    def _refresh_body_title(self) -> None:
        editor = self._config.editor
        parts: list[str] = []
        if editor and Path(editor).is_file():
            parts.append(f"ctrl+x e → {Path(editor).name}")
        parts.append("Markdown ON" if self._markdown_mode else "Markdown OFF")
        self.query_one("#body-area", TextArea).border_title = "  ".join(parts)
        from rich.text import Text

        t = Text()
        if self._markdown_mode:
            t.append("[MD] ", style="bold green")
        t.append("^S", style="bold yellow")
        t.append(" Send  ")
        t.append("Esc", style="bold yellow")
        t.append(" Cancel")
        self.query_one("#compose-footer", Static).update(t)

    def _do_edit_external(self) -> None:
        """Write body to a temp file, open the configured editor, read back."""
        editor = self._config.editor
        if not editor or not Path(editor).is_file():
            self.notify("No external editor configured.", severity="warning")
            return

        body_area = self.query_one("#body-area", TextArea)
        current_text = body_area.text

        with tempfile.NamedTemporaryFile(
            suffix=".txt", delete=False, mode="w", encoding="utf-8"
        ) as f:
            f.write(current_text)
            tmppath = Path(f.name)

        with self.app.suspend():  # pyright: ignore[reportUnknownMemberType]
            subprocess.run([editor, str(tmppath)], check=False)  # noqa: S603

        new_text = tmppath.read_text(encoding="utf-8")
        tmppath.unlink(missing_ok=True)
        body_area.load_text(new_text)

    def _do_add_attachment(self) -> None:
        """Push the file-picker screen."""
        from .add_attachment_screen import AddAttachmentScreen

        def _on_path(path_str: str | None) -> None:
            if not path_str:
                return
            p = Path(path_str)
            if not p.is_file():
                self.notify(f"Not a file: {path_str}", severity="error")
                return
            self._attachment_paths.append(p)
            self._refresh_attachments_bar()

        self.app.push_screen(AddAttachmentScreen(), _on_path)  # pyright: ignore[reportUnknownMemberType]

    def on_attachments_bar_files_dropped(
        self, event: AttachmentsBar.FilesDropped
    ) -> None:
        self._attachment_paths.extend(event.paths)
        self._refresh_attachments_bar()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_field(self, container_id: str) -> str:
        """Comma-joined non-empty address inputs in *container_id*."""
        container = self.query_one(f"#{container_id}", Vertical)
        return ", ".join(
            inp.value.strip() for inp in container.query(Input) if inp.value.strip()
        )

    def _refresh_add_buttons(self, container: Vertical) -> None:
        """Show the + button only on the last address row."""
        rows = list(container.query(_AddrRow))
        for row in rows[:-1]:
            row.query_one(".addr-add-btn", Button).display = False
        if rows:
            rows[-1].query_one(".addr-add-btn", Button).display = True

    def _add_addr_row(self, container_id: str) -> None:
        """Append a new empty address row and refresh + button visibility."""
        from ..widgets.contact_suggester import ContactSuggester

        suggester = ContactSuggester(self._contacts) if self._contacts else None
        container = self.query_one(f"#{container_id}", Vertical)
        new_row = _AddrRow(suggester=suggester, classes="addr-row")
        container.mount(new_row)

        def _after() -> None:
            self._refresh_add_buttons(container)
            new_row.query_one(Input).focus()

        self.call_after_refresh(_after)

    def _remove_addr_row(self, btn: Button) -> None:
        """Remove the address row that owns *btn*, or clear it if it is the last one."""
        row = btn.parent
        if not isinstance(row, _AddrRow):
            return
        container = row.parent
        if not isinstance(container, Vertical):
            return
        if len(list(container.query(_AddrRow))) > 1:
            row.remove()
            self.call_after_refresh(lambda: self._refresh_add_buttons(container))
        else:
            row.query_one(Input).value = ""

    def _get_account(self) -> AnyAccount | None:
        sel = self.query_one("#from-select", Select)  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
        selected = sel.value  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
        if selected is Select.BLANK:
            return None
        for acc in self._accounts:
            if acc.name == selected:
                return acc
        return None

    def _refresh_attachments_bar(self) -> None:
        if not self._attachment_paths:
            text = "Attachments: (none)  [ctrl+x a  or drop files here]"
        else:
            names = "  ".join(p.name for p in self._attachment_paths)
            count = len(self._attachment_paths)
            text = f"Attachments ({count}): {names}  [ctrl+x a to add more]"
        self.query_one("#attachments-bar", AttachmentsBar).update(text)

    def _save_to_folder(
        self,
        raw: bytes,
        account: AnyAccount,
        folder_hint: str,
        override: str | None,
        *,
        silent: bool = False,
    ) -> IndexedMessage | None:
        """Store *raw* bytes in the named folder and insert the index row.

        Returns the inserted ``IndexedMessage`` (with its assigned id) on
        success, or ``None`` if the folder could not be found or the write
        failed.  Sync's PushAppendOp picks up rows where ``uid IS NULL``.

        Pass ``silent=True`` to suppress user-visible notifications on
        folder-not-found and write errors (used when the save is best-effort).
        """
        mirror = self._mirrors.get(account.name)
        if mirror is None:
            _log.warning(
                "No mirror for account %r — cannot save to %s",
                account.name,
                folder_hint,
            )
            return None

        folder_refs = mirror.list_folders(account_name=account.name)
        folder_names = [fr.folder_name for fr in folder_refs]
        folder_name = override or find_folder(folder_names, folder_hint)
        if folder_name is None:
            if not silent:
                self.notify(
                    f"Could not find '{folder_hint}' folder for {account.name}",
                    severity="warning",
                )
            return None

        folder_ref = FolderRef(account_name=account.name, folder_name=folder_name)
        try:
            storage_key = mirror.store_message(folder=folder_ref, raw_message=raw)
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "Failed to store message in %s/%s: %s",
                account.name,
                folder_name,
                exc,
            )
            if not silent:
                self.notify(
                    f"Could not save to {folder_hint}: {exc}", severity="warning"
                )
            return None

        projected = project_rfc822_message(
            message_ref=MessageRef(
                account_name=account.name,
                folder_name=folder_name,
                id=0,
            ),
            raw_message=raw,
            storage_key=storage_key,
        )
        return self._index.insert_message(message=projected)

    def _delete_local_message(self, account: AnyAccount, entry: IndexedMessage) -> None:
        """Remove a locally stored message from the mirror and the index."""
        mirror = self._mirrors.get(account.name)
        if mirror is not None:
            folder_ref = FolderRef(
                account_name=entry.message_ref.account_name,
                folder_name=entry.message_ref.folder_name,
            )
            try:
                mirror.delete_message(folder=folder_ref, storage_key=entry.storage_key)
            except Exception as exc:  # noqa: BLE001
                _log.warning("Could not delete draft from mirror: %s", exc)
        self._index.delete_message(message_ref=entry.message_ref)
