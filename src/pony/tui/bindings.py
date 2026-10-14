"""Turning a keymap into Textual bindings.

A keymap is data (:mod:`pony.keys` for the vocabulary, `pony.mail.keymap`
and `pony.calendar.keymap` for the two tables), because the browser front
end needs the same tables and a ``textual.binding.Binding`` means nothing
to it.  This is the terminal's half of that: one converter, used by both
halves, so a key cannot be live in one front end and absent from the
other.

The groups each half assigns into a ``BINDINGS`` list are beside its own
screens -- `pony.tui.mail.bindings`, `pony.tui.calendar.bindings`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.binding import Binding

if TYPE_CHECKING:
    from pony.keys import KeyBinding


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


__all__ = [
    "BindingType",
    "to_textual",
]
