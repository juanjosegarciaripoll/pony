"""One-line search dialog — slides up from the bottom of the screen."""

from __future__ import annotations

from .floating_input_screen import SimpleInputScreen


class SearchDialogScreen(SimpleInputScreen):
    """Floating input bar for entering a search query.

    Dismissed with the typed string (non-empty) or ``None`` on escape.
    """

    INPUT_LABEL = "/"
    INPUT_PLACEHOLDER = "from:alice subject:hello bare words…"
