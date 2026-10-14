"""Input dialog for creating a new folder in the local mirror."""

from __future__ import annotations

from .floating_input_screen import SimpleInputScreen


class NewFolderScreen(SimpleInputScreen):
    """Floating input bar for typing a new folder name.

    Dismissed with the trimmed folder name on submit, or ``None`` on
    escape / empty input.
    """

    INPUT_LABEL = "New folder:"
    INPUT_PLACEHOLDER = "Archive, Projects/2026, ..."
