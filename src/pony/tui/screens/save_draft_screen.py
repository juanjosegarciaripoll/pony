"""Simple yes/no modal: prompt the user to save a draft before discarding."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Label


class SaveDraftScreen(Screen[bool]):
    """Ask the user whether to save the current draft.

    Dismisses with ``True`` (save) or ``False`` (discard).
    """

    INHERIT_BINDINGS = False

    CSS = """
    SaveDraftScreen {
        align: center middle;
    }

    #dialog {
        width: 50;
        height: auto;
        border: solid $primary;
        padding: 1 2;
    }

    #prompt {
        margin-bottom: 1;
    }

    #buttons {
        layout: horizontal;
        height: auto;
        align: center middle;
    }

    Button {
        margin: 0 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Save to Drafts before closing?", id="prompt")
            with Horizontal(id="buttons"):
                yield Button("Save [Y]", id="save-btn", variant="success")
                yield Button("Discard [N]", id="discard-btn", variant="error")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "save-btn")

    def on_key(self, event: object) -> None:
        from textual.events import Key
        if isinstance(event, Key):
            if event.key in ("y", "enter"):
                self.dismiss(True)
            elif event.key in ("n", "escape", "q"):
                self.dismiss(False)
