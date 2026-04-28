"""Centered modal help screen listing the TUI's keyboard shortcuts.

Replaces Textual's built-in command-palette side panel.  The content
is a curated, category-grouped reference rather than an auto-discovered
list from ``Screen.BINDINGS`` — screens and widgets contribute bindings
from many places, and a hand-maintained grouping reads better than
whatever order those contributions happen in.  Keep in sync with the
bindings declared on ``MainScreen``, ``FolderPanel``,
``MessageListPanel``, and ``MessageViewPanel``.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Label, Static


@dataclass(frozen=True, slots=True)
class _Section:
    title: str
    bindings: tuple[tuple[str, str], ...]


# Two-column layout: left list first, right list second.  Keep sections
# short so the modal fits on a standard 80×24 terminal.
_LEFT_SECTIONS: tuple[_Section, ...] = (
    _Section("Navigation", (
        ("g", "Get mail (sync)"),
        ("n / p", "Next / previous folder"),
        ("N / P", "Next / previous INBOX"),
        ("/", "Search current folder"),
        ("Q", "Quit"),
    )),
    _Section("Compose", (
        ("c", "New message"),
        ("r", "Reply"),
        ("R", "Reply all"),
        ("f", "Forward"),
        ("w", "Open in web browser"),
    )),
    _Section("Folders", (
        ("Shift+N", "New folder"),
    )),
    _Section("Contacts", (
        ("B", "Browse contacts"),
        ("H", "Harvest from folder"),
    )),
)

_RIGHT_SECTIONS: tuple[_Section, ...] = (
    _Section("Messages", (
        ("u", "Mark unread"),
        ("C", "Mark all read"),
        ("!", "Flag / unflag"),
        ("D", "Trash"),
        ("A", "Archive"),
        ("Y", "Copy to folder…"),
        ("M", "Move to folder…"),
        ("m / Shift+↑↓", "Toggle mark / extend"),
    )),
    _Section("Attachments", (
        ("1-9 / 0", "Open attachment N / open all"),
        ("Ctrl+1-9 / Ctrl+0", "Save attachment N / save all"),
    )),
    _Section("This panel", (
        ("F1", "Toggle this help"),
        ("Esc / q", "Dismiss"),
    )),
)


def _render_column(sections: tuple[_Section, ...]) -> str:
    """Render one column as a rich-markup string: section headers in
    accent colour, each binding as ``[b]key[/b]  description``."""
    lines: list[str] = []
    for idx, section in enumerate(sections):
        if idx > 0:
            lines.append("")
        lines.append(f"[b $accent]{markup_escape(section.title)}[/b $accent]")
        width = max(len(key) for key, _ in section.bindings) + 2
        for key, desc in section.bindings:
            lines.append(
                f"[b]{markup_escape(key).ljust(width)}[/b]"
                f" {markup_escape(desc)}"
            )
    return "\n".join(lines)


class HelpScreen(Screen[None]):
    """Centered modal listing the TUI's keyboard shortcuts."""

    BINDINGS = [
        Binding("escape", "dismiss_help", "Close", show=False),
        Binding("q", "dismiss_help", "Close", show=False),
        Binding("f1", "dismiss_help", "Close", show=False),
    ]

    CSS = """
    HelpScreen {
        align: center middle;
    }

    #help-dialog {
        width: auto;
        max-width: 90;
        height: auto;
        border: solid $primary;
        padding: 1 2;
        background: $surface;
    }

    #help-title {
        text-style: bold;
        width: 100%;
        content-align: center middle;
        margin-bottom: 1;
    }

    #help-columns {
        layout: horizontal;
        height: auto;
    }

    .help-column {
        width: auto;
        height: auto;
        padding: 0 2 0 0;
    }

    #help-hint {
        color: $text-muted;
        margin-top: 1;
        content-align: center middle;
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Label("Keyboard shortcuts", id="help-title")
            with Horizontal(id="help-columns"):
                yield Static(_render_column(_LEFT_SECTIONS), classes="help-column")
                yield Static(_render_column(_RIGHT_SECTIONS), classes="help-column")
            yield Static("Press F1, Esc or q to close.", id="help-hint")
        yield Footer()

    def action_dismiss_help(self) -> None:
        self.dismiss(None)
