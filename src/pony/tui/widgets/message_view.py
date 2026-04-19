"""Message view panel — bottom-right pane showing the selected message."""

from __future__ import annotations

import logging
import tempfile
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from ...domain import FolderRef, IndexedMessage
from ...protocols import MirrorRepository
from ..message_renderer import (
    RenderedMessage,
    build_browser_html,
    extract_attachment,
    fmt_size,
    render_message,
)

_log = logging.getLogger(__name__)



class MessageViewPanel(VerticalScroll):
    """Scrollable message reader: header block, attachment list, body text.

    Arrow keys / Page-Up / Page-Down scroll within the message.
    ``n`` / ``p`` move to the next / previous message in the list.
    ``q`` closes the panel and returns focus to the message list.
    """

    BORDER_TITLE = "Message"

    BINDINGS = [
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close", show=False),
        Binding("n", "next_message", "Next", show=False),
        Binding("p", "prev_message", "Prev", show=False),
        Binding("space", "page_down_or_next", "Next page", show=False),
        Binding("pagedown", "page_down_or_next", "Next page", show=False),
        Binding("<", "scroll_home", "Top", show=False),
        Binding(">", "scroll_end", "Bottom", show=False),
    ]

    @dataclass
    class CloseRequested(Message):
        """Posted when the user presses q to dismiss the message view."""

    @dataclass
    class NextRequested(Message):
        """Posted when the user presses n to advance to the next message."""

    @dataclass
    class PrevRequested(Message):
        """Posted when the user presses p to go back to the previous message."""

    @dataclass
    class NextUnreadRequested(Message):
        """Posted when space/page-down is pressed at the bottom of the message."""

    def compose(self) -> ComposeResult:
        yield Static("", id="content")

    def action_close(self) -> None:
        self.post_message(self.CloseRequested())

    def action_next_message(self) -> None:
        self.post_message(self.NextRequested())

    def action_prev_message(self) -> None:
        self.post_message(self.PrevRequested())

    def action_page_down_or_next(self) -> None:
        if self.scroll_y >= self.max_scroll_y:
            self.post_message(self.NextUnreadRequested())
        else:
            self.scroll_page_down(animate=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_message(
        self,
        message: IndexedMessage,
        mirror: MirrorRepository,
    ) -> None:
        """Fetch raw bytes from the mirror and render the message."""
        self._rendered: RenderedMessage | None = None
        self._current_message: IndexedMessage | None = message
        try:
            raw = mirror.get_message_bytes(
                folder=FolderRef(
                    account_name=message.message_ref.account_name,
                    folder_name=message.message_ref.folder_name,
                ),
                storage_key=message.storage_key,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("load_message failed for %s", message.message_ref)
            self._set_content(
                f"(could not load message: {type(exc).__name__}: {exc})"
            )
            return
        self._rendered = render_message(raw)
        self._set_content(self._build_markup(self._rendered))
        self.scroll_home(animate=False)

    def clear(self) -> None:
        self._rendered = None
        self._current_message = None
        self._set_content("")

    def open_in_browser(self) -> None:
        """Render the message as a self-contained HTML file and open it."""
        if self._rendered is None:
            return
        html = build_browser_html(self._rendered.raw_bytes)
        with tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w", encoding="utf-8"
        ) as f:
            f.write(html)
            path = f.name
        webbrowser.open(Path(path).as_uri())

    def save_attachment(self, index: int, dest_dir: Path) -> str | None:
        """Save attachment *index* (1-based) to *dest_dir*."""
        if self._rendered is None:
            return None
        payload = extract_attachment(self._rendered.raw_bytes, index)
        if payload is None:
            return None
        dest = dest_dir / payload.filename
        dest.write_bytes(payload.data)
        return payload.filename

    def save_all_attachments(self, dest_dir: Path) -> list[str]:
        """Save all attachments to *dest_dir*."""
        if self._rendered is None:
            return []
        saved: list[str] = []
        for att in self._rendered.attachments:
            result = self.save_attachment(att.index, dest_dir)
            if result:
                saved.append(result)
        return saved

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _set_content(self, text: str) -> None:
        self.query_one("#content", Static).update(text)

    def _build_markup(self, r: RenderedMessage) -> str:
        lines: list[str] = []

        lines.append(f"From:    {r.from_}")
        lines.append(f"To:      {r.to}")
        if r.cc:
            lines.append(f"Cc:      {r.cc}")
        lines.append(f"Date:    {r.date}")
        lines.append(f"Subject: {r.subject}")

        if r.attachments:
            lines.append("")
            lines.append("Attachments:")
            for att in r.attachments:
                name = markup_escape(att.filename)
                link = f"[@click=\"app.open_attachment('{att.index}')\"]"
                lines.append(
                    f"  [{att.index}] {link}{name}[/]"
                    f"  {att.content_type}"
                    f"  ({fmt_size(att.size_bytes)})"
                )

        lines.append("─" * 60)
        lines.append("")
        lines.append(r.body)

        return "\n".join(lines)


