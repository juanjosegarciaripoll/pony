"""Confirmation bar for trashing duplicate copies in a folder."""

from __future__ import annotations

from .floating_input_screen import SimpleInputScreen


class DedupeConfirmScreen(SimpleInputScreen):
    """Ask for the message count before any duplicate is trashed.

    The count has to be read off the prompt and typed back, the same
    gate ``pony folder dedup --apply`` uses: this expunges mail from the
    server on the next sync, so it should not be answerable by reflex
    the way a ``y/N`` prompt is.  Dismissed with the typed text, or
    ``None`` on escape.
    """

    def __init__(self, *, count: int, groups: int, folder: str) -> None:
        super().__init__()
        self.INPUT_LABEL = (
            f"Trash {count} duplicate(s) in {groups} group(s) from {folder}? "
            f"Type {count} to confirm:"
        )
        self.INPUT_PLACEHOLDER = "Esc to cancel"
