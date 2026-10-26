"""The F1 cheatsheet has to match the keys the reader actually binds.

Keeping a hand-written key reference in step with the code is exactly
the kind of promise a comment cannot keep: ``Shift+F8`` reached the
panel only because someone remembered, and `G` (goto folder) never did.
These tests compare the panel's rows against the ``BINDINGS`` the app,
the reader screen and its three panels declare, in both directions, so
a key added without a row fails here — and so does a row left behind by
a key that is gone.
"""

from __future__ import annotations

import unittest

from textual.binding import Binding

from pony.tui.app import PonyApp
from pony.tui.screens.help_screen import (
    _LEFT_SECTIONS,
    _RIGHT_SECTIONS,
    HelpScreen,
)
from pony.tui.screens.main_screen import MainScreen
from pony.tui.widgets.folder_panel import FolderPanel
from pony.tui.widgets.message_list import MessageListPanel
from pony.tui.widgets.message_view import MessageViewPanel

_SOURCES = {
    "PonyApp": PonyApp,
    "MainScreen": MainScreen,
    "HelpScreen": HelpScreen,
    "FolderPanel": FolderPanel,
    "MessageListPanel": MessageListPanel,
    "MessageViewPanel": MessageViewPanel,
}


def _declared_keys() -> dict[str, set[str]]:
    """Every key each class binds, keyed by the class's name."""
    return {
        name: {b.key for b in cls.BINDINGS if isinstance(b, Binding)}
        for name, cls in _SOURCES.items()
    }


def _documented_keys() -> set[str]:
    """Every key the cheatsheet's rows claim to document."""
    return {
        key
        for section in (*_LEFT_SECTIONS, *_RIGHT_SECTIONS)
        for row in section.bindings
        for key in row.keys
    }


class HelpPanelTests(unittest.TestCase):
    """The panel and the bindings are two spellings of one list."""

    def test_every_bound_key_has_a_row(self) -> None:
        documented = _documented_keys()
        for source, keys in _declared_keys().items():
            for key in sorted(keys):
                with self.subTest(source=source, key=key):
                    self.assertIn(
                        key,
                        documented,
                        f"{source} binds {key!r} but no help-panel row lists it — "
                        "add it to a row's keys in help_screen.py (and to the key "
                        "table in docs/tui.md)",
                    )

    def test_no_row_documents_a_key_that_is_gone(self) -> None:
        bound = set().union(*_declared_keys().values())
        for key in sorted(_documented_keys()):
            with self.subTest(key=key):
                self.assertIn(
                    key,
                    bound,
                    f"the help panel lists {key!r} but nothing binds it any more",
                )

    def test_rows_without_keys_are_deliberate(self) -> None:
        """A keyless row documents a Textual default, not one of ours."""
        keyless = [
            row.display
            for section in (*_LEFT_SECTIONS, *_RIGHT_SECTIONS)
            for row in section.bindings
            if not row.keys
        ]
        self.assertEqual(keyless, ["Enter"])
