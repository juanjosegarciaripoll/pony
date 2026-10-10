"""The one keymap both front ends read.

The point of `pony.keymap` is that a key cannot mean one thing in the
terminal and another in the browser, so most of what matters here is
agreement: that the Textual bindings are built from the table, that the
web spelling of each key round-trips, and that no two bindings on one
surface claim the same key.
"""

from __future__ import annotations

import collections

from textual.binding import Binding

from pony.keymap import (
    APPLICATION_KEYS,
    LIST_KEYS,
    MAIL_KEYMAP,
    MARK_KEYS,
    MOTION_KEYS,
    READER_KEYS,
    KeyBinding,
    Surface,
    for_surface,
    web_key,
)
from pony.tui.bindings import MARK_BINDINGS, MOTION_BINDINGS, to_textual


def test_the_keymap_is_free_of_textual() -> None:
    """A browser cannot do anything with a `Binding`, so there are none."""
    assert all(isinstance(binding, KeyBinding) for binding in MAIL_KEYMAP)


def test_every_key_is_claimed_once_per_surface() -> None:
    """Two bindings for one key on one surface means one never fires."""
    for surface in Surface:
        keys = [b.key for b in MAIL_KEYMAP if b.surface is surface]
        duplicates = [key for key, n in collections.Counter(keys).items() if n > 1]
        assert duplicates == [], f"{surface} claims {duplicates} twice"


def test_the_textual_bindings_are_built_from_the_table() -> None:
    converted = to_textual(APPLICATION_KEYS)

    assert all(isinstance(binding, Binding) for binding in converted)
    quit_binding = converted[0]
    assert isinstance(quit_binding, Binding)
    assert (quit_binding.key, quit_binding.action) == ("Q", "quit")
    # Quit has to win over whatever has focus, and that survives the
    # conversion.
    assert quit_binding.priority is True


def test_the_shared_list_bindings_come_from_the_keymap() -> None:
    assert tuple(to_textual(MARK_KEYS)) == MARK_BINDINGS
    assert tuple(to_textual(MOTION_KEYS)) == MOTION_BINDINGS
    assert (*MARK_KEYS, *MOTION_KEYS) == LIST_KEYS


def test_the_reader_screen_binds_exactly_the_reader_and_list_keys() -> None:
    """What the screen declares is the table, not a second copy of it."""
    from pony.tui.screens.main_screen import MainScreen

    expected = to_textual(for_surface(MAIL_KEYMAP, Surface.READER, Surface.LIST))
    assert list(MainScreen.BINDINGS) == expected


def test_the_application_binds_exactly_the_application_keys() -> None:
    from pony.tui.app import PonyApp

    assert list(PonyApp.BINDINGS) == to_textual(APPLICATION_KEYS)


def test_for_surface_keeps_declaration_order() -> None:
    reader_only = for_surface(MAIL_KEYMAP, Surface.READER)

    assert reader_only[0].action == "sync"
    assert all(b.surface is Surface.READER for b in reader_only)
    # Every reader key, plus the pane-resize ones, which are on the same
    # surface but kept in their own group.
    assert set(READER_KEYS).issubset(set(reader_only))


def test_web_keys_use_the_browser_spelling() -> None:
    # A bare character is reported as itself.
    assert web_key("A") == "A"
    assert web_key("!") == "!"
    assert web_key("/") == "/"
    # Named keys are not.
    assert web_key("f1") == "F1"
    assert web_key("f2") == "F2"
    assert web_key("shift+up") == "shift+ArrowUp"
    assert web_key("shift+down") == "shift+ArrowDown"
    # Modifiers survive in order, with the base key translated.
    assert web_key("ctrl+g") == "ctrl+g"
    assert web_key("ctrl+left") == "ctrl+ArrowLeft"
    assert web_key("space") == " "


def test_every_binding_has_a_web_spelling() -> None:
    """A key the browser cannot name is a key it can never match."""
    for binding in MAIL_KEYMAP:
        spelled = web_key(binding.key)
        assert spelled
        assert not spelled.endswith("+")


def test_the_contacts_browser_binds_the_contacts_surface() -> None:
    """The browser's keys are in the table, not in the screen."""
    from pony.tui.bindings import MARK_BINDINGS
    from pony.tui.screens.contact_browser_screen import ContactBrowserScreen

    expected = [
        *to_textual(for_surface(MAIL_KEYMAP, Surface.CONTACTS)),
        *MARK_BINDINGS,
    ]
    assert list(ContactBrowserScreen.BINDINGS) == expected


def test_the_composer_binds_the_compose_surface() -> None:
    from pony.tui.screens.compose_screen import ComposeScreen

    assert list(ComposeScreen.BINDINGS) == to_textual(
        for_surface(MAIL_KEYMAP, Surface.COMPOSE),
    )


def test_every_compose_key_wins_over_the_focused_field() -> None:
    """A bare Input would otherwise swallow them as text."""
    from pony.keymap import COMPOSE_KEYS

    assert all(binding.priority for binding in COMPOSE_KEYS)


def test_the_calendar_keeps_its_own_table_in_the_same_vocabulary() -> None:
    from chronos.keymap import CALENDAR_KEYMAP, CalendarSurface
    from chronos.keymap import for_surface as for_calendar_surface
    from chronos.tui.bindings import main_bindings
    from chronos.tui.bindings import to_textual as calendar_to_textual

    assert all(isinstance(binding, KeyBinding) for binding in CALENDAR_KEYMAP)
    assert main_bindings() == calendar_to_textual(
        for_calendar_surface(CALENDAR_KEYMAP, CalendarSurface.VIEW),
    )


def test_both_halves_spell_the_keys_they_share_the_same_way() -> None:
    """`c` creates, `g` syncs and `Q` quits on both sides."""
    from chronos.keymap import KEY_NEW, KEY_QUIT, KEY_SYNC, KEY_SYNC_DIALOG

    mail = {binding.action: binding.key for binding in MAIL_KEYMAP}
    assert mail["compose_new"] == KEY_NEW
    assert mail["sync"] == KEY_SYNC_DIALOG
    assert mail["background_sync"] == KEY_SYNC
    assert mail["quit"] == KEY_QUIT


def test_every_calendar_binding_has_a_web_spelling_too() -> None:
    from chronos.keymap import CALENDAR_KEYMAP

    for binding in CALENDAR_KEYMAP:
        spelled = web_key(binding.key)
        assert spelled
        assert not spelled.endswith("+")
    # The vim-style "go to date" key is one the DOM names differently.
    assert web_key("colon") == ":"
