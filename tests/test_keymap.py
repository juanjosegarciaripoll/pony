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


def test_the_folder_tree_binds_the_folders_surface() -> None:
    """A widget's keys are in the table too.

    Whole-list equality for five named *screens* is what let `N` be
    written into the tree, where it shadowed the reader's "New folder"
    and took it out of the footer as well.
    """
    from pony.tui.bindings import FOLDER_BINDINGS
    from pony.tui.widgets.folder_panel import FolderPanel

    expected = to_textual(for_surface(MAIL_KEYMAP, Surface.FOLDERS))
    assert list(FolderPanel.BINDINGS) == expected
    assert tuple(expected) == FOLDER_BINDINGS


def test_the_message_list_binds_the_list_surface() -> None:
    from pony.tui.widgets.message_list import MessageListPanel

    assert list(MessageListPanel.BINDINGS) == to_textual(
        for_surface(MAIL_KEYMAP, Surface.LIST),
    )


def test_no_widget_or_screen_shadows_a_reader_key_with_another_action() -> None:
    """One key, one action, wherever it is bound.

    Every ``BINDINGS`` list under ``pony.tui`` is checked, not just the
    five named screens -- a widget is exactly where the shadowing
    happened.  A key may be *absent* from a pane; what it may not do is
    mean a different thing there without the table saying so.
    """
    import importlib
    import pkgutil

    from textual.binding import Binding as TextualBinding
    from textual.widgets import Input, TextArea

    import pony.tui

    table = {
        binding.key: binding.action
        for binding in for_surface(MAIL_KEYMAP, Surface.READER, Surface.APPLICATION)
    }
    # The contacts browser and the composer replace the reader rather
    # than sitting inside it, so a letter may mean something else there
    # -- `D` deletes a contact where it trashes a message.  The tree and
    # the message list are panes *of* the reader and are held to it; see
    # `test_no_key_means_two_things_at_once_in_the_reader`.
    local = {
        binding.key
        for binding in for_surface(MAIL_KEYMAP, Surface.CONTACTS, Surface.COMPOSE)
    }

    offenders: list[str] = []
    for info in pkgutil.walk_packages(pony.tui.__path__, "pony.tui."):
        module = importlib.import_module(info.name)
        for name in dir(module):
            obj = getattr(module, name)
            bindings = getattr(obj, "BINDINGS", None)
            if not isinstance(bindings, list):
                continue
            if getattr(obj, "__module__", "") != info.name:
                continue
            # A text field owns its own editing chords -- ctrl+left is
            # "previous word" in anything you can type into, and a field
            # that could not do that would be broken.  Textual filters
            # those keys out of the screen's chain while such a widget
            # has focus, so there is no clash to resolve.
            if isinstance(obj, type) and issubclass(obj, (Input, TextArea)):
                continue
            for binding in bindings:
                if not isinstance(binding, TextualBinding):
                    continue
                if binding.key in local:
                    continue
                expected = table.get(binding.key)
                if expected is not None and expected != binding.action:
                    offenders.append(
                        f"{info.name}.{name} binds {binding.key!r} to "
                        f"{binding.action!r}, the table says {expected!r}",
                    )
    assert offenders == []


def test_the_help_screen_is_rendered_from_the_table() -> None:
    """It was a third hand-typed copy, and it contradicted itself on `N`."""
    from pony.tui.screens.help_screen import HelpScreen

    shown = HelpScreen().bindings_shown()

    assert shown == for_surface(
        MAIL_KEYMAP,
        Surface.APPLICATION,
        Surface.READER,
        Surface.LIST,
        Surface.FOLDERS,
    )
    # Every one of the keys the hand-typed version had lost.
    actions = {binding.action for binding in shown}
    assert {
        "background_sync",
        "edit_draft",
        "print_pdf",
        "save_message",
        "attachments_open",
        "attachments_save",
        "goto_folder",
        "cursor_first",
        "cursor_last",
    } <= actions
    # And it no longer claims `N` twice.
    keys = [binding.key for binding in shown]
    assert keys.count("N") == 1


def test_the_help_screen_can_describe_another_surface() -> None:
    from pony.tui.screens.help_screen import HelpScreen

    shown = HelpScreen(Surface.CONTACTS).bindings_shown()

    assert shown == for_surface(MAIL_KEYMAP, Surface.CONTACTS)


def test_the_compose_footer_is_rendered_from_the_table() -> None:
    """The one-line cheatsheet was a fourth copy, already out of date."""
    import re

    from pony.keymap import COMPOSE_KEYS
    from pony.tui.screens.compose_screen import _compose_cheatsheet

    plain = re.sub(r"\[[^]]*\]", "", _compose_cheatsheet())

    for binding in COMPOSE_KEYS:
        if binding.show:
            assert binding.description in plain, binding.key
    assert plain == "^S Send  Esc Cancel  Alt+A Attach  Alt+E Editor  Alt+M Markdown"


def test_the_standalone_apps_answer_the_application_keys() -> None:
    """`pony contacts browse` answered to neither `Q` nor `F1`.

    And it injected Textual's `ctrl+p` command palette, which nothing
    else in the program has.
    """
    from pony.tui.app import ContactsApp, EmlViewerApp, PonyApp

    for app in (PonyApp, ContactsApp, EmlViewerApp):
        assert app.ENABLE_COMMAND_PALETTE is False, app.__name__
        bound = {
            binding.key: binding.action
            for binding in app.BINDINGS
            if isinstance(binding, Binding)
        }
        assert bound.get("Q") == "quit", app.__name__
        assert bound.get("f1") == "show_help", app.__name__


def test_the_calendar_agenda_opens_what_the_table_says_enter_opens() -> None:
    """`enter` is `open_event`, and the agenda has to answer it."""
    from chronos.keymap import KEY_OPEN
    from chronos.tui.screens.main_screen import MainScreen as CalendarScreen

    assert KEY_OPEN == "enter"
    # `EventList` is a `DataTable`, whose own `enter` posts `RowSelected`;
    # the screen listened only for `RowHighlighted`, so the key did
    # nothing in the default view.
    assert hasattr(CalendarScreen, "on_data_table_row_selected")


def test_no_key_means_two_things_at_once_in_the_reader() -> None:
    """The reader and its two panes are live together, so they agree.

    `N` was `new_folder` in the reader and `next_inbox` in the tree, and
    the tree is a pane of the reader -- so with the tree focused, which
    is the natural place to create a folder, `N` created nothing and
    "New folder" was gone from the footer as well.

    The contacts browser and the composer are *not* in this set: they
    replace the reader, so `D` there may delete a contact.
    """
    import collections

    together = for_surface(
        MAIL_KEYMAP,
        Surface.APPLICATION,
        Surface.READER,
        Surface.LIST,
        Surface.FOLDERS,
    )
    actions: dict[str, set[str]] = collections.defaultdict(set)
    for binding in together:
        actions[binding.key].add(binding.action)

    clashing = {key: sorted(names) for key, names in actions.items() if len(names) > 1}
    assert clashing == {}
