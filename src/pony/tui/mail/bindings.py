"""The mail reader's binding groups, as its widgets assign them.

Kept as tuples because several widgets splat them into a ``BINDINGS``
list of their own.  Each is a slice of `pony.mail.keymap` put through
`pony.tui.bindings.to_textual`, so none of these keys is written out a
second time.
"""

from __future__ import annotations

from ...mail.keymap import (
    FOLDER_KEYS,
    INVITATION_KEYS,
    MARK_KEYS,
    MESSAGE_KEYS,
    MOTION_KEYS,
)
from ..bindings import BindingType, to_textual

MARK_BINDINGS: tuple[BindingType, ...] = tuple(to_textual(MARK_KEYS))
MOTION_BINDINGS: tuple[BindingType, ...] = tuple(to_textual(MOTION_KEYS))
FOLDER_BINDINGS: tuple[BindingType, ...] = tuple(to_textual(FOLDER_KEYS))
MESSAGE_BINDINGS: tuple[BindingType, ...] = tuple(to_textual(MESSAGE_KEYS))
INVITATION_BINDINGS: tuple[BindingType, ...] = tuple(to_textual(INVITATION_KEYS))


__all__ = [
    "FOLDER_BINDINGS",
    "INVITATION_BINDINGS",
    "MARK_BINDINGS",
    "MESSAGE_BINDINGS",
    "MOTION_BINDINGS",
]
