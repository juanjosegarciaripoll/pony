"""Input dialog for picking one or more attachments by number."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Label

from ...message_export import TEXT_BODY, WHOLE_MESSAGE, PartKey, part_key
from .floating_input_screen import FloatingInputScreen


def parse_attachment_selection(text: str, *, total: int) -> list[PartKey] | None:
    """Parse the picker input into a list of part names.

    Part ``0`` is the message itself, ``T`` is its body as text with no
    HTML in it, and ``1..total`` are its attachments — the numbering
    :mod:`pony.message_export` defines and both front ends show.

    Accepted forms:
        "*"         → every part (``[0, "T", 1..total]``)
        "0"         → ``[0]``, the message as it arrived
        "T"         → ``["T"]``, the body as text (``t`` too)
        "1,3, 5"    → ``[1, 3, 5]`` (whitespace tolerated)
        "1-3"       → ``[1, 2, 3]`` (inclusive range)
        "0,T,1-3"   → the lot, named one way or another

    Returns ``None`` on empty input, unknown tokens, malformed or
    reversed ranges, out-of-range indices, or duplicates — the caller
    surfaces the error.
    """
    text = text.strip()
    if not text:
        return None
    if text == "*":
        return [WHOLE_MESSAGE, TEXT_BODY, *range(1, total + 1)]
    parts: list[PartKey] = []
    for token in (t.strip() for t in text.split(",")):
        if not token:
            continue
        if "-" in token:
            lo_text, _, hi_text = token.partition("-")
            try:
                lo, hi = int(lo_text.strip()), int(hi_text.strip())
            except ValueError:
                return None
            if lo > hi:
                return None
            parts.extend(range(lo, hi + 1))
            continue
        key = part_key(token)
        if key is None:
            return None
        parts.append(key)
    if not parts:
        return None
    if any(isinstance(key, int) and not 0 <= key <= total for key in parts):
        return None
    if len(set(parts)) != len(parts):
        return None
    return parts


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
        indices = parse_attachment_selection(
            event.value,
            total=self._attachment_count,
        )
        self.dismiss(indices)
