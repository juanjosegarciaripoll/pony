"""Base class for one-line floating input bars at the bottom of the screen."""

from __future__ import annotations

from typing import TypeVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Input, Label

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


class SimpleInputScreen(FloatingInputScreen[str | None]):
    """Floating one-line prompt that dismisses with its trimmed input.

    Subclasses set :attr:`INPUT_LABEL` and :attr:`INPUT_PLACEHOLDER`.
    Submitting dismisses with the stripped text, or ``None`` when the field
    is empty; Escape dismisses with ``None`` (via :class:`FloatingInputScreen`).
    """

    INPUT_LABEL = ""
    INPUT_PLACEHOLDER = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="floating-bar"):
            yield Label(self.INPUT_LABEL, id="floating-label")
            yield Input(placeholder=self.INPUT_PLACEHOLDER, id="floating-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        self.dismiss(text or None)
