"""Input dialog for creating a new folder in the local mirror."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Input, Label


class NewFolderScreen(Screen[str | None]):
    """Floating input bar for typing a new folder name.

    Dismissed with the trimmed folder name on submit, or ``None`` on
    escape / empty input.
    """

    INHERIT_BINDINGS = False

    CSS = """
    NewFolderScreen {
        background: transparent;
        align: center bottom;
    }

    #new-folder-bar {
        width: 80%;
        height: 3;
        background: $boost;
        border: solid $primary;
        align: left middle;
        padding: 0 1;
    }

    #new-folder-label {
        width: auto;
        color: $accent;
        margin-right: 1;
    }

    #new-folder-input {
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
        with Horizontal(id="new-folder-bar"):
            yield Label("New folder:", id="new-folder-label")
            yield Input(
                placeholder="Archive, Projects/2026, ...",
                id="new-folder-input",
            )

    def on_mount(self) -> None:
        self.query_one("#new-folder-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip()
        self.dismiss(name if name else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
