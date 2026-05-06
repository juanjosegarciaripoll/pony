"""Input dialog for creating a new folder in the local mirror."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Label

from .floating_input_screen import FloatingInputScreen


class NewFolderScreen(FloatingInputScreen[str | None]):
    """Floating input bar for typing a new folder name.

    Dismissed with the trimmed folder name on submit, or ``None`` on
    escape / empty input.
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="floating-bar"):
            yield Label("New folder:", id="floating-label")
            yield Input(
                placeholder="Archive, Projects/2026, ...",
                id="floating-input",
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip()
        self.dismiss(name if name else None)
