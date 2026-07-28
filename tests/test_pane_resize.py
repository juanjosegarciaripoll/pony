"""Resizable panes: the keybindings, the clamps, and the saved state.

The three panes (folders, message list, reader) are laid out by CSS with
fixed proportions.  ``Ctrl`` plus an arrow key moves the boundary between
them, and the result is written to a small JSON file so it is still there
next time.

The list/reader boundary uses ``fr`` units rather than percentages
because the reader is ``display: none`` until a message is opened — with
``fr`` the list expands to fill the pane on its own, where a percentage
would leave the reader's share blank.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import uuid4

from conftest import TMP_ROOT
from corpus import plain_text
from tui_helpers import build_pony_app

from pony.domain import FolderRef
from pony.tui.screens.main_screen import MainScreen
from pony.tui.ui_state import (
    DEFAULT_FOLDER_WIDTH_PCT,
    DEFAULT_LIST_SHARE,
    MAX_FOLDER_WIDTH_PCT,
    MIN_FOLDER_WIDTH_PCT,
    PaneSizes,
    load_pane_sizes,
    save_pane_sizes,
)
from pony.tui.widgets.folder_panel import FolderPanel
from pony.tui.widgets.message_list import MessageListPanel
from pony.tui.widgets.message_view import MessageViewPanel

_INBOX = FolderRef(account_name="acct", folder_name="INBOX")


def _state_path() -> Path:
    root = TMP_ROOT / "ui-state" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root / "ui_state.json"


def _screen(app: object) -> MainScreen:
    screen = app.screen  # type: ignore[attr-defined]
    assert isinstance(screen, MainScreen)
    return screen


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


class PaneSizeStateTest(unittest.TestCase):
    """The state file is advisory — never a reason to fail startup."""

    def test_a_missing_file_yields_the_defaults(self) -> None:
        self.assertEqual(load_pane_sizes(_state_path()), PaneSizes())
        self.assertEqual(load_pane_sizes(None), PaneSizes())

    def test_a_round_trip_preserves_the_values(self) -> None:
        path = _state_path()
        save_pane_sizes(path, PaneSizes(folder_width_pct=40, list_share=60))

        self.assertEqual(
            load_pane_sizes(path), PaneSizes(folder_width_pct=40, list_share=60)
        )

    def test_saving_creates_the_directory(self) -> None:
        path = _state_path().parent / "nested" / "ui_state.json"
        save_pane_sizes(path, PaneSizes())

        self.assertTrue(path.is_file())

    def test_malformed_json_falls_back_to_the_defaults(self) -> None:
        path = _state_path()
        path.write_text("{not json at all", encoding="utf-8")

        self.assertEqual(load_pane_sizes(path), PaneSizes())

    def test_a_json_value_that_is_not_an_object_is_ignored(self) -> None:
        path = _state_path()
        path.write_text("[1, 2, 3]", encoding="utf-8")

        self.assertEqual(load_pane_sizes(path), PaneSizes())

    def test_non_integer_values_are_ignored(self) -> None:
        path = _state_path()
        path.write_text('{"folder_width_pct": "wide"}', encoding="utf-8")

        self.assertEqual(load_pane_sizes(path), PaneSizes())

    def test_out_of_range_values_are_clamped_on_load(self) -> None:
        """A hand-edited file cannot push a pane off the screen."""
        path = _state_path()
        path.write_text(
            '{"folder_width_pct": 500, "list_share": -20}', encoding="utf-8"
        )

        loaded = load_pane_sizes(path)

        self.assertEqual(loaded.folder_width_pct, MAX_FOLDER_WIDTH_PCT)
        self.assertGreaterEqual(loaded.list_share, 1)

    def test_an_unwritable_destination_is_shrugged_off(self) -> None:
        """A read-only data dir must not interrupt the user."""
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # A directory where the file should be makes the write fail.
        path.mkdir()

        save_pane_sizes(path, PaneSizes())  # must not raise

    def test_saving_to_no_path_is_a_noop(self) -> None:
        save_pane_sizes(None, PaneSizes())  # must not raise


# ---------------------------------------------------------------------------
# Keybindings
# ---------------------------------------------------------------------------


async def test_the_folder_panel_widens_and_narrows() -> None:
    app, *_ = build_pony_app(label="resize-folders", seed=[(_INBOX, plain_text())])

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one(FolderPanel)
        start = panel.size.width

        await pilot.press("ctrl+right")
        await pilot.pause()
        wider = panel.size.width
        assert wider > start

        await pilot.press("ctrl+left")
        await pilot.press("ctrl+left")
        await pilot.pause()
        assert panel.size.width < start


async def test_the_message_list_grows_and_shrinks_against_the_reader() -> None:
    app, *_ = build_pony_app(label="resize-list", seed=[(_INBOX, plain_text())])

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # open the reader so both panes are visible
        await pilot.pause()
        message_list = app.screen.query_one(MessageListPanel)
        reader = app.screen.query_one(MessageViewPanel)
        start_list = message_list.size.height
        start_reader = reader.size.height

        await pilot.press("ctrl+down")
        await pilot.pause()
        assert message_list.size.height > start_list
        assert reader.size.height < start_reader

        await pilot.press("ctrl+up")
        await pilot.press("ctrl+up")
        await pilot.pause()
        assert message_list.size.height < start_list


async def test_the_list_still_fills_the_pane_while_the_reader_is_closed() -> None:
    """The reason the split uses ``fr`` and not percentages."""
    app, *_ = build_pony_app(label="resize-hidden", seed=[(_INBOX, plain_text())])

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        message_list = app.screen.query_one(MessageListPanel)
        reader = app.screen.query_one(MessageViewPanel)
        right_pane = app.screen.query_one("#right-pane")
        assert reader.display is False

        await pilot.press("ctrl+down")
        await pilot.pause()

        # No blank band where the hidden reader's share would be: the
        # list's outer box (content + border) is the whole pane.
        assert message_list.outer_size.height == right_pane.size.height


async def test_resizing_stops_at_the_limits() -> None:
    """Holding the key down cannot collapse or hide a pane."""
    app, *_ = build_pony_app(label="resize-clamp", seed=[(_INBOX, plain_text())])

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = _screen(app)

        for _ in range(30):
            await pilot.press("ctrl+left")
        await pilot.pause()
        assert screen._pane_sizes.folder_width_pct == MIN_FOLDER_WIDTH_PCT
        assert app.screen.query_one(FolderPanel).size.width > 0

        for _ in range(40):
            await pilot.press("ctrl+right")
        await pilot.pause()
        assert screen._pane_sizes.folder_width_pct == MAX_FOLDER_WIDTH_PCT


# ---------------------------------------------------------------------------
# Persistence through the screen
# ---------------------------------------------------------------------------


async def test_a_resize_is_written_to_the_state_file() -> None:
    path = _state_path()
    app, *_ = build_pony_app(label="resize-save", seed=[(_INBOX, plain_text())])

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _screen(app)._ui_state_path = path

        await pilot.press("ctrl+right")
        await pilot.pause()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["folder_width_pct"] > DEFAULT_FOLDER_WIDTH_PCT
    assert saved["list_share"] == DEFAULT_LIST_SHARE


async def test_saved_sizes_are_restored_on_the_next_launch() -> None:
    path = _state_path()
    save_pane_sizes(path, PaneSizes(folder_width_pct=45, list_share=70))

    app, *_ = build_pony_app(label="resize-restore", seed=[(_INBOX, plain_text())])
    app._ui_state_path = path  # type: ignore[attr-defined]

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = _screen(app)

        assert screen._pane_sizes == PaneSizes(folder_width_pct=45, list_share=70)
        # 45 % of 120 columns, give or take the border.
        assert 50 <= app.screen.query_one(FolderPanel).size.width <= 56


async def test_a_resize_at_the_limit_does_not_rewrite_the_file() -> None:
    """Already-clamped keypresses are a no-op, not a redundant write."""
    path = _state_path()
    app, *_ = build_pony_app(label="resize-noop", seed=[(_INBOX, plain_text())])

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = _screen(app)
        screen._pane_sizes = PaneSizes(
            folder_width_pct=MIN_FOLDER_WIDTH_PCT, list_share=DEFAULT_LIST_SHARE
        )
        screen._ui_state_path = path

        await pilot.press("ctrl+left")
        await pilot.pause()

    assert not path.exists()
