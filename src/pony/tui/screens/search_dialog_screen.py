"""One-line search dialog — slides up from the bottom of the screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Input, Label


class SearchDialogScreen(Screen[str | None]):
    """Floating input bar for entering a search query.

    Dismissed with the typed string (non-empty) or ``None`` on escape.
    """

    INHERIT_BINDINGS = False

    CSS = """
    SearchDialogScreen {
        background: transparent;
        align: center bottom;
    }

    #search-bar {
        width: 80%;
        height: 3;
        background: $boost;
        border: solid $primary;
        align: left middle;
        padding: 0 1;
    }

    #search-label {
        width: auto;
        color: $accent;
        margin-right: 1;
    }

    #search-input {
        width: 1fr;
        height: 1;
        border: none;
        background: $boost;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="search-bar"):
            yield Label("/", id="search-label")
            yield Input(
                placeholder="from:alice subject:hello bare words…",
                id="search-input",
            )

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        raw = event.value.strip()
        self.dismiss(raw if raw else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
