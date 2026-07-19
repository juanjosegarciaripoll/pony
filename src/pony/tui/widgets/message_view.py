"""Message view panel — bottom-right pane showing the selected message."""

from __future__ import annotations

import logging
import tempfile
import webbrowser
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path

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
from ..terminal import suspend_for_external_program

_log = logging.getLogger(__name__)


def _escape(text: str) -> str:
    """Escape text for Textual markup.

    Rich 14.x ``markup.escape`` only escapes ``[`` when followed by a
    tag-like pattern, but Textual's tokenizer treats *any* bare ``[`` as an
    open-tag opener.  A plain ``replace`` is the safe choice here.

    Control characters (especially ESC / \\x1b) are stripped so that a
    malicious email header cannot inject terminal escape sequences into
    the output stream.  Tab, newline and carriage-return are kept.
    """
    text = "".join(ch for ch in text if ch >= " " or ch in "\t\n\r")
    return text.replace("[", "\\[")


_FORMAT_RICH = {
    "B1": "[bold]",
    "B0": "[/bold]",
    "I1": "[italic]",
    "I0": "[/italic]",
    "U1": "[underline]",
    "U0": "[/underline]",
    "S1": "[strike]",
    "S0": "[/strike]",
}


def _render_body(body: str, links: tuple[tuple[str, str], ...]) -> str:
    """Convert a styled body string into Rich markup.

    Handles ``\\x00LINK:{idx}\\x00`` (clickable link tokens) and
    ``\\x00B1\\x00`` / ``\\x00B0\\x00`` etc. (bold/italic/underline/strike).
    """
    segments = body.split("\x00")
    parts: list[str] = []
    for seg in segments:
        if seg.startswith("LINK:"):
            try:
                idx = int(seg[5:])
                kind, _ = links[idx]
            except (ValueError, IndexError):
                continue
            if kind == "web":
                parts.append(f"[@click=\"screen.activate_link('{idx}')\"]\\[🌐 ↗][/]")
            else:
                parts.append(f"[@click=\"screen.compose_link('{idx}')\"]\\[✉ ][/]")
        elif seg in _FORMAT_RICH:
            parts.append(_FORMAT_RICH[seg])
        else:
            parts.append(_escape(seg))
    return "".join(parts)


def _unique_path(dest_dir: Path, filename: str) -> Path:
    """Return dest_dir/filename, appending -N before the extension if it exists."""
    # Strip path traversal components and control chars from an untrusted filename.
    safe = Path(filename).name
    safe = "".join(ch for ch in safe if ch >= " ") or "attachment"
    candidate = dest_dir / safe
    if not candidate.exists():
        return candidate
    stem = Path(safe).stem
    suffix = Path(safe).suffix
    n = 1
    while True:
        candidate = dest_dir / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


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
                message_id=summary.message_id,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("load_message failed for %s", summary.message_ref)
            msg = f"(could not load message: {type(exc).__name__}: {exc})"
            self._set_content(_escape(msg))
            return
        self._rendered = render_message(raw)
        self._set_content(self._build_markup(self._rendered))
        self.scroll_home(animate=False)

    def load_bytes(self, raw: bytes) -> None:
        """Render a message from raw RFC 5322 bytes (no mirror lookup needed)."""
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
        # A browser may itself be a terminal application.  Suspend Textual so
        # it restores terminal modes (including mouse reporting) afterwards.
        with suspend_for_external_program(self.app):
            webbrowser.open(Path(path).as_uri())

    @property
    def attachment_count(self) -> int:
        """Number of attachments on the currently-loaded message."""
        if self._rendered is None:
            return 0
        return len(self._rendered.attachments)

    @property
    def raw_bytes(self) -> bytes | None:
        """Raw RFC 5322 bytes of the currently-loaded message, or None."""
        if self._rendered is None:
            return None
        return self._rendered.raw_bytes

    def save_attachment(self, index: int, dest_dir: Path) -> str | None:
        """Save attachment *index* (1-based) to *dest_dir*.

        Returns the saved filename (may differ from the original if a file
        with that name already existed).  Returns *None* when the index is
        out of range.  Raises ``OSError`` on write failure.
        """
        if self._rendered is None:
            return None
        payload = extract_attachment(self._rendered.raw_bytes, index)
        if payload is None:
            return None
        dest = _unique_path(dest_dir, payload.filename)
        dest.write_bytes(payload.data)
        return dest.name

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

    def body_link(self, idx: int) -> tuple[str, str] | None:
        """Return the (kind, target) pair at *idx*, or None if out of range."""
        try:
            return self._body_links[idx]
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
            shown = _escape(f"{display} <{addr}>" if display else addr)
            parts.append(
                f"[@click=\"screen.compose_address('{idx}')\"]{shown}[/]"
                f"[@click=\"screen.harvest_contact('{idx}')\"] (+)[/]"
            )
        return label + ", ".join(parts)

    def _build_markup(self, r: RenderedMessage) -> str:
        self._header_addresses: list[tuple[str, str]] = []
        self._body_links = r.links
        lines: list[str] = []

        for label, header in (
            ("From:    ", r.from_),
            ("To:      ", r.to),
            ("Cc:      ", r.cc),
        ):
            line = self._addr_field(label, header)
            if line is not None:
                lines.append(line)
        lines.append(f"Date:    {_escape(r.date)}")
        lines.append(f"Subject: {_escape(r.subject)}")

        if r.attachments:
            lines.append("")
            lines.append("Attachments:")
            for att in r.attachments:
                name = _escape(att.filename)
                link = f"[@click=\"screen.open_attachment('{att.index}')\"]"
                lines.append(
                    f"  [{att.index}] {link}{name}[/]"
                    f"  {_escape(att.content_type)}"
                    f"  ({fmt_size(att.size_bytes)})"
                )

        lines.append("─" * 60)
        lines.append("")
        lines.append(_render_body(r.styled_body or r.body, r.links))

        return "\n".join(lines)
