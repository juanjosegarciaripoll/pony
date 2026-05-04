"""Reusable yes/no confirmation dialog."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Static

from .dialog_screen import DialogScreen


class ConfirmScreen(DialogScreen):
    """Modal confirmation dialog.  Dismisses with True or False."""

    DEFAULT_BUTTON_ID = "yes"

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
        Binding("escape", "cancel", "No", show=False),
    ]

    CSS = """
    #dialog {
        width: 60;
    }

    #body {
        margin-bottom: 1;
    }
    """

    def __init__(
        self,
        title: str,
        body: str,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Static(self._body, id="body")
            with Horizontal(id="buttons"):
                yield Button("Yes [Y]", id="yes", variant="error")
                yield Button("No [N]", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
