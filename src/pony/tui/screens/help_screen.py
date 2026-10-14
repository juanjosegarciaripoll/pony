"""Centered modal help screen listing the reader's keyboard shortcuts.

Replaces Textual's built-in command-palette side panel.  The content is
rendered from :mod:`pony.mail.keymap` — the same table the footer and the
browser's ``/keymap.json`` are built from — because this used to be a
third hand-typed copy of it, with a docstring asking whoever changed a
binding to remember.  Nobody did: it contradicted itself on ``N``
(listing both "N / P Next / previous INBOX" and "Shift+N New folder")
and had fallen eight keys behind the table.

The grouping is still a decision, because the table's declaration order
is not a teaching order.  An action nobody put in a group lands in
"Other" rather than disappearing, which is what keeps a new binding
visible without anyone remembering this file.

A handful of keys belong to a widget rather than to the table — Enter
opens, the digits open attachments — and those are listed verbatim,
under a heading that says as much.

The open message is a surface of its own, so its keys come from the
table like the rest; which of its meanings applies — ``n`` as "next
message" or as "next row" — is decided by the pane that has focus, and
the help lists both.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Label, Static

from pony.keys import KeyBinding, for_surface

from ...mail.keymap import MAIL_KEYMAP, Surface

# Which section each action belongs under.  Keyed on the action name, so
# renaming a key never moves it and adding one only has to be mentioned
# here if "Other" is the wrong place for it.
_SECTIONS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "Navigation",
        frozenset(
            {
                "cursor_down",
                "cursor_up",
                "cursor_first",
                "cursor_last",
                "next_inbox",
                "prev_inbox",
                "next_message",
                "prev_message",
                "goto_folder",
                "search",
            }
        ),
    ),
    (
        "Compose",
        frozenset(
            {
                "compose_new",
                "compose_reply",
                "compose_reply_all",
                "compose_forward",
                "edit_draft",
                "send",
                "cancel",
                "add_attachment",
                "edit_external",
                "toggle_markdown",
            }
        ),
    ),
    (
        "Messages",
        frozenset(
            {
                "mark_unread",
                "mark_all_read",
                "toggle_flagged",
                "trash",
                "archive",
                "copy",
                "move",
                "save_message",
                "open_browser",
                "print_pdf",
                "attachments_open",
                "attachments_save",
                "mark_down",
                "mark_up",
                "close_message",
                "page_down_or_next",
                "scroll_home",
                "scroll_end",
            }
        ),
    ),
    (
        "Folders & layout",
        frozenset(
            {
                "new_folder",
                "narrow_folders",
                "widen_folders",
                "shrink_list",
                "grow_list",
            }
        ),
    ),
    (
        "Contacts",
        frozenset(
            {
                "browse_contacts",
                "harvest_contacts",
                "new_contact",
                "edit",
                "delete_marked",
                "merge_marked",
                "close",
            }
        ),
    ),
    (
        "Mail & calendar",
        frozenset({"sync", "background_sync", "respond_invitation"}),
    ),
    ("This program", frozenset({"show_help", "quit", "toggle_calendar"})),
)

_LEFT_ORDER = ("Navigation", "Compose", "Folders & layout", "Contacts")
_RIGHT_ORDER = ("Messages", "Mail & calendar", "This program", "Other")

# Keys a widget owns rather than the table: either the same key means
# different things in different panes, or there are twenty of them.
# Listed by hand *and labelled as such*, which is the honest version of
# what this whole file used to be.
_WIDGET_KEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "Open the row under the cursor"),
    ("Esc / q", "Leave a search, with no message open"),
    ("1-9 / 0", "Open attachment N / open all"),
    ("Ctrl+1-9 / Ctrl+0", "Save attachment N / save all"),
    ("Drag border", "Resize a pane with the mouse"),
)


@dataclass(frozen=True, slots=True)
class _Section:
    title: str
    rows: tuple[tuple[str, str], ...]


def _section_for(action: str) -> str:
    base = action.split("(", 1)[0]
    for title, actions in _SECTIONS:
        if base in actions:
            return title
    return "Other"


def _key_display(key: str) -> str:
    """*key* as a person reads it, not as Textual spells it."""
    parts = key.split("+")
    names = {
        "ctrl": "Ctrl",
        "alt": "Alt",
        "shift": "Shift",
        "up": "↑",
        "down": "↓",
        "left": "←",
        "right": "→",
        "escape": "Esc",
        "enter": "Enter",
        "space": "Space",
        "f1": "F1",
        "f2": "F2",
    }
    return "+".join(names.get(part, part) for part in parts)


def _bucket(bindings: tuple[KeyBinding, ...]) -> dict[str, list[tuple[str, str]]]:
    """Group *bindings* into sections, collapsing aliases for one action.

    Two keys for one action are a terminal-quirk double (``M`` and
    ``shift+m``) or a convenience (``/`` and ``s`` for search), and the
    help reads better naming both on one line than listing the action
    twice.
    """
    buckets: dict[str, list[tuple[str, str]]] = {}
    seen: dict[tuple[str, str], int] = {}
    for binding in bindings:
        section = _section_for(binding.action)
        rows = buckets.setdefault(section, [])
        where = (section, binding.action)
        if where in seen:
            index = seen[where]
            keys, description = rows[index]
            rows[index] = (f"{keys} / {_key_display(binding.key)}", description)
            continue
        seen[where] = len(rows)
        rows.append((_key_display(binding.key), binding.description))
    return buckets


def _columns(
    bindings: tuple[KeyBinding, ...],
) -> tuple[tuple[_Section, ...], tuple[_Section, ...]]:
    buckets = _bucket(bindings)
    left = [
        _Section(title, tuple(buckets[title]))
        for title in _LEFT_ORDER
        if buckets.get(title)
    ]
    right = [
        _Section(title, tuple(buckets[title]))
        for title in _RIGHT_ORDER
        if buckets.get(title)
    ]
    right.append(_Section("In a pane", _WIDGET_KEYS))
    return tuple(left), tuple(right)


def _render_column(sections: tuple[_Section, ...]) -> str:
    """One column as a rich-markup string: section headers in the accent
    colour, each row as ``[b]key[/b]  description``."""
    lines: list[str] = []
    for idx, section in enumerate(sections):
        if idx > 0:
            lines.append("")
        lines.append(f"[b $accent]{markup_escape(section.title)}[/b $accent]")
        width = max((len(key) for key, _ in section.rows), default=0) + 2
        for key, desc in section.rows:
            lines.append(
                f"[b]{markup_escape(key).ljust(width)}[/b] {markup_escape(desc)}"
            )
    return "\n".join(lines)


class HelpScreen(Screen[None]):
    """Centered modal listing the keyboard shortcuts of one surface."""

    # `f1` keeps the table's action name: it is the same operation seen
    # from the inside, and a screen that renamed it would be a key
    # meaning two things depending on where you pressed it.
    BINDINGS = [
        Binding("escape", "dismiss_help", "Close", show=False),
        Binding("q", "dismiss_help", "Close", show=False),
        Binding("f1", "show_help", "Close", show=False),
    ]

    CSS = """
    HelpScreen {
        align: center middle;
    }

    #help-dialog {
        width: auto;
        max-width: 90;
        height: auto;
        border: solid $primary;
        padding: 1 2;
        background: $surface;
    }

    #help-title {
        text-style: bold;
        width: 100%;
        content-align: center middle;
        margin-bottom: 1;
    }

    #help-columns {
        layout: horizontal;
        height: auto;
    }

    .help-column {
        width: auto;
        height: auto;
        padding: 0 2 0 0;
    }

    #help-hint {
        color: $text-muted;
        margin-top: 1;
        content-align: center middle;
        width: 100%;
    }
    """

    #: The reader: everything live in the three-pane view, plus the keys
    #: that are live wherever the user is.
    READER_SURFACES = (
        Surface.APPLICATION,
        Surface.READER,
        Surface.LIST,
        Surface.FOLDERS,
        Surface.MESSAGE,
    )

    def __init__(self, *surfaces: Surface, **kwargs: object) -> None:
        """*surfaces* are the parts of the program to describe.

        Called with none, it describes the reader — which is what ``F1``
        from the mail screen means.  The contacts browser passes its own,
        so a standalone ``pony contacts browse`` can offer help without
        advertising keys it does not have.
        """
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._surfaces = surfaces or self.READER_SURFACES

    def bindings_shown(self) -> tuple[KeyBinding, ...]:
        """Every table entry this screen is describing."""
        return for_surface(MAIL_KEYMAP, *self._surfaces)

    def compose(self) -> ComposeResult:
        left, right = _columns(self.bindings_shown())
        with Vertical(id="help-dialog"):
            yield Label("Keyboard shortcuts", id="help-title")
            with Horizontal(id="help-columns"):
                yield Static(_render_column(left), classes="help-column")
                yield Static(_render_column(right), classes="help-column")
            yield Static("Press F1, Esc or q to close.", id="help-hint")
        yield Footer()

    def action_dismiss_help(self) -> None:
        self.dismiss(None)

    def action_show_help(self) -> None:
        """Help is already showing, so ++f1++ closes it."""
        self.dismiss(None)
