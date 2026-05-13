"""Dialog for acting on an inline message link (Open / Copy / Cancel)."""

from __future__ import annotations

import webbrowser

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Static

from .dialog_screen import DialogScreen


class LinkActionScreen(DialogScreen):
    """Modal shown when the user clicks a ``[link↗]`` token in a message body."""

    DEFAULT_BUTTON_ID = "open"

    BINDINGS = [
        Binding("o", "open_link", "Open", show=False),
        Binding("c", "copy_link", "Copy", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    CSS = """
    #dialog {
        width: 70;
    }

    #body {
        margin-bottom: 1;
        overflow: hidden auto;
        max-height: 5;
    }
    """

    def __init__(self, url: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._url = url

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Link", id="title")
            yield Static(self._url, id="body")
            with Horizontal(id="buttons"):
                yield Button("Open [O]", id="open", variant="primary")
                yield Button("Copy [C]", id="copy")
                yield Button("Cancel [Esc]", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open":
            self.action_open_link()
        elif event.button.id == "copy":
            self.action_copy_link()
        else:
            self.dismiss(False)

    def action_open_link(self) -> None:
        try:
            webbrowser.open(self._url)
        except OSError as exc:
            self.notify(str(exc), severity="error")
            return
        self.dismiss(False)

    def action_copy_link(self) -> None:
        self.app.copy_to_clipboard(self._url)  # pyright: ignore[reportUnknownMemberType]
        self.notify("Link copied")
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)
