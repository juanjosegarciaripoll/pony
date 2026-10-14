"""Text field for one email address, aware of pasted address lists."""

from __future__ import annotations

from textual.events import Paste
from textual.message import Message
from textual.widgets import Input


class AddressInput(Input):
    """An address field that keeps every line of a pasted address list.

    ``Input`` pastes ``event.text.splitlines()[0]`` and drops the rest,
    which silently loses recipients when a list is copied one per line
    out of another mail client.  A newline separates addresses exactly
    as a comma does, so fold the lines together and announce the paste
    — the composer then splits the result into one row per address.
    """

    class Pasted(Message):
        """An address list was pasted into *input* and needs splitting."""

        def __init__(self, input: Input) -> None:  # noqa: A002
            super().__init__()
            self.input = input

    def _on_paste(self, event: Paste) -> None:
        # Textual dispatches ``_on_*`` to every class in the MRO, so
        # without this the base handler runs straight after and pastes
        # its first line a second time.
        event.prevent_default()
        text = ", ".join(
            line.strip() for line in event.text.splitlines() if line.strip()
        )
        if text:
            selection = self.selection
            if selection.is_empty:
                self.insert_text_at_cursor(text)
            else:
                self.replace(text, *selection)
            self.post_message(self.Pasted(self))
        event.stop()
