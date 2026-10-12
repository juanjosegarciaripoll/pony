"""What the receiving server said about who sent the open message.

The header line carries one character — a padlock, shut only when the
domain in ``From`` is the domain that signed the message.  Everything
behind it is here, because a lock is read as "safe" and the only honest
place to say what it actually means is somewhere with room for a
sentence.  See :mod:`pony.authentication`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Static

from .dialog_screen import DialogScreen

if TYPE_CHECKING:
    from ...authentication import MessageAuthentication


class SenderCheckScreen(DialogScreen):
    """Modal shown when the padlock beside ``From`` is chosen."""

    DEFAULT_BUTTON_ID = "close"

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("q", "cancel", "Close", show=False),
    ]

    CSS = """
    #dialog {
        width: 72;
    }

    #summary {
        margin-bottom: 1;
    }

    #meaning {
        margin-bottom: 1;
    }

    #detail {
        color: $text-muted;
    }
    """

    def __init__(self, authentication: MessageAuthentication, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._auth = authentication

    def compose(self) -> ComposeResult:
        auth = self._auth
        with Vertical(id="dialog"):
            yield Label(f"{auth.icon}  {auth.headline()}", id="title")
            if auth.worth_showing:
                # With no report there is nothing to summarise: the
                # title already says "not checked".
                yield Static(auth.label(), id="summary")
            yield Static(auth.meaning(), id="meaning")
            if auth.worth_showing:
                yield Static(auth.detail(), id="detail")
            with Horizontal(id="buttons"):
                yield Button("Close [Esc]", id="close", variant="primary")

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(True)
