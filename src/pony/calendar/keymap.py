"""Which key does what, with no user interface attached.

This module holds two things: the vocabulary both halves of the program
describe a keyboard with, and the calendar's own table written in it.
The mail reader's table is :mod:`pony.mail.keymap`, which imports the
vocabulary from here — the same arrangement as :mod:`pony.outcome`,
and for the same reason: the dependency arrow runs from ``pony`` to
the calendar, so the vocabulary both halves share lives here.

A front end converts the table it needs:
:func:`pony.tui.calendar.bindings.to_textual` for the terminal, and
:mod:`pony.web` serves it as JSON.  Both help screens render from it, so
neither can advertise a key the other does not have, and a key cannot
mean one thing in the terminal and another in a browser.

The calendar's keys deliberately echo the mail reader's: ``c`` creates
(compose / new event), ``g`` syncs, ``G`` goes somewhere, ``Q`` quits,
``B`` browses contacts, ``/`` searches.  One table each, two tables that
rhyme.
"""

from __future__ import annotations

from enum import StrEnum

from pony.keys import KeyBinding


class CalendarSurface(StrEnum):
    """Where a calendar key is live.

    The terminal maps these onto screens; the browser maps them onto the
    page it is showing.  ``VIEW`` is every date-axis view — the agenda
    list and both grids — because a key that moves the viewed date means
    the same thing in all of them.
    """

    # Live wherever the user is, including over a dialog.
    APPLICATION = "application"
    # Any view of a date range: the agenda list, the timeline, the month.
    VIEW = "view"
    # The event form.
    EDIT = "edit"
    # One event, opened to be read.
    DETAIL = "detail"
    # The box a search query is typed into.
    SEARCH = "search"


# ----------------------------------------------------------------------
# The calendar's keys
# ----------------------------------------------------------------------
#
# Bare letters, no Ctrl prefix, deliberately echoing the mail client's.
# The agenda view is reached with `a`; the single-day / multi-day
# timeline with the number keys `1`-`7`.

KEY_VIEW_AGENDA = "a"
# Calendar-style month grid.
KEY_VIEW_MONTH = "M"
KEY_VIEW_MONTH_ALT = "shift+m"

# Timeline span selectors. `1` is the single-day view; `2`-`7` are the
# multi-day grid showing that many days.
KEY_SPANS = (1, 2, 3, 4, 5, 6, 7)

# Agenda-only window tuners. In other views these keys are no-ops.
KEY_AGENDA_DAY = "d"
KEY_AGENDA_WEEK = "w"
KEY_AGENDA_MONTH = "m"

KEY_TODAY = "t"
# Opens the "go to date" dialog. `G` is what the mail reader uses to jump
# to a folder, so one capital means "go somewhere" in both halves; the
# vim-style `:` that used to be the only way still works.
KEY_GOTO = "G"
KEY_GOTO_ALTS = ("shift+g", "colon")

# Date-axis navigation. `n`/`p` step the viewed date by the view's
# natural unit (a day in Day / Grid; the window size in Agenda);
# `N`/`P` shift it by a week in every view.
KEY_NEXT_DAY = "n"
KEY_PREV_DAY = "p"
# Same shift-letter caveat as the rest: terminals split on whether
# they emit the uppercase character or the modifier form.
KEY_NEXT_WEEK = "N"
KEY_NEXT_WEEK_ALT = "shift+n"
KEY_PREV_WEEK = "P"
KEY_PREV_WEEK_ALT = "shift+p"

# `c` (create) and `g` (get/sync) mirror Pony's compose / get-mail
# keys; `Q` mirrors Pony's quit. The shift-letter aliases cope with
# terminals that emit the modifier form instead of the bare capital.
KEY_NEW = "c"
KEY_EDIT = "e"
KEY_DELETE = "D"
KEY_DELETE_ALT = "shift+d"
# Spelled the way the mail reader spells it, so one key means one thing
# in both halves of the program: ++ctrl+g++ syncs in the background
# without asking and restarts the periodic countdown, while `g` opens the
# confirmation dialog and shows progress — mail's "Get mail". The capital
# is not a third way to sync: it goes to a date, as it goes to a folder
# in the mail reader.
KEY_SYNC = "ctrl+g"
KEY_SYNC_DIALOG = "g"
KEY_SEARCH = "/"
# The host application's contact store, browsed with the same key the
# mail reader uses. Bound whether or not a host supplied one: pressing it
# without one says so, rather than leaving the user wondering whether
# they mistyped.
KEY_CONTACTS = "B"
KEY_CONTACTS_ALT = "shift+b"
KEY_QUIT = "Q"
KEY_QUIT_ALT = "shift+q"
KEY_OPEN = "enter"
KEY_BACK = "escape"
KEY_HELP = "f1"
KEY_TOGGLE_CALENDARS = "C"
KEY_TOGGLE_CALENDARS_ALT = "shift+c"


def span_label(days: int) -> str:
    return "1 day" if days == 1 else f"{days} days"


def _span_binding(days: int) -> KeyBinding:
    """A `1`-`7` key that selects the *days*-wide timeline.

    Only the endpoints (`1 day` / `7 days`) are shown in the footer —
    they advertise the available range without flooding it with seven
    near-identical entries. The in-between selectors stay hidden but
    work, and the help screen lists all of them.
    """
    return KeyBinding(
        str(days),
        f"select_span({days})",
        span_label(days),
        CalendarSurface.VIEW,
        show=days in (KEY_SPANS[0], KEY_SPANS[-1]),
    )


VIEW_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding(KEY_VIEW_AGENDA, "view_agenda", "Agenda", CalendarSurface.VIEW),
    KeyBinding(KEY_VIEW_MONTH, "view_month", "Month", CalendarSurface.VIEW),
    KeyBinding(
        KEY_VIEW_MONTH_ALT,
        "view_month",
        "Month",
        CalendarSurface.VIEW,
        show=False,
    ),
    *(_span_binding(days) for days in KEY_SPANS),
    KeyBinding(
        KEY_AGENDA_DAY,
        "agenda_window_day",
        "Day",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(
        KEY_AGENDA_WEEK,
        "agenda_window_week",
        "Week",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(
        KEY_AGENDA_MONTH,
        "agenda_window_month",
        "Month",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(KEY_TODAY, "today", "Today", CalendarSurface.VIEW),
    KeyBinding(KEY_GOTO, "goto_date", "Go to", CalendarSurface.VIEW),
    *(
        KeyBinding(key, "goto_date", "Go to", CalendarSurface.VIEW, show=False)
        for key in KEY_GOTO_ALTS
    ),
    KeyBinding(KEY_NEXT_DAY, "next_day", "Next day", CalendarSurface.VIEW, show=False),
    KeyBinding(KEY_PREV_DAY, "prev_day", "Prev day", CalendarSurface.VIEW, show=False),
    KeyBinding(
        KEY_NEXT_WEEK,
        "next_week",
        "Next week",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(
        KEY_NEXT_WEEK_ALT,
        "next_week",
        "Next week",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(
        KEY_PREV_WEEK,
        "prev_week",
        "Prev week",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(
        KEY_PREV_WEEK_ALT,
        "prev_week",
        "Prev week",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(KEY_NEW, "new_event", "New", CalendarSurface.VIEW),
    KeyBinding(KEY_EDIT, "edit_event", "Edit", CalendarSurface.VIEW),
    KeyBinding(KEY_OPEN, "open_event", "Open", CalendarSurface.VIEW, show=False),
    KeyBinding(KEY_DELETE, "delete_event", "Delete", CalendarSurface.VIEW),
    KeyBinding(
        KEY_DELETE_ALT,
        "delete_event",
        "Delete",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(
        KEY_TOGGLE_CALENDARS,
        "toggle_calendars",
        "Calendars",
        CalendarSurface.VIEW,
    ),
    KeyBinding(
        KEY_TOGGLE_CALENDARS_ALT,
        "toggle_calendars",
        "Calendars",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(KEY_SYNC_DIALOG, "sync_dialog", "Sync", CalendarSurface.VIEW),
    KeyBinding(KEY_SYNC, "sync", "Bg sync", CalendarSurface.VIEW, show=False),
    KeyBinding(KEY_CONTACTS, "browse_contacts", "Contacts", CalendarSurface.VIEW),
    KeyBinding(
        KEY_CONTACTS_ALT,
        "browse_contacts",
        "Contacts",
        CalendarSurface.VIEW,
        show=False,
    ),
    KeyBinding(KEY_SEARCH, "search", "Search", CalendarSurface.VIEW),
    KeyBinding(KEY_HELP, "show_help", "Help", CalendarSurface.VIEW),
    KeyBinding(KEY_QUIT, "quit", "Quit", CalendarSurface.VIEW),
    KeyBinding(KEY_QUIT_ALT, "quit", "Quit", CalendarSurface.VIEW, show=False),
)

EDIT_KEYS: tuple[KeyBinding, ...] = (
    # A Ctrl chord, like Save, because a bare letter would be swallowed
    # as text by the focused field.  `ctrl+d` only resolves to an action
    # when editing an existing event, so the footer hides it in "new
    # event" mode.
    KeyBinding("ctrl+s", "save", "Save", CalendarSurface.EDIT),
    KeyBinding("ctrl+d", "delete", "Delete", CalendarSurface.EDIT),
    KeyBinding(KEY_BACK, "cancel", "Cancel", CalendarSurface.EDIT),
)

# The two small screens whose keys were written out in
# `pony.tui.calendar.bindings` from these same constants, so they existed in
# the terminal and in no table: the browser has neither screen, but a
# key that runs an operation belongs here either way.
DETAIL_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding(KEY_BACK, "close", "Back", CalendarSurface.DETAIL),
    KeyBinding(KEY_EDIT, "edit", "Edit", CalendarSurface.DETAIL),
)

SEARCH_KEYS: tuple[KeyBinding, ...] = (
    KeyBinding(KEY_OPEN, "submit", "Search", CalendarSurface.SEARCH),
    KeyBinding(KEY_BACK, "cancel", "Cancel", CalendarSurface.SEARCH),
)

CALENDAR_KEYMAP: tuple[KeyBinding, ...] = (
    *VIEW_KEYS,
    *EDIT_KEYS,
    *DETAIL_KEYS,
    *SEARCH_KEYS,
)


__all__ = [
    "CALENDAR_KEYMAP",
    "DETAIL_KEYS",
    "EDIT_KEYS",
    "SEARCH_KEYS",
    "KEY_AGENDA_DAY",
    "KEY_AGENDA_MONTH",
    "KEY_AGENDA_WEEK",
    "KEY_BACK",
    "KEY_CONTACTS",
    "KEY_CONTACTS_ALT",
    "KEY_DELETE",
    "KEY_DELETE_ALT",
    "KEY_EDIT",
    "KEY_GOTO",
    "KEY_GOTO_ALTS",
    "KEY_HELP",
    "KEY_NEW",
    "KEY_NEXT_DAY",
    "KEY_NEXT_WEEK",
    "KEY_NEXT_WEEK_ALT",
    "KEY_OPEN",
    "KEY_PREV_DAY",
    "KEY_PREV_WEEK",
    "KEY_PREV_WEEK_ALT",
    "KEY_QUIT",
    "KEY_QUIT_ALT",
    "KEY_SEARCH",
    "KEY_SPANS",
    "KEY_SYNC",
    "KEY_SYNC_DIALOG",
    "KEY_TODAY",
    "KEY_TOGGLE_CALENDARS",
    "KEY_TOGGLE_CALENDARS_ALT",
    "KEY_VIEW_AGENDA",
    "KEY_VIEW_MONTH",
    "KEY_VIEW_MONTH_ALT",
    "VIEW_KEYS",
    "CalendarSurface",
    "span_label",
]
