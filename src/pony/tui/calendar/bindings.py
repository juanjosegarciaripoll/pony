"""The terminal's converter from the keymap to Textual bindings.

The keys themselves are not written here: they are data in
:mod:`pony.calendar.keymap`, which the browser front end reads too.  This
module turns a slice of that table into what `Screen.BINDINGS` wants,
and re-exports the key constants that screens refer to by name.
"""

from __future__ import annotations

from pony.calendar.keymap import (
    CALENDAR_KEYMAP,
    DETAIL_KEYS,
    EDIT_KEYS,
    KEY_AGENDA_DAY,
    KEY_AGENDA_MONTH,
    KEY_AGENDA_WEEK,
    KEY_BACK,
    KEY_CONTACTS,
    KEY_CONTACTS_ALT,
    KEY_DELETE,
    KEY_DELETE_ALT,
    KEY_EDIT,
    KEY_GOTO,
    KEY_GOTO_ALTS,
    KEY_HELP,
    KEY_NEW,
    KEY_NEXT_DAY,
    KEY_NEXT_WEEK,
    KEY_NEXT_WEEK_ALT,
    KEY_OPEN,
    KEY_PREV_DAY,
    KEY_PREV_WEEK,
    KEY_PREV_WEEK_ALT,
    KEY_QUIT,
    KEY_QUIT_ALT,
    KEY_SEARCH,
    KEY_SPANS,
    KEY_SYNC,
    KEY_SYNC_DIALOG,
    KEY_TODAY,
    KEY_TOGGLE_CALENDARS,
    KEY_TOGGLE_CALENDARS_ALT,
    KEY_VIEW_AGENDA,
    KEY_VIEW_MONTH,
    KEY_VIEW_MONTH_ALT,
    SEARCH_KEYS,
    VIEW_KEYS,
    span_label,
)
from pony.tui.bindings import BindingType, to_textual


def main_bindings() -> list[BindingType]:
    """Bindings owned by `MainScreen` — every date-axis view's keys."""
    return to_textual(VIEW_KEYS)


def edit_bindings() -> list[BindingType]:
    return to_textual(EDIT_KEYS)


def detail_bindings() -> list[BindingType]:
    return to_textual(DETAIL_KEYS)


def search_bindings() -> list[BindingType]:
    return to_textual(SEARCH_KEYS)


__all__ = [
    "CALENDAR_KEYMAP",
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
    "detail_bindings",
    "edit_bindings",
    "main_bindings",
    "search_bindings",
    "span_label",
]
