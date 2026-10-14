"""The chrome every page shares: title bar, tabs, footer, overlays.

The browser front end is a full-screen application, not a document:
the window is divided into panes, each pane scrolls on its own, and the
page itself never does.  That is a layout decision, so it is made in one
place — here and in ``static/pony.css`` — rather than once per page.

What varies between pages is the panes and which keys are live, and both
arrive as arguments.  The footer is built from the same keymap the
browser dispatches on, so it cannot advertise a key the page does not
answer to.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING

from pony.calendar.keymap import CALENDAR_KEYMAP, CalendarSurface, web_key
from pony.calendar.keymap import for_surface as for_calendar_surface

from ..keymap import MAIL_KEYMAP, Surface, for_surface
from .html import Raw, document, element, join

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from pony.calendar.keymap import KeyBinding
    from pony.calendar.outcome import Notice


class Tab(StrEnum):
    """Where the tab bar leads: the mail, the calendar, the contacts."""

    MAIL = "mail"
    CALENDAR = "calendar"
    CONTACTS = "contacts"


# Which keys are live on which page.  The browser fetches the same
# table, keyed by the page's `data-surface`, so this and the dispatcher
# cannot disagree about what a page answers to.
class PageSurface(StrEnum):
    READER = "reader"
    COMPOSE = "compose"
    CONTACTS = "contacts"
    CALENDAR = "calendar"
    EVENT = "event"


def bindings_for(surface: PageSurface) -> tuple[KeyBinding, ...]:
    """Every binding live on *surface*, application keys included."""
    application = for_surface(MAIL_KEYMAP, Surface.APPLICATION)
    match surface:
        case PageSurface.READER:
            # The message's own surface comes last on purpose.  Its
            # `n`, `p`, `<` and `>` are the same keys the list claims,
            # resolved in the terminal by which pane has focus and here
            # by `one_per_web_key` keeping the first -- the list's
            # meaning, because a browser has no focused pane.  What is
            # left of the surface is what a page can do with no focus to
            # consult: close the open message, and page through it.
            return (
                *application,
                *for_surface(MAIL_KEYMAP, Surface.READER, Surface.LIST),
                *for_surface(MAIL_KEYMAP, Surface.MESSAGE),
            )
        case PageSurface.COMPOSE:
            return for_surface(MAIL_KEYMAP, Surface.COMPOSE)
        case PageSurface.CONTACTS:
            return (
                *application,
                *for_surface(MAIL_KEYMAP, Surface.CONTACTS, Surface.LIST),
            )
        case PageSurface.CALENDAR:
            return (
                *application,
                *for_calendar_surface(CALENDAR_KEYMAP, CalendarSurface.VIEW),
            )
        case PageSurface.EVENT:
            return for_calendar_surface(CALENDAR_KEYMAP, CalendarSurface.EDIT)


def one_per_web_key(bindings: Iterable[KeyBinding]) -> list[KeyBinding]:
    """*bindings* with every repeated key dropped, the first one kept.

    A surface can inherit the application keys and declare them again
    (the calendar's view surface does), and a bare capital and its
    ``shift+`` spelling are one key to a browser.  Either way the second
    can never be matched -- ``Array.find`` takes the first -- so listing
    it would promise a key that does nothing.  The footer and
    ``/keymap.json`` both need this, which is why it is not written
    twice.
    """
    seen: set[str] = set()
    kept: list[KeyBinding] = []
    for binding in bindings:
        key = web_key(binding.key)
        if key in seen:
            continue
        seen.add(key)
        kept.append(binding)
    return kept


def page(
    *,
    title: str,
    surface: PageSurface,
    tab: Tab,
    panes: Raw,
    status: str = "",
    data: Mapping[str, str] | None = None,
    notices: Sequence[Notice] = (),
    folders: Sequence[str] = (),
) -> str:
    """One complete page: chrome around *panes*.

    *status* is the line beside the tabs — the open folder, the viewed
    month — which is what the terminal puts in its sub-title.  *folders*
    populates the datalist that the move, copy and go-to prompts
    complete against, so those dialogs need no request of their own.
    """
    body_data = {"surface": surface.value, **(data or {})}
    return document(
        title=title,
        stylesheets=("/static/pony.css",),
        scripts=("/static/pony.js",),
        data=body_data,
        body=join(
            (
                element(
                    "header",
                    element("span", "Pony Express", class_="brand"),
                    _tabs(tab),
                    element("span", status, id="status"),
                    # The `g` key, for a screen that has no keys. The
                    # dispatcher sends it to whichever half the page is
                    # showing.
                    element(
                        "button",
                        "Sync",
                        type="button",
                        class_="action",
                        data_action="sync",
                        id="sync",
                    ),
                    # ++shift+q++, for the same screen that has no keys.
                    # It asks first: a tap is easier to do by accident,
                    # and nothing on a phone can start the server again.
                    element(
                        "button",
                        "Quit",
                        type="button",
                        class_="action",
                        data_action="quit",
                        data_confirm="Stop Pony Express?",
                        data_agree="Stop",
                        id="quit",
                    ),
                    id="chrome",
                ),
                element("div", panes, id="panes"),
                _footer(bindings_for(surface)),
                element(
                    "div",
                    notice_list(notices),
                    id="notices",
                    role="status",
                    aria_live="polite",
                ),
                element("dialog", "", id="help"),
                element("dialog", "", id="prompt"),
                element(
                    "datalist",
                    join(element("option", value=name) for name in folders),
                    id="folder-names",
                ),
                _keymap_data(surface),
            ),
        ),
    )


def keymap_rows(surface: PageSurface) -> list[dict[str, object]]:
    """*surface*'s bindings, one row per key the browser can see.

    ``available`` is what keeps the help honest: a key the terminal has
    and the browser has not is listed and marked, rather than appearing
    to work.  One function behind both the copy in the page and the one
    ``/keymap.json`` serves, so the two cannot drift.
    """
    # Deferred, because `app` is what assembles the action tables and it
    # imports this module to build its pages.
    from .app import is_available

    return [
        {
            "key": web_key(binding.key),
            "action": binding.action,
            "description": binding.description,
            "surface": binding.surface,
            "show": binding.show,
            "available": is_available(binding.action),
        }
        for binding in one_per_web_key(bindings_for(surface))
    ]


def _keymap_data(surface: PageSurface) -> Raw:
    """The surface's keymap, in the page, for the script to read.

    It used to arrive in a request of its own, fired last in ``start()``
    while ``keydown`` was already listening, so every key pressed before
    it landed matched nothing and was dropped without a word.  The
    footer was already rendered from this same table server-side; the
    script reads it from here now, and there is no window to lose a
    press in.

    ``<`` is escaped so no value could close this element early.  None
    of them can today -- they are the keymap's own prose -- and the
    escape costs nothing.
    """
    payload = json.dumps(keymap_rows(surface)).replace("<", "\\u003c")
    return element(
        "script",
        Raw(payload),
        type="application/json",
        id="keymap",
    )


def notice_list(notices: Iterable[Notice]) -> Raw:
    """Notices as the page shows them, in the order they were made."""
    return join(
        element("p", notice.text, class_=f"notice {notice.severity}")
        for notice in notices
    )


def pane(
    *,
    name: str,
    heading: Raw | str,
    body: Raw,
    classes: str = "",
) -> Raw:
    """One pane: a heading strip that stays put, and a body that scrolls.

    Every pane in the program has the same two parts, which is what
    gives the message list, the contacts list and the agenda the same
    column headers in the same place.
    """
    return element(
        "section",
        element("div", heading, class_="pane-head"),
        element("div", body, class_="pane-body"),
        id=name,
        class_=f"pane {classes}".strip(),
    )


def columns(*cells: tuple[str, str]) -> Raw:
    """A heading strip made of named columns: ``(class, label)`` pairs."""
    return element(
        "div",
        join(element("span", label, class_=name) for name, label in cells),
        class_="columns",
    )


def _tabs(current: Tab) -> Raw:
    from . import urls

    entries = (
        (Tab.MAIL, "Mail", urls.MAIL),
        (Tab.CALENDAR, "Calendar", urls.CALENDAR),
        (Tab.CONTACTS, "Contacts", urls.CONTACTS),
    )
    return element(
        "nav",
        join(
            element(
                "a",
                label,
                href=href,
                class_="tab current" if tab is current else "tab",
            )
            for tab, label, href in entries
        ),
        id="tabs",
    )


def _footer(bindings: tuple[KeyBinding, ...]) -> Raw:
    """The keys worth advertising, in the table's own order.

    The footer listed "Q Quit" twice until the de-duplication went in;
    it is :func:`one_per_web_key` now, over the keys worth showing.
    """
    shown = one_per_web_key(binding for binding in bindings if binding.show)
    return element(
        "footer",
        join(
            element(
                "span",
                element("kbd", web_key(binding.key)),
                element("span", binding.description, class_="what"),
                class_="key",
            )
            for binding in shown
        ),
        id="keys",
    )


__all__ = [
    "PageSurface",
    "Tab",
    "bindings_for",
    "columns",
    "keymap_rows",
    "notice_list",
    "one_per_web_key",
    "page",
    "pane",
]
