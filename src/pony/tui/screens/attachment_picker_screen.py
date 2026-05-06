"""Input dialog for picking one or more attachments by number."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Label

from .floating_input_screen import FloatingInputScreen


def parse_attachment_selection(text: str, *, total: int) -> list[int] | None:
    """Parse the picker input into a list of 1-based attachment indices.

    Accepted forms:
        "*"         → every attachment (``[1..total]``)
        "1"         → ``[1]``
        "1,3, 5"    → ``[1, 3, 5]`` (whitespace tolerated)

    Returns ``None`` on empty input, non-numeric tokens, out-of-range
    indices, or duplicates — the caller surfaces the error.
    """
    text = text.strip()
    if not text:
        return None
    if text == "*":
        return list(range(1, total + 1))
    try:
        parts = [int(p.strip()) for p in text.split(",") if p.strip()]
    except ValueError:
        return None
    if not parts:
        return None
    if any(i < 1 or i > total for i in parts):
        return None
    if len(set(parts)) != len(parts):
        return None
    return parts


class AttachmentPickerScreen(FloatingInputScreen[list[int] | None]):
    """Floating input bar for typing an attachment selection.

    Dismissed with the parsed list of 1-based indices on submit, or
    ``None`` on escape / empty / invalid input.
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
                f"{self._action_label} 1-{self._attachment_count}:",
                id="floating-label",
            )
            yield Input(
                placeholder="1,3 or *",
                id="floating-input",
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        indices = parse_attachment_selection(
            event.value,
            total=self._attachment_count,
        )
        self.dismiss(indices)
