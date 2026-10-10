"""Which key does what, with no user interface attached.

A keyboard-first client has exactly one keymap, and it cannot live in a
widget library: the terminal needs Textual ``Binding`` objects, a browser
needs a table it can match a ``keydown`` against, and the help screen in
each needs the same list of descriptions.  Three copies of ``D`` means
three chances for them to disagree.

So the keymap is data here, grouped by *surface* — the part of the
program a key is live on — and each front end converts it:
``pony.tui.bindings.to_textual`` for the terminal,
``pony.web`` serves it as JSON.  Both help screens render from it, so
neither can advertise a key the other does not have.

The action names are the ones the front ends already dispatch on
(Textual's ``action_<name>``, the web's ``POST /action/<name>``), which
is what lets one table drive both.  The calendar keeps its own
(:mod:`chronos.keymap`) because it is a subsystem with its own surfaces;
the two deliberately echo each other, so ``c`` creates and ``g`` syncs on
both sides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Surface(StrEnum):
    """Where a key is live.

    A front end binds the surfaces it has.  The terminal maps them onto
    an app and its screens; the browser maps them onto the page it is
    showing.
    """

    # Live wherever the user is, including over a dialog.
    APPLICATION = "application"
    # The three-pane mail reader.
    READER = "reader"
    # Any list of rows that supports marking and cursor motion.
    LIST = "list"
    # The compose form.
    COMPOSE = "compose"


@dataclass(frozen=True, slots=True)
class KeyBinding:
    """One key, and the action it runs.

    *key* is spelled the way Textual spells it (``"ctrl+g"``, ``"f1"``,
    ``"!"``), because that is the more constrained of the two
    vocabularies; :func:`web_key` translates to what a browser reports.

    *show* is whether this belongs in a footer or a toolbar — the keys
    worth advertising to someone who has not learned them yet.  Every
    binding appears in the help, shown or not.
    """

    key: str
    action: str
    description: str
    surface: Surface
    show: bool = True
    # True for keys that must win over whatever has focus, such as quit.
    priority: bool = False


APPLICATION_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding("Q", "quit", "Quit", Surface.APPLICATION, priority=True),
    KeyBinding("f1", "show_help", "Help", Surface.APPLICATION),
    # One description serves both footers and both help screens, so it
    # names the pair rather than the destination: "Calendar" reads wrong
    # from inside the calendar.
    KeyBinding("f2", "toggle_calendar", "Mail / Calendar", Surface.APPLICATION),
)

# Marking rows, then acting on the marks in bulk: the contacts
# browser and the message list both offer it.
MARK_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding("m", "mark_down", "Mark", Surface.LIST),
    KeyBinding("shift+down", "mark_down", "Mark ↓", Surface.LIST, show=False),
    KeyBinding("shift+up", "mark_up", "Mark ↑", Surface.LIST, show=False),
)

# Row-cursor motion, shared by every list.
MOTION_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding("n", "cursor_down", "Next", Surface.LIST, show=False),
    KeyBinding("p", "cursor_up", "Prev", Surface.LIST, show=False),
    KeyBinding("<", "cursor_first", "First", Surface.LIST, show=False),
    KeyBinding(">", "cursor_last", "Last", Surface.LIST, show=False),
)

LIST_KEYS: tuple[KeyBinding, ...] = (*MARK_KEYS, *MOTION_KEYS)


READER_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding("g", "sync", "Get mail", Surface.READER),
    KeyBinding("ctrl+g", "background_sync", "Bg sync", Surface.READER, show=False),
    KeyBinding("c", "compose_new", "Compose", Surface.READER),
    KeyBinding("e", "edit_draft", "Edit draft", Surface.READER),
    KeyBinding("r", "compose_reply", "Reply", Surface.READER),
    KeyBinding("R", "compose_reply_all", "Reply all", Surface.READER),
    KeyBinding("f", "compose_forward", "Forward", Surface.READER),
    KeyBinding("w", "open_browser", "Web view", Surface.READER),
    KeyBinding("ctrl+p", "print_pdf", "Print PDF", Surface.READER),
    KeyBinding("u", "mark_unread", "Mark unread", Surface.READER),
    KeyBinding("!", "toggle_flagged", "Flag", Surface.READER),
    KeyBinding("D", "trash", "Trash", Surface.READER),
    KeyBinding("A", "archive", "Archive", Surface.READER),
    KeyBinding("C", "mark_all_read", "Mark all read", Surface.READER),
    KeyBinding("Y", "copy", "Copy", Surface.READER),
    KeyBinding("M", "move", "Move", Surface.READER),
    KeyBinding("N", "new_folder", "New folder", Surface.READER),
    KeyBinding("s", "save_message", "Save", Surface.READER, show=False),
    KeyBinding("O", "attachments_open", "Open att.", Surface.READER, show=False),
    KeyBinding("S", "attachments_save", "Save att.", Surface.READER, show=False),
    KeyBinding("/", "search", "Search", Surface.READER, show=False),
    KeyBinding("G", "goto_folder", "Goto folder", Surface.READER, show=False),
    KeyBinding(
        "H",
        "harvest_contacts",
        "Harvest contacts",
        Surface.READER,
        show=False,
    ),
    KeyBinding("B", "browse_contacts", "Contacts", Surface.READER),
    KeyBinding("i", "respond_invitation", "Invitation", Surface.READER, show=False),
)

# Pane geometry is a terminal concern — a browser reflows instead — so
# these are kept apart from the actions a front end has to implement.
PANE_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding(
        "ctrl+left",
        "narrow_folders",
        "Narrower folders",
        Surface.READER,
        show=False,
    ),
    KeyBinding(
        "ctrl+right",
        "widen_folders",
        "Wider folders",
        Surface.READER,
        show=False,
    ),
    KeyBinding("ctrl+up", "shrink_list", "Smaller list", Surface.READER, show=False),
    KeyBinding("ctrl+down", "grow_list", "Bigger list", Surface.READER, show=False),
)

MAIL_KEYMAP: tuple[KeyBinding, ...] = (
    *APPLICATION_KEYS,
    *READER_KEYS,
    *LIST_KEYS,
    *PANE_KEYS,
)


def for_surface(
    keymap: tuple[KeyBinding, ...],
    *surfaces: Surface,
) -> tuple[KeyBinding, ...]:
    """Every binding live on any of *surfaces*, in declaration order."""
    wanted = frozenset(surfaces)
    return tuple(binding for binding in keymap if binding.surface in wanted)


def web_key(key: str) -> str:
    """*key* as a browser reports it in a ``keydown`` event.

    Textual spells a bare character as itself and a named key in lower
    case (``"f1"``, ``"shift+up"``); the DOM reports ``event.key`` with
    its own capitalisation (``"F1"``, ``"ArrowUp"``) and the modifiers
    separately.  This produces the normalised form
    ``[ctrl+][shift+]<event.key>`` that the browser dispatcher builds for
    each press, so the two can be compared without either side guessing.
    """
    *modifiers, base = key.split("+")
    return "+".join([*modifiers, _WEB_KEY_NAMES.get(base, base)])


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
    "f1": "F1",
    "f2": "F2",
}


__all__ = [
    "APPLICATION_KEYS",
    "LIST_KEYS",
    "MAIL_KEYMAP",
    "MARK_KEYS",
    "MOTION_KEYS",
    "PANE_KEYS",
    "READER_KEYS",
    "KeyBinding",
    "Surface",
    "for_surface",
    "web_key",
]
