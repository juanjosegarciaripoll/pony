"""Input dialog for picking one or more attachments by number."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Label

from ...message_export import PartKey, parse_part_selection
from .floating_input_screen import FloatingInputScreen


class AttachmentPickerScreen(FloatingInputScreen[list[PartKey] | None]):
    """Floating input bar for typing a part selection.

    Dismissed with the parsed list of part numbers on submit, or
    ``None`` on escape / empty / invalid input.  ``0`` is the message
    itself, so a message with no attachments still has something to
    offer and the prompt says so.
    """

    def __init__(
        self,
        *,
        action_label: str,
        attachment_count: int,
    ) -> None:
        super().__init__()
        self._action_label = action_label
        self._attachment_count = attachment_count

    def compose(self) -> ComposeResult:
        with Horizontal(id="floating-bar"):
            yield Label(
                f"{self._action_label} 0-{self._attachment_count}"
                " (0 = whole message, T = text):",
                id="floating-label",
            )
            yield Input(
                placeholder="0, T, 1,3 or *",
                id="floating-input",
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        indices = parse_part_selection(
            event.value,
            total=self._attachment_count,
        )
        self.dismiss(indices)
