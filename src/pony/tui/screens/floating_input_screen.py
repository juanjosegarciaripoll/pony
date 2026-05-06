"""Base class for one-line floating input bars at the bottom of the screen."""

from __future__ import annotations

from typing import TypeVar

from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Input

_R = TypeVar("_R")


class FloatingInputScreen(Screen[_R]):
    """Transparent overlay with a compact input bar anchored at the bottom.

    Subclasses compose a ``Horizontal(id="floating-bar")`` containing a
    ``Label(id="floating-label")`` and an ``Input(id="floating-input")``.
    This class supplies the layout, bar styling, focus-on-mount, and
    Escape handling.
    """

    INHERIT_BINDINGS = False

    DEFAULT_CSS = """
    FloatingInputScreen {
        background: transparent;
        align: center bottom;
    }

    #floating-bar {
        width: 80%;
        height: 3;
        background: $boost;
        border: solid $primary;
        align: left middle;
        padding: 0 1;
    }

    #floating-label {
        width: auto;
        color: $accent;
        margin-right: 1;
    }

    #floating-input {
        width: 1fr;
        height: 1;
        border: none;
        background: $boost;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)
