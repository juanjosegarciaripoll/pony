"""Base class for modal yes/no dialog screens."""

from __future__ import annotations

import contextlib

from textual.screen import Screen
from textual.widgets import Button


class DialogScreen(Screen[bool]):
    """Shared styling for modal dialogs that dismiss with True or False.

    Subclasses compose a ``Vertical(id="dialog")`` containing a ``Label(id=
    "title")``, their own body widgets, and a ``Horizontal(id="buttons")``
    holding the action Buttons. This class supplies the centered layout,
    the dialog border, title style, button-row alignment, and compact
    single-row Button sizing.

    Set ``DEFAULT_BUTTON_ID`` on the subclass to focus that button on mount
    so that Enter activates it (Textual's built-in behaviour for focused
    buttons).  Call ``mark_busy`` from ``on_button_pressed`` before any work
    that takes perceptible time.
    """

    DEFAULT_BUTTON_ID: str | None = None

    DEFAULT_CSS = """
    DialogScreen {
        align: center middle;
    }

    #dialog {
        height: auto;
        border: solid $primary;
        padding: 1 2;
    }

    #title {
        text-style: bold;
        margin-bottom: 1;
    }

    #buttons {
        layout: horizontal;
        height: auto;
        align: center middle;
    }

    #buttons Button {
        margin: 0 1;
        height: 1;
        min-width: 0;
        border: none;
        padding: 0 1;
    }

    #buttons Button:focus {
        text-style: reverse bold;
    }
    """

    def on_mount(self) -> None:
        if self.DEFAULT_BUTTON_ID is not None:
            with contextlib.suppress(Exception):
                self.query_one(f"#{self.DEFAULT_BUTTON_ID}", Button).focus()

    def mark_busy(self, button_id: str, busy_label: str = "Working…") -> None:
        """Disable all buttons and relabel the activated one.

        Call at the start of ``on_button_pressed`` before dispatching work
        that takes perceptible time.  Prevents double-activation.
        """
        for btn in self.query(Button):
            btn.disabled = True
        with contextlib.suppress(Exception):
            self.query_one(f"#{button_id}", Button).label = busy_label
