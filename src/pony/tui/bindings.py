"""Turning the shared keymap into Textual bindings.

The keymap itself is data in :mod:`pony.keymap`, because the browser
front end needs the same table and a ``textual.binding.Binding`` means
nothing to it.  This module is the terminal's half of it: a converter,
plus the two groups several list widgets assign straight into their
``BINDINGS``.  Both are derived from the keymap now rather than written
out a second time, so a key cannot be live in one front end and absent
from the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.binding import Binding

from pony.keys import KeyBinding

from ..keymap import (
    FOLDER_KEYS,
    INVITATION_KEYS,
    MARK_KEYS,
    MESSAGE_KEYS,
    MOTION_KEYS,
)

if TYPE_CHECKING:
    pass


# `Screen.BINDINGS` is an invariant list typed as
# `list[Binding | tuple[str, str] | tuple[str, str, str]]`.  We only ever
# build `Binding` instances, but the declared type has to match that
# wider element type for a subclass to be able to assign the result.
BindingType = Binding | tuple[str, str] | tuple[str, str, str]


def to_textual(bindings: tuple[KeyBinding, ...]) -> list[BindingType]:
    """Each of *bindings* as Textual wants it."""
    return list[BindingType](
        Binding(
            binding.key,
            binding.action,
            binding.description,
            show=binding.show,
            priority=binding.priority,
        )
        for binding in bindings
    )


# Kept as tuples because several widgets splat them into a `BINDINGS`
# list of their own.
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
    "BindingType",
    "to_textual",
]
