"""Base class for modal yes/no dialog screens."""

from __future__ import annotations

from textual.screen import Screen


class DialogScreen(Screen[bool]):
    """Shared styling for modal dialogs that dismiss with True or False.

    Subclasses compose a ``Vertical(id="dialog")`` containing a ``Label(id=
    "title")``, their own body widgets, and a ``Horizontal(id="buttons")``
    holding the action Buttons. This class supplies the centered layout,
    the dialog border, title style, button-row alignment, and compact
    single-row Button sizing.
    """

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
    """
