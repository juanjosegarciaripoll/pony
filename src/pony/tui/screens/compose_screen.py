"""Compose screen — full-screen email editor."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import tempfile
from email.message import EmailMessage
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Paste
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Input,
    Label,
    Select,
    Static,
    TextArea,
)

from chronos.outcome import Outcome

from ...compose_utils import (
    build_email_message,
    format_display_address,
    split_address_list,
)
from ...composer import DraftSpec
from ...contact_actions import contact_for_address
from ...delivery import deliver_message, file_message
from ...domain import (
    AnyAccount,
    AppConfig,
    IndexedMessage,
)
from ...keymap import MAIL_KEYMAP, Surface, for_surface
from ...mailbox_actions import Mailboxes
from ...protocols import (
    ContactRepository,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from ...smtp_sender import DEFAULT_CONNECT_ATTEMPTS as SMTP_CONNECT_ATTEMPTS
from ..bindings import to_textual
from ..widgets.address_input import AddressInput

_log = logging.getLogger(__name__)


class AttachmentsBar(Vertical):
    """Container holding one _AttachRow per attachment and a persistent + button."""

    can_focus = False

    class FilesDropped(Message):
        def __init__(self, paths: list[Path]) -> None:
            super().__init__()
            self.paths = paths

    def compose(self) -> ComposeResult:
        with Horizontal(id="attach-add-row", classes="attach-row"):
            yield Label(
                "(no attachments)",
                id="attach-empty-label",
                classes="attach-name row-field",
            )
            yield Static("", classes="row-btn-spacer")
            yield Button("+", classes="attach-add-btn")

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


class _AttachRow(Horizontal):
    """One attachment row: [filename 1fr] [× remove]."""

    DEFAULT_CSS = ""

    def __init__(self, path: Path, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._path = path

    @property
    def attachment_path(self) -> Path:
        return self._path

    def compose(self) -> ComposeResult:
        yield Label(self._path.name, classes="attach-name row-field")
        yield Button("×", classes="attach-remove-btn")


TO_CONTAINER = "to-container"
CC_CONTAINER = "cc-container"
BCC_CONTAINER = "bcc-container"

_PLACEHOLDERS = {
    TO_CONTAINER: "recipient@example.com",
    CC_CONTAINER: "(optional)",
    BCC_CONTAINER: "(optional)",
}


def _split_addresses(addresses: str) -> list[str]:
    """One address per row, always non-empty so a blank row is rendered.

    Delegates the actual splitting so a comma inside a quoted display
    name does not turn one recipient into two rows.
    """
    parts = split_address_list(addresses)
    return parts if parts else [""]


class _AddrRow(Horizontal):
    """One address input row: [Input] [× remove] [+ add]."""

    DEFAULT_CSS = ""

    def __init__(
        self,
        value: str = "",
        *,
        contacts: ContactRepository | None = None,
        placeholder: str = "(optional)",
        input_id: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._addr_value = value
        self._contacts = contacts
        self._placeholder = placeholder
        self._input_id = input_id

    def compose(self) -> ComposeResult:
        if self._contacts is not None:
            from ..widgets.contact_suggester import RecipientInput

            yield RecipientInput(
                self._contacts,
                self._addr_value,
                placeholder=self._placeholder,
                input_id=self._input_id,
                input_classes="addr-input field-input",
                classes="row-field",
            )
        else:
            yield AddressInput(
                self._addr_value,
                placeholder=self._placeholder,
                id=self._input_id,
                classes="addr-input field-input row-field",
            )
        yield Button("×", classes="addr-remove-btn")
        yield Button("+", classes="addr-add-btn")

    @property
    def address_input(self) -> Input:
        """The row's text field, whether or not completion wraps it."""
        return self.query_one(Input)


class ComposeScreen(Screen[bool]):
    """Full-screen email composer.

    Ctrl+S or Alt+S sends the message.  Escape prompts to save a draft.
    Alt+E opens the body in the configured external editor.
    Alt+A adds a file attachment.  Alt+M toggles Markdown mode.
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
        height: auto;
    }

    .field-row > .field-label {
        content-align: right top;
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

    /* ── Shared row geometry ────────────────────────────── */

    /* One width for every field that carries row buttons, so the × and
       + columns line up down the whole header — recipients and
       attachments alike.  Stretched to full width the buttons ended up
       on the far right of a wide terminal, a screen away from the label,
       with no way to tell which row owned which button.  Overrides
       RecipientInput's own width, which is DEFAULT_CSS and therefore
       lower priority than a screen rule. */
    .row-field {
        width: 1fr;
        max-width: 60;
    }

    /* Holds the × column open on a row that has no × of its own, so the
       + beneath a stack of rows still lands in the + column. */
    .row-btn-spacer {
        width: 3;
        height: 1;
    }

    #attachments-bar {
        height: auto;
        width: 1fr;
    }

    .attach-row {
        height: 1;
    }

    .attach-name {
        color: $text-muted;
        padding: 0 1;
    }

    .attach-remove-btn {
        width: 3;
        height: 1;
        min-width: 3;
        border: none;
        background: $boost;
        color: $text-muted;
    }

    .attach-add-btn {
        width: 3;
        height: 1;
        min-width: 3;
        border: none;
        background: $boost;
        color: $accent;
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
        height: auto;
    }

    /* Both buttons keep their cell whether or not they are drawn: + is
       hidden rather than removed on every row but the last, so × does
       not shift sideways from one row to the next. */
    .addr-remove-btn {
        width: 3;
        height: 1;
        min-width: 3;
        border: none;
        background: $boost;
        color: $text-muted;
    }

    .addr-add-btn {
        width: 3;
        height: 1;
        min-width: 3;
        border: none;
        background: $boost;
        color: $accent;
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

    # Composer-specific actions use Alt+letter so that standard text-editing
    # shortcuts (Ctrl+X cut, Ctrl+C copy, Ctrl+V paste, Ctrl+Z undo, …) keep
    # working inside the body TextArea and address Input fields.
    BINDINGS = to_textual(for_surface(MAIL_KEYMAP, Surface.COMPOSE))

    def __init__(
        self,
        config: AppConfig,
        accounts: list[AnyAccount],
        index: IndexRepository,
        mirrors: dict[str, MirrorRepository],
        initial: DraftSpec,
        contacts: ContactRepository | None = None,
        source_draft: IndexedMessage | None = None,
        credentials: CredentialsProvider | None = None,
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
        self._source_draft = source_draft
        self._credentials = credentials
        self._attachment_paths: list[Path] = list(initial.attachment_paths)
        self._markdown_mode: bool = initial.markdown_mode
        # True while the send worker is in flight.  ctrl+s is a priority
        # binding and stays live during the send, so without this a slow
        # server invites a second, duplicate delivery.
        self._sending: bool = False

    def compose(self) -> ComposeResult:

        from_options: list[tuple[str, str]] = [
            (self._account_from_label(a), a.name) for a in self._accounts
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
            for label, container_id, initial in (
                ("To:", TO_CONTAINER, self._initial.to),
                ("Cc:", CC_CONTAINER, self._initial.cc),
                ("Bcc:", BCC_CONTAINER, self._initial.bcc),
            ):
                with Horizontal(classes="addr-group"):
                    yield Label(label, classes="field-label")
                    with Vertical(id=container_id, classes="addr-container"):
                        for position, addr in enumerate(_split_addresses(initial)):
                            yield _AddrRow(
                                addr,
                                contacts=self._contacts,
                                placeholder=_PLACEHOLDERS[container_id],
                                # A stable handle on the first recipient
                                # field, which is where focus lands and
                                # what drives the composer's tests.
                                input_id="to-input"
                                if container_id == TO_CONTAINER and position == 0
                                else None,
                                classes="addr-row",
                            )
            with Horizontal(classes="field-row"):
                yield Label("Subject:", classes="field-label")
                yield Input(
                    self._initial.subject,
                    id="subject-input",
                    classes="field-input",
                )
            with Horizontal(classes="addr-group"):
                yield Label("Attach:", classes="field-label")
                yield AttachmentsBar(id="attachments-bar")
        # ── Body fills remaining screen space ──
        yield TextArea(self._initial.body, id="body-area")
        yield Static("", id="compose-footer")

    def on_mount(self) -> None:
        self._refresh_attachments_bar()
        self._refresh_body_title()
        for container_id in (TO_CONTAINER, CC_CONTAINER, BCC_CONTAINER):
            self._refresh_add_buttons(self.query_one(f"#{container_id}", Vertical))
        # Focus the To field for new mail, body for reply/forward.
        if self._initial.to:
            self.query_one("#body-area", TextArea).focus()
        else:
            self._first_input(TO_CONTAINER).focus()
        self._maybe_prompt_missing_name()

    def on_unmount(self) -> None:
        """Delete the scratch files this composer was given ownership of.

        Only paths passed as ``owned_paths`` are touched — a forward's
        materialised copy of the original, never a file the user picked
        from their own disk.  This runs however the screen goes away
        (sent, cancelled, or the app shutting down), so the copy cannot
        outlive the compose that needed it.
        """
        for path in self._initial.owned_paths:
            with contextlib.suppress(OSError):
                path.unlink()
            # The forward path puts its file in a directory of its own;
            # take that with it, but never recursively and never when
            # anything else has appeared inside.
            with contextlib.suppress(OSError):
                path.parent.rmdir()

    def _maybe_prompt_missing_name(self) -> None:
        """Open the contact editor when the selected account has no display name."""
        account = self._get_account()
        if account is None:
            return
        if self._account_from_address(account) != account.email_address:
            return
        if self._contacts is None:
            self.notify(
                "From: will use only your email address. "
                "Set full_name in your config file to add a display name.",
                severity="warning",
                timeout=8,
            )
            return
        from .contact_edit_screen import ContactEditScreen

        # No display name to offer, so this either finds the record the
        # address already has or starts an empty one for it.
        contact = contact_for_address(self._contacts, "", account.email_address)
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ContactEditScreen(contact, self._contacts),
        )

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
        elif "attach-add-btn" in btn.classes:
            self.action_add_attachment()
            event.stop()
        elif "attach-remove-btn" in btn.classes:
            row = btn.parent
            if isinstance(row, _AttachRow):
                with contextlib.suppress(ValueError):
                    self._attachment_paths.remove(row.attachment_path)
                self._refresh_attachments_bar()
            event.stop()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @property
    def _mailboxes(self) -> Mailboxes:
        """The stores filing a draft or a sent copy works on."""
        return Mailboxes(self._config, self._index, self._mirrors)

    def _report(self, outcome: Outcome) -> None:
        """Say everything an operation had to say, in the order it said it."""
        for notice in outcome.notices:
            self.notify(notice.text, severity=notice.severity)

    def _build_message(self, account: AnyAccount) -> EmailMessage:
        """Assemble the current form state into an ``EmailMessage``.

        Sending and draft-saving both go through here so that the two can
        never disagree about what the form contains — notably the
        threading headers, which a draft has to carry in order to still
        land in its thread when it is resumed and sent later.
        """
        return build_email_message(
            from_address=self._account_from_address(account),
            to=self._collect_field(TO_CONTAINER),
            cc=self._collect_field(CC_CONTAINER),
            bcc=self._collect_field(BCC_CONTAINER),
            subject=self.query_one("#subject-input", Input).value.strip(),
            body=self.query_one("#body-area", TextArea).text,
            attachment_paths=self._attachment_paths,
            markdown_mode=self._markdown_mode,
            in_reply_to=self._initial.in_reply_to,
            references=self._initial.references,
        )

    def action_send(self) -> None:
        """Validate fields, then hand the send to a background worker.

        Everything that touches widgets happens here, on the message
        pump; only the SMTP conversation is moved off it.  A connect to
        an unreachable server can take the full connect timeout times the
        attempt count, and doing that inline froze the whole app.
        """
        if self._sending:
            self.notify("A send is already in progress.", severity="warning")
            return

        to = self._collect_field(TO_CONTAINER)
        if not to:
            self.notify("'To' field is required.", severity="error")
            self._first_input(TO_CONTAINER).focus()
            return

        account = self._get_account()
        if account is None:
            self.notify("Could not determine sending account.", severity="error")
            return

        cc = self._collect_field(CC_CONTAINER)
        msg = self._build_message(account)

        self._sending = True
        self._deliver(account, msg, to, cc)

    @work(exclusive=True, group="compose-send")
    async def _deliver(
        self,
        account: AnyAccount,
        msg: EmailMessage,
        to: str,
        cc: str,
    ) -> None:
        """Hand the send to :func:`pony.delivery.deliver_message`.

        What is left here is the part that is a user interface: saying
        which server is being dialled, saying that it did not answer the
        first time, and leaving the form up when the send failed so the
        text is not lost.  Everything else — the pre-send draft, the
        SMTP conversation, the Sent copy, the harvested addresses — is
        one call, the same one the browser makes.

        The worker body runs on the event loop, so widget access here is
        safe; only the blocking call is handed to a thread.
        """
        assert account.smtp is not None
        host = account.smtp.host
        self.notify(f"Connecting to {host}…", timeout=6)

        def _progress(attempt: int, total: int) -> None:
            # Called from the worker thread, so it must hop back.
            if attempt == 1:
                return
            self.app.call_from_thread(  # pyright: ignore[reportUnknownMemberType]
                self.notify,
                f"{host} did not answer — retrying ({attempt} of {total})",
                severity="warning",
                timeout=6,
            )

        try:
            outcome = await asyncio.to_thread(
                deliver_message,
                self._mailboxes,
                msg,
                account,
                credentials=self._credentials,
                contacts=self._contacts,
                to=to,
                cc=cc,
                connect_timeout=self._config.smtp_connect_timeout_seconds,
                connect_attempts=SMTP_CONNECT_ATTEMPTS,
                on_attempt=_progress,
            )
        finally:
            self._sending = False

        self._report(outcome)
        if outcome.failed:
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Prompt to save draft if the form has any content."""
        has_content = bool(
            self._collect_field(TO_CONTAINER)
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
                    self._report(
                        file_message(
                            self._mailboxes,
                            self._build_message(account).as_bytes(),
                            account,
                            folder_hint="Drafts",
                            override=account.drafts_folder,
                        ).outcome,
                    )
            self.dismiss(False)

        from .save_draft_screen import SaveDraftScreen

        self.app.push_screen(SaveDraftScreen(), _on_save)  # pyright: ignore[reportUnknownMemberType]

    def action_toggle_markdown(self) -> None:
        self._markdown_mode = not self._markdown_mode
        self._refresh_body_title()
        state = "ON" if self._markdown_mode else "OFF"
        self.notify(f"Markdown {state}", timeout=2)

    def _refresh_body_title(self) -> None:
        editor = self._config.editor
        parts: list[str] = []
        if editor and Path(editor).is_file():
            parts.append(f"Alt+E → {Path(editor).name}")
        parts.append("Markdown ON" if self._markdown_mode else "Markdown OFF")
        self.query_one("#body-area", TextArea).border_title = "  ".join(parts)
        md = "[b $success][MD] [/b $success]" if self._markdown_mode else ""
        k = "[b $warning]"
        e = "[/b $warning]"
        keys = (
            f"{k}^S{e} Send  {k}Esc{e} Cancel  "
            f"{k}Alt+A{e} Attach  {k}Alt+E{e} Editor  {k}Alt+M{e} Markdown"
        )
        self.query_one("#compose-footer", Static).update(f"{md}{keys}")

    def action_edit_external(self) -> None:
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

    def action_add_attachment(self) -> None:
        """Push the file-picker screen."""
        from .add_attachment_screen import AddAttachmentScreen

        # If the + button triggered this, redirect focus away from it afterwards
        # so Textual's button-focus style doesn't linger.
        focused = self.focused
        restore_to = (
            self.query_one("#body-area", TextArea)
            if isinstance(focused, Button)
            else focused
        )

        def _on_path(path_str: str | None) -> None:
            if path_str:
                p = Path(path_str)
                if not p.is_file():
                    self.notify(f"Not a file: {path_str}", severity="error")
                else:
                    self._attachment_paths.append(p)
                    self._refresh_attachments_bar()
            if restore_to is not None:
                self.call_after_refresh(restore_to.focus)

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

    def _first_input(self, container_id: str) -> Input:
        """The topmost address field of *container_id*."""
        container = self.query_one(f"#{container_id}", Vertical)
        return next(iter(container.query(Input)))

    def _refresh_add_buttons(self, container: Vertical) -> None:
        """Draw the + button only on the last address row.

        Hidden, not removed: the button keeps its cell either way, so ×
        sits at the same column on every row of the group.
        """
        rows = list(container.query(_AddrRow))
        for row in rows[:-1]:
            row.query_one(".addr-add-btn", Button).visible = False
        if rows:
            rows[-1].query_one(".addr-add-btn", Button).visible = True

    def _new_addr_row(self, container_id: str, value: str = "") -> _AddrRow:
        return _AddrRow(
            value,
            contacts=self._contacts,
            placeholder=_PLACEHOLDERS[container_id],
            classes="addr-row",
        )

    def _add_addr_row(self, container_id: str) -> None:
        """Append a new empty address row and refresh + button visibility."""
        container = self.query_one(f"#{container_id}", Vertical)
        new_row = self._new_addr_row(container_id)
        container.mount(new_row)

        def _after() -> None:
            self._refresh_add_buttons(container)
            new_row.address_input.focus()

        self.call_after_refresh(_after)

    def _remove_addr_row(self, btn: Button) -> None:
        """Remove the address row that owns *btn*, or clear it if it is the last one."""
        row = btn.parent
        if not isinstance(row, _AddrRow):
            return
        container = row.parent
        if not isinstance(container, Vertical):
            return
        rows = list(container.query(_AddrRow))
        if len(rows) > 1:
            remaining_rows = [candidate for candidate in rows if candidate is not row]
            for candidate in remaining_rows[:-1]:
                candidate.query_one(".addr-add-btn", Button).visible = False
            remaining_rows[-1].query_one(".addr-add-btn", Button).visible = True
            row.remove()
        else:
            row.address_input.value = ""

    # ------------------------------------------------------------------
    # Splitting a typed or pasted address list into one row per address
    # ------------------------------------------------------------------

    async def on_input_blurred(self, event: Input.Blurred) -> None:
        """Split a multi-address field once the user has left it.

        Waiting for the blur keeps typing predictable: splitting on the
        comma itself would move the cursor to another row mid-address,
        and would have to second-guess a comma inside a quoted display
        name that the user has not finished typing.
        """
        await self._split_addr_row(event.input)

    async def on_address_input_pasted(self, event: AddressInput.Pasted) -> None:
        """Split straight away on paste — there is no half-typed address."""
        event.stop()
        await self._split_addr_row(event.input)

    async def _split_addr_row(self, inp: Input) -> None:
        """Give each address in *inp* a row of its own, in place."""
        node: Widget | None = inp
        while node is not None and not isinstance(node, _AddrRow):
            node = node.parent if isinstance(node.parent, Widget) else None
        if node is None:
            return
        container = node.parent
        if not isinstance(container, Vertical) or container.id is None:
            return
        parts = split_address_list(inp.value)
        if not parts:
            return

        if inp.value != parts[0]:
            # Leaving the field tidies it too: a trailing separator or
            # stray whitespace goes, so the row shows exactly what it
            # contributes to the header.  The rewrite is not typing, so
            # it must not reopen the completion list under a field the
            # user has already left.
            with inp.prevent(Input.Changed):
                inp.value = parts[0]
        if len(parts) == 1:
            return

        # Mounted next to the row they came from, so a list expanded in
        # the middle of a group does not jump to the end of it.  Awaited
        # because _refresh_add_buttons reaches into the new rows, which
        # have not composed their buttons until the mount completes.
        await container.mount_all(
            [self._new_addr_row(container.id, part) for part in parts[1:]],
            after=node,
        )
        self._refresh_add_buttons(container)

    def _account_from_address(self, account: AnyAccount) -> str:
        if account.full_name:
            return format_display_address(account.full_name, account.email_address)
        if self._contacts is not None:
            contact = self._contacts.find_contact_by_email(
                email_address=account.email_address
            )
            if contact is not None and contact.display_name:
                return format_display_address(
                    contact.display_name, account.email_address
                )
        return account.email_address

    def _account_from_label(self, account: AnyAccount) -> str:
        from_addr = self._account_from_address(account)
        if from_addr != account.email_address:
            return from_addr
        return f"{account.name} <{account.email_address}>"

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
        bar = self.query_one("#attachments-bar", AttachmentsBar)
        for row in list(bar.query(_AttachRow)):
            row.remove()
        has_paths = bool(self._attachment_paths)
        hint = "(no attachments)" if not has_paths else ""
        bar.query_one("#attach-empty-label", Label).update(hint)
        paths = list(self._attachment_paths)

        def _mount() -> None:
            if paths:
                bar.mount(
                    *[_AttachRow(p, classes="attach-row") for p in paths],
                    before="#attach-add-row",
                )

        self.call_after_refresh(_mount)
