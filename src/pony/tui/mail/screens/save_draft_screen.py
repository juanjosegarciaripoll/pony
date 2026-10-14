"""Simple yes/no modal: prompt the user to save a draft before discarding."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Label

from .dialog_screen import DialogScreen


class SaveDraftScreen(DialogScreen):
    """Ask the user whether to save the current draft.

    Dismisses with ``True`` (save) or ``False`` (discard).
    """

    INHERIT_BINDINGS = False
    DEFAULT_BUTTON_ID = "save-btn"

    BINDINGS = [
        Binding("y", "save", show=False),
        Binding("n", "discard", show=False),
        Binding("q", "discard", show=False),
        Binding("escape", "discard", show=False),
    ]

    CSS = """
    #dialog {
        width: 50;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Save to Drafts before closing?", id="title")
            with Horizontal(id="buttons"):
                yield Button("Save [Y]", id="save-btn", variant="success")
                yield Button("Discard [N]", id="discard-btn", variant="error")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "save-btn")

    def action_save(self) -> None:
        self.dismiss(True)

    def action_discard(self) -> None:
        self.dismiss(False)
