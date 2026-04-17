"""Reusable yes/no confirmation dialog."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static


class ConfirmScreen(Screen[bool]):
    """Modal confirmation dialog.  Dismisses with True or False."""

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("enter", "confirm", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
        Binding("escape", "cancel", "No", show=False),
    ]

    CSS = """
    ConfirmScreen {
        align: center middle;
    }

    #confirm-dialog {
        width: 60;
        height: auto;
        border: solid $primary;
        padding: 1 2;
    }

    #confirm-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #confirm-body {
        margin-bottom: 1;
    }

    #confirm-buttons {
        layout: horizontal;
        height: auto;
        align: center middle;
    }

    #confirm-buttons Button {
        margin: 0 2;
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
        with Vertical(id="confirm-dialog"):
            yield Label(self._title, id="confirm-title")
            yield Static(self._body, id="confirm-body")
            with Vertical(id="confirm-buttons"):
                yield Button("Yes [Y]", id="yes", variant="error")
                yield Button("No [N]", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
