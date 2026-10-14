"""The vocabulary a keyboard is described with, shared by both halves.

A :class:`KeyBinding` is one key, one action, one description, and the
surfaces it is live on.  Each half of the program writes its own table in
this vocabulary -- :mod:`pony.mail.keymap` for the mail reader,
:mod:`pony.calendar.keymap` for the calendar -- and each front end
converts the table it needs: :func:`pony.tui.bindings.to_textual` for the
terminal, ``/keymap.json`` for the browser.  Both help screens render
from the tables, so neither front end can advertise a key the other does
not have, and a key cannot mean one thing in the terminal and another in
a browser.

The surfaces themselves are not shared: a mail surface is a pane of the
reader (:class:`pony.mail.keymap.Surface`), a calendar surface a view of the
agenda (:class:`pony.calendar.keymap.CalendarSurface`).  Only the shape
of a binding and the two functions that read a table are.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KeyBinding:
    """One key, and the action it runs.

    *key* is spelled the way Textual spells it (``"ctrl+g"``, ``"f1"``,
    ``"!"``), because that is the more constrained of the two
    vocabularies; :func:`web_key` translates to what a browser reports.

    *surface* is which part of the program the key is live on, named by
    the half of the program that owns the table
    (:class:`pony.mail.keymap.Surface`, :class:`CalendarSurface`).  It is
    typed as a plain string so one dataclass can serve both.

    *show* is whether this belongs in a footer or a toolbar — the keys
    worth advertising to someone who has not learned them yet.  Every
    binding appears in the help, shown or not.
    """

    key: str
    action: str
    description: str
    surface: str
    show: bool = True
    # True for keys that must win over whatever has focus, such as quit.
    priority: bool = False


# Only the keys whose DOM name differs from Textual's. A bare character
# is reported as itself, so "!" and "/" need no entry.
_WEB_KEY_NAMES = {
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "enter": "Enter",
    "escape": "Escape",
    "space": " ",
    "tab": "Tab",
    "backspace": "Backspace",
    "delete": "Delete",
    "pagedown": "PageDown",
    "pageup": "PageUp",
    "colon": ":",
    "f1": "F1",
    "f2": "F2",
}


def web_key(key: str) -> str:
    """*key* as a browser reports it in a ``keydown`` event.

    Textual spells a bare character as itself and a named key in lower
    case (``"f1"``, ``"shift+up"``); the DOM reports ``event.key`` with
    its own capitalisation (``"F1"``, ``"ArrowUp"``) and the modifiers
    separately.  This produces the normalised form
    ``[ctrl+][shift+]<event.key>`` that the browser dispatcher builds for
    each press, so the two can be compared without either side guessing.

    ``shift`` disappears in front of a single character, because that is
    what the browser does: pressing shift+m reports ``"M"`` with the
    shift flag set, and a dispatcher that added ``shift+`` to every
    capital letter could never match a plain ``"M"``.  Spelling it
    ``"shift+m"`` on this side and ``"M"`` on the other is how every
    shift-letter alias came to be served, listed in the help, and dead.
    """
    *modifiers, base = key.split("+")
    name = _WEB_KEY_NAMES.get(base, base)
    if len(name) == 1 and "shift" in modifiers:
        return "+".join([*(m for m in modifiers if m != "shift"), name.upper()])
    return "+".join([*modifiers, name])


def key_for(keymap: tuple[KeyBinding, ...], action: str) -> str:
    """Which key runs *action*, for prose that names it.

    A sentence in the interface that tells the user which key to press
    has to say what the table says, or it is a hand-typed copy that
    goes stale the day the key moves -- which is what happened to the
    compose screen's cheatsheet.  Empty when nothing is bound to the
    action, so a surface that cannot offer it says nothing rather than
    naming a key that does nothing.
    """
    for binding in keymap:
        if binding.action == action:
            return binding.key
    return ""


def for_surface(
    keymap: tuple[KeyBinding, ...],
    *surfaces: str,
) -> tuple[KeyBinding, ...]:
    """Every binding live on any of *surfaces*, in declaration order."""
    wanted = frozenset(surfaces)
    return tuple(binding for binding in keymap if binding.surface in wanted)


__all__ = [
    "KeyBinding",
    "for_surface",
    "key_for",
    "web_key",
]
