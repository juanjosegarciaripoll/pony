"""Which key does what in the mail reader, with no interface attached.

A keyboard-first client has exactly one keymap, and it cannot live in a
widget library: the terminal needs Textual ``Binding`` objects, a browser
needs a table it can match a ``keydown`` against, and the help screen in
each needs the same list of descriptions.  Three copies of ``D`` means
three chances for them to disagree.

So the keymap is data here, grouped by *surface* — the part of the
program a key is live on — and each front end converts it:
``pony.tui.bindings.to_textual`` for the terminal, and ``pony.web``
serves it as JSON.  Both help screens render from it, so neither can
advertise a key the other does not have.

The vocabulary — :class:`chronos.keymap.KeyBinding`, :func:`for_surface`
and :func:`web_key` — is shared with the calendar, which keeps its own
table in :mod:`chronos.keymap`.  The two deliberately echo each other,
so ``c`` creates and ``g`` syncs on both sides.

The action names are the ones the front ends already dispatch on
(Textual's ``action_<name>``, the web's ``POST /action/<name>``), which
is what lets one table drive both.
"""

from __future__ import annotations

from enum import StrEnum

from chronos.keymap import KeyBinding, for_surface, web_key


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
    # The folder tree.  Its own surface because two of its keys mean
    # something different there than in the message list: `n`/`p` walk
    # the tree rather than the rows under it.
    FOLDERS = "folders"
    # The compose form.
    COMPOSE = "compose"
    # The contacts browser.
    CONTACTS = "contacts"


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

# The folder tree, which is not a list of messages: `n`/`p` step through
# folders here, and `N`/`P` jump between the accounts' INBOXes.
#
# These were written straight into the widget, where `N` shadowed the
# reader's "New folder" -- so from the tree, the pane where one would
# naturally create a folder, it was unreachable and absent from the
# footer too.  `N`/`P` keep the tree, because years of muscle memory
# point at them; it is "New folder" that moved, to `F`.
FOLDER_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding("n", "cursor_down", "Next folder", Surface.FOLDERS, show=False),
    KeyBinding("p", "cursor_up", "Prev folder", Surface.FOLDERS, show=False),
    KeyBinding("N", "next_inbox", "Next inbox", Surface.FOLDERS, show=False),
    KeyBinding("P", "prev_inbox", "Prev inbox", Surface.FOLDERS, show=False),
)


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
    # `F` for folder.  It was `N`, which the folder tree also claimed for
    # "next INBOX" -- and the tree is a pane of the reader, so one of the
    # two had to move.  A bare capital rather than a chord, like every
    # other reader verb: ++ctrl+n++ is reserved by Chrome and Firefox for
    # "new window", which a page can neither see nor refuse, so serving
    # it would have put a key in the browser's own help that could never
    # fire.
    KeyBinding("F", "new_folder", "New folder", Surface.READER),
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

# The contacts browser: one list, with the same marking vocabulary as
# the message list and the bulk operations that act on the marks.
CONTACT_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding("/", "search", "Search", Surface.CONTACTS),
    KeyBinding("s", "search", "Search", Surface.CONTACTS, show=False),
    KeyBinding("c", "new_contact", "New", Surface.CONTACTS),
    KeyBinding("e", "edit", "Edit", Surface.CONTACTS),
    KeyBinding("D", "delete_marked", "Delete marked", Surface.CONTACTS),
    KeyBinding("M", "merge_marked", "Merge marked", Surface.CONTACTS),
    KeyBinding("escape", "close", "Close", Surface.CONTACTS, priority=True),
    KeyBinding("q", "close", "Close", Surface.CONTACTS, show=False),
)

# The compose form.  Chords rather than bare letters, because a letter
# belongs to whatever field has focus — which is also why every one of
# these wins over the focused widget.
COMPOSE_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding("ctrl+s", "send", "Send", Surface.COMPOSE, priority=True),
    KeyBinding(
        "alt+s",
        "send",
        "Send",
        Surface.COMPOSE,
        show=False,
        priority=True,
    ),
    KeyBinding("escape", "cancel", "Cancel", Surface.COMPOSE, priority=True),
    KeyBinding(
        "alt+a",
        "add_attachment",
        "Attach",
        Surface.COMPOSE,
        priority=True,
    ),
    KeyBinding("alt+e", "edit_external", "Editor", Surface.COMPOSE, priority=True),
    KeyBinding("alt+m", "toggle_markdown", "Markdown", Surface.COMPOSE, priority=True),
)

MAIL_KEYMAP: tuple[KeyBinding, ...] = (
    *APPLICATION_KEYS,
    *READER_KEYS,
    *LIST_KEYS,
    *FOLDER_KEYS,
    *PANE_KEYS,
    *CONTACT_KEYS,
    *COMPOSE_KEYS,
)


__all__ = [
    "APPLICATION_KEYS",
    "COMPOSE_KEYS",
    "CONTACT_KEYS",
    "FOLDER_KEYS",
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
