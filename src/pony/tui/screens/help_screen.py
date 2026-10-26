"""Centered modal help screen listing the TUI's keyboard shortcuts.

Replaces Textual's built-in command-palette side panel.  The content
is a curated, category-grouped reference rather than an auto-discovered
list from ``Screen.BINDINGS`` — screens and widgets contribute bindings
from many places, and a hand-maintained grouping reads better than
whatever order those contributions happen in.

Curated does not mean remembered: every row carries the binding keys it
documents, and ``tests/test_help_panel.py`` checks those against the
keys the app, the reader screen and its three panels actually declare.
A new key with no row here fails that test, and so does a row naming a
key nothing binds any more.  A row may document behaviour that comes
from a Textual default rather than one of our bindings (``Enter`` to
open a message, say) by listing no keys at all.
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
class _Row:
    """One line of the cheatsheet.

    ``display`` is for the reader — ranges and alternatives spelled the
    way a person reads them — while ``keys`` is the same keys exactly as
    a ``Binding`` spells them, which is what the test compares against.
    """

    display: str
    action: str
    keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Section:
    title: str
    bindings: tuple[_Row, ...]


# The attachment accelerators are one row each but ten keys each.
_DIGITS: tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")
_CTRL_DIGITS: tuple[str, ...] = tuple(f"ctrl+{d}" for d in _DIGITS)

# Two-column layout: left list first, right list second.  Keep sections
# short so the modal fits on a standard 80×24 terminal.
_LEFT_SECTIONS: tuple[_Section, ...] = (
    _Section(
        "Navigation",
        (
            _Row("g / Ctrl+G", "Get mail — now / in the background", ("g", "ctrl+g")),
            _Row("n / p", "Next / previous row", ("n", "p")),
            _Row("< / >", "First / last row", ("<", ">")),
            _Row("N / P", "Next / previous INBOX", ("N", "P")),
            _Row("/", "Search current folder", ("/",)),
            _Row("Q", "Quit", ("Q",)),
        ),
    ),
    _Section(
        "Compose",
        (
            _Row("c", "New message", ("c",)),
            _Row("e", "Edit the selected draft", ("e",)),
            _Row("r", "Reply", ("r",)),
            _Row("R", "Reply all", ("R",)),
            _Row("f", "Forward", ("f",)),
            _Row("w", "Open in web browser", ("w",)),
        ),
    ),
    _Section(
        "Folders & layout",
        (
            _Row("Shift+G", "Goto folder…", ("G",)),
            _Row("Shift+N", "New folder", ("N",)),
            _Row(
                "Ctrl+← → ↑ ↓",
                "Resize panes (or drag a border)",
                ("ctrl+left", "ctrl+right", "ctrl+up", "ctrl+down"),
            ),
            _Row("Shift+F8", "Dedupe this folder", ("shift+f8",)),
        ),
    ),
    _Section(
        "Contacts",
        (
            _Row("B", "Browse contacts", ("B",)),
            _Row("H", "Harvest from folder", ("H",)),
        ),
    ),
)

_RIGHT_SECTIONS: tuple[_Section, ...] = (
    _Section(
        "Messages",
        (
            _Row("Enter", "Open message"),
            _Row("Esc / q", "Close message", ("escape", "q")),
            _Row("Space / PgDn", "Page through the message", ("space", "pagedown")),
            _Row("u", "Mark unread", ("u",)),
            _Row("C", "Mark all read", ("C",)),
            _Row("!", "Flag / unflag", ("!",)),
            _Row("D", "Trash", ("D",)),
            _Row("A", "Archive", ("A",)),
            _Row("Y", "Copy to folder…", ("Y",)),
            _Row("M", "Move to folder…", ("M",)),
            _Row(
                "m / Shift+↑↓",
                "Toggle mark / extend",
                ("m", "shift+up", "shift+down"),
            ),
            _Row("s / Ctrl+P", "Save message… / print to PDF", ("s", "ctrl+p")),
        ),
    ),
    _Section(
        "Attachments",
        (
            _Row("1-9 / 0", "Open attachment N / open all", _DIGITS),
            _Row("Ctrl+1-9 / Ctrl+0", "Save attachment N / save all", _CTRL_DIGITS),
            _Row("O / S", "Open / save every attachment", ("O", "S")),
        ),
    ),
    _Section(
        "Calendar",
        (
            _Row("F2", "Agenda (F2 again returns)", ("f2",)),
            _Row("i", "Answer invitation on this message", ("i",)),
        ),
    ),
    _Section(
        "This panel",
        (
            _Row("F1", "Toggle this help", ("f1",)),
            _Row("Esc / q", "Dismiss", ("escape", "q")),
        ),
    ),
)


def _render_column(sections: tuple[_Section, ...]) -> str:
    """Render one column as a rich-markup string: section headers in
    accent colour, each binding as ``[b]key[/b]  description``."""
    lines: list[str] = []
    for idx, section in enumerate(sections):
        if idx > 0:
            lines.append("")
        lines.append(f"[b $accent]{markup_escape(section.title)}[/b $accent]")
        width = max(len(row.display) for row in section.bindings) + 2
        for row in section.bindings:
            lines.append(
                f"[b]{markup_escape(row.display).ljust(width)}[/b] "
                f"{markup_escape(row.action)}"
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
