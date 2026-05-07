"""Message view panel — bottom-right pane showing the selected message."""

from __future__ import annotations

import logging
import tempfile
import webbrowser
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from ...domain import FolderMessageSummary, FolderRef
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
        Binding("0", "screen.open_attachment('0')", show=False),
        Binding("1", "screen.open_attachment('1')", show=False),
        Binding("2", "screen.open_attachment('2')", show=False),
        Binding("3", "screen.open_attachment('3')", show=False),
        Binding("4", "screen.open_attachment('4')", show=False),
        Binding("5", "screen.open_attachment('5')", show=False),
        Binding("6", "screen.open_attachment('6')", show=False),
        Binding("7", "screen.open_attachment('7')", show=False),
        Binding("8", "screen.open_attachment('8')", show=False),
        Binding("9", "screen.open_attachment('9')", show=False),
        Binding("ctrl+0", "screen.save_attachment('0')", show=False),
        Binding("ctrl+1", "screen.save_attachment('1')", show=False),
        Binding("ctrl+2", "screen.save_attachment('2')", show=False),
        Binding("ctrl+3", "screen.save_attachment('3')", show=False),
        Binding("ctrl+4", "screen.save_attachment('4')", show=False),
        Binding("ctrl+5", "screen.save_attachment('5')", show=False),
        Binding("ctrl+6", "screen.save_attachment('6')", show=False),
        Binding("ctrl+7", "screen.save_attachment('7')", show=False),
        Binding("ctrl+8", "screen.save_attachment('8')", show=False),
        Binding("ctrl+9", "screen.save_attachment('9')", show=False),
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
        summary: FolderMessageSummary,
        mirror: MirrorRepository,
    ) -> None:
        """Fetch raw bytes from the mirror and render the message."""
        self._rendered: RenderedMessage | None = None
        try:
            raw = mirror.get_message_bytes(
                folder=FolderRef(
                    account_name=summary.message_ref.account_name,
                    folder_name=summary.message_ref.folder_name,
                ),
                storage_key=summary.storage_key,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("load_message failed for %s", summary.message_ref)
            self._set_content(f"(could not load message: {type(exc).__name__}: {exc})")
            return
        self._rendered = render_message(raw)
        self._set_content(self._build_markup(self._rendered))
        self.scroll_home(animate=False)

    def clear(self) -> None:
        self._rendered = None
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

    @property
    def attachment_count(self) -> int:
        """Number of attachments on the currently-loaded message."""
        if self._rendered is None:
            return 0
        return len(self._rendered.attachments)

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

    def header_address(self, idx: int) -> tuple[str, str] | None:
        """Return the (display_name, email) pair at *idx*, or None if out of range."""
        try:
            return self._header_addresses[idx]
        except (AttributeError, IndexError):
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _set_content(self, text: str) -> None:
        self.query_one("#content", Static).update(text)

    def _addr_field(self, label: str, header_str: str) -> str | None:
        pairs = [(d, a) for d, a in getaddresses([header_str]) if a]
        if not pairs:
            return None
        parts: list[str] = []
        for raw_display, addr in pairs:
            display = raw_display.strip().strip("\"'")
            idx = len(self._header_addresses)
            self._header_addresses.append((display, addr))
            shown = markup_escape(f"{display} <{addr}>" if display else addr)
            parts.append(
                f"[@click=\"screen.compose_address('{idx}')\"]{shown}[/]"
                f"[@click=\"screen.harvest_contact('{idx}')\"] (+)[/]"
            )
        return label + ", ".join(parts)

    def _build_markup(self, r: RenderedMessage) -> str:
        self._header_addresses: list[tuple[str, str]] = []
        lines: list[str] = []

        for label, header in (
            ("From:    ", r.from_),
            ("To:      ", r.to),
            ("Cc:      ", r.cc),
        ):
            line = self._addr_field(label, header)
            if line is not None:
                lines.append(line)
        lines.append(f"Date:    {r.date}")
        lines.append(f"Subject: {r.subject}")

        if r.attachments:
            lines.append("")
            lines.append("Attachments:")
            for att in r.attachments:
                name = markup_escape(att.filename)
                link = f"[@click=\"screen.open_attachment('{att.index}')\"]"
                lines.append(
                    f"  [{att.index}] {link}{name}[/]"
                    f"  {att.content_type}"
                    f"  ({fmt_size(att.size_bytes)})"
                )

        lines.append("─" * 60)
        lines.append("")
        lines.append(markup_escape(r.body))

        return "\n".join(lines)
