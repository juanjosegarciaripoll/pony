"""One-line search dialog — slides up from the bottom of the screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Label

from .floating_input_screen import FloatingInputScreen


class SearchDialogScreen(FloatingInputScreen[str | None]):
    """Floating input bar for entering a search query.

    Dismissed with the typed string (non-empty) or ``None`` on escape.
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="floating-bar"):
            yield Label("/", id="floating-label")
            yield Input(
                placeholder="from:alice subject:hello bare words…",
                id="floating-input",
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        raw = event.value.strip()
        self.dismiss(raw if raw else None)
