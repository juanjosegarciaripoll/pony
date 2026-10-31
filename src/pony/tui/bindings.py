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
    """Each of *bindings* as Textual wants it.

    An ``alt+<letter>`` key is emitted twice.  A terminal reports Alt
    held with a capital letter as ``alt+shift+<letter>``, which is what
    arrives when Caps Lock is on or when shift comes along for the ride
    -- and the footer spelling the shortcut "Alt+A" makes holding shift
    the natural reading.  Unbound, that key falls through to whatever
    has focus and types the letter into the message.

    Doing it here rather than in the tables keeps the keymap a
    description of which key does what: ``alt+shift+a`` is not a second
    key a user could choose, it is the same key as the terminal spells
    it.  The browser, which reads the same tables, never sees it.
    """
    textual: list[BindingType] = []
    for binding in bindings:
        textual.append(
            Binding(
                binding.key,
                binding.action,
                binding.description,
                show=binding.show,
                priority=binding.priority,
            )
        )
        shifted = _shifted_alt(binding.key)
        if shifted is not None:
            textual.append(
                Binding(
                    shifted,
                    binding.action,
                    binding.description,
                    show=False,
                    priority=binding.priority,
                )
            )
    return textual


def _shifted_alt(key: str) -> str | None:
    """``alt+a`` -> ``alt+shift+a``; anything else -> ``None``."""
    prefix = "alt+"
    if not key.startswith(prefix):
        return None
    letter = key[len(prefix) :]
    if len(letter) != 1 or not letter.isalpha():
        return None
    return f"alt+shift+{letter}"


__all__ = [
    "BindingType",
    "to_textual",
]
