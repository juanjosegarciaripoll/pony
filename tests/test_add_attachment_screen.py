"""Tests for AddAttachmentScreen — the attachment file-picker.

The screen is normally pushed by the compose flow; here it is driven in
isolation through a minimal host ``App`` (same approach as
``tests/test_save_message_screen.py``) plus Textual's ``Pilot``.

``asyncio_mode = "auto"`` (see ``pyproject.toml``) means ``async def test_*``
coroutines run directly without an explicit decorator.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import DirectoryTree, Input

import pony.tui.screens.add_attachment_screen as aas_module
from pony.tui.screens.add_attachment_screen import AddAttachmentScreen


def _make_tree(label: str) -> Path:
    """Create a temp directory tree with a couple of files and a subdir.

    Returns the directory path.  ``tempfile`` lives outside the home
    directory, so a screen rooted here exercises the "outside home" branch.
    """
    base = Path(tempfile.mkdtemp(prefix=f"pony-attach-{label}-"))
    (base / "alpha.txt").write_text("a", encoding="utf-8")
    (base / "beta.txt").write_text("b", encoding="utf-8")
    sub = base / "subdir"
    sub.mkdir()
    (sub / "gamma.txt").write_text("g", encoding="utf-8")
    return base


def _make_home_tree(label: str) -> Path:
    """Create a temp directory *inside* the home directory.

    A screen started here exercises the branch that roots the tree at
    ``Path.home()`` so parent folders stay navigable.
    """
    home_base = Path.home() / ".pony-test-attach"
    home_base.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix=f"{label}-", dir=home_base))
    (base / "file.txt").write_text("x", encoding="utf-8")
    return base


class _Host(App[str | None]):
    """Minimal host app that immediately pushes the picker screen."""

    def __init__(self, start_dir: Path) -> None:
        super().__init__()
        self._start_dir = start_dir

    def compose(self) -> ComposeResult:
        return iter([])

    def on_mount(self) -> None:
        self.push_screen(AddAttachmentScreen(self._start_dir), self.exit)


def _screen(pilot_app: App[str | None]) -> AddAttachmentScreen:
    screen = pilot_app.screen
    assert isinstance(screen, AddAttachmentScreen)
    return screen


# ---------------------------------------------------------------------------
# Unit tests — construction / root selection (no Pilot required)
# ---------------------------------------------------------------------------


class ConstructionTest(unittest.TestCase):
    def setUp(self) -> None:
        # Each test should start from a clean session-dir state.
        aas_module._session_dir = None

    def tearDown(self) -> None:
        aas_module._session_dir = None

    def test_outside_home_roots_at_initial_dir(self) -> None:
        base = _make_tree("ctor-out")
        screen = AddAttachmentScreen(base)
        self.assertEqual(screen._root, base.resolve())
        self.assertEqual(screen._initial_dir, base.resolve())

    def test_inside_home_roots_at_home(self) -> None:
        base = _make_home_tree("ctor-in")
        screen = AddAttachmentScreen(base)
        self.assertEqual(screen._root, Path.home())
        self.assertEqual(screen._initial_dir, base.resolve())

    def test_session_dir_used_when_no_start_dir(self) -> None:
        base = _make_tree("ctor-session")
        aas_module._session_dir = base
        screen = AddAttachmentScreen()
        self.assertEqual(screen._initial_dir, base.resolve())

    def test_falls_back_to_cwd(self) -> None:
        # No start_dir, no session dir -> resolves to cwd, no crash.
        screen = AddAttachmentScreen()
        self.assertTrue(screen._initial_dir.is_absolute())


# ---------------------------------------------------------------------------
# Pilot (async) tests — interaction
# ---------------------------------------------------------------------------


async def test_mount_focuses_tree_and_seeds_path() -> None:
    aas_module._session_dir = None
    base = _make_tree("mount")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        tree = screen.query_one("#file-tree", DirectoryTree)
        self_inp = screen.query_one("#path-input", Input)
        assert screen.focused is tree
        assert self_inp.value == str(base.resolve())


async def test_select_file_dismisses_with_path() -> None:
    aas_module._session_dir = None
    base = _make_tree("select-file")
    target = base / "alpha.txt"
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        tree = screen.query_one("#file-tree", DirectoryTree)
        # Emit the FileSelected message directly — robust against async load.
        screen.on_directory_tree_file_selected(
            DirectoryTree.FileSelected(tree.root, target)
        )
        await pilot.pause()
    assert pilot.app.return_value == str(target)
    # Selecting a file remembers its parent directory for next time.
    assert aas_module._session_dir == target.parent


async def test_directory_selected_updates_path_input() -> None:
    aas_module._session_dir = None
    base = _make_tree("dir-select")
    sub = base / "subdir"
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        tree = screen.query_one("#file-tree", DirectoryTree)
        screen.on_directory_tree_directory_selected(
            DirectoryTree.DirectorySelected(tree.root, sub)
        )
        await pilot.pause()
        inp = screen.query_one("#path-input", Input)
        assert inp.value == str(sub)


async def test_escape_cancels_with_none() -> None:
    aas_module._session_dir = None
    base = _make_tree("escape")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert pilot.app.return_value is None


async def test_action_cancel_dismisses_none() -> None:
    aas_module._session_dir = None
    base = _make_tree("action-cancel")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        screen.action_cancel()
        await pilot.pause()
    assert pilot.app.return_value is None


async def test_focus_path_action_focuses_input() -> None:
    aas_module._session_dir = None
    base = _make_tree("focus-path")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+l")
        await pilot.pause()
        screen = _screen(pilot.app)
        inp = screen.query_one("#path-input", Input)
        assert screen.focused is inp


async def test_input_submit_valid_dir_navigates() -> None:
    aas_module._session_dir = None
    base = _make_tree("submit-valid")
    sub = base / "subdir"
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        inp = screen.query_one("#path-input", Input)
        inp.value = str(sub)
        screen.on_input_submitted(Input.Submitted(inp, str(sub)))
        await pilot.pause()
        # Navigation re-roots the tree and rewrites the path bar.
        assert screen._root == sub
        assert screen.query_one("#path-input", Input).value == str(sub)
        tree = screen.query_one("#file-tree", DirectoryTree)
        assert Path(tree.path) == sub
    assert aas_module._session_dir == sub


async def test_input_submit_invalid_dir_notifies_error() -> None:
    aas_module._session_dir = None
    base = _make_tree("submit-invalid")
    missing = base / "does-not-exist"
    notifications: list[str] = []
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        original_root = screen._root
        # Capture notify calls without going through the toast subsystem.
        screen.notify = lambda message, **_: notifications.append(message)  # type: ignore[method-assign]
        inp = screen.query_one("#path-input", Input)
        screen.on_input_submitted(Input.Submitted(inp, str(missing)))
        await pilot.pause()
        # Root unchanged; an error was reported.
        assert screen._root == original_root
        assert notifications
        assert "Not a directory" in notifications[0]


# ---------------------------------------------------------------------------
# Typeahead
# ---------------------------------------------------------------------------


async def test_typeahead_jumps_to_matching_node() -> None:
    aas_module._session_dir = None
    base = _make_tree("typeahead")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        # Let the async directory loader populate the root children.
        for _ in range(5):
            await pilot.pause()
        screen = _screen(pilot.app)
        tree = screen.query_one("#file-tree", DirectoryTree)
        assert screen.focused is tree

        # Type "b" — should land on beta.txt; the hint reflects the buffer.
        await pilot.press("b")
        await pilot.pause()
        assert screen._typeahead == "b"
        hint = screen.query_one("#hint-bar")
        assert "Search: b" in str(hint.render())

        cursor = tree.cursor_node
        assert cursor is not None
        assert str(cursor.label).lower().startswith("b")


async def test_typeahead_backspace_trims_buffer() -> None:
    aas_module._session_dir = None
    base = _make_tree("backspace")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause()
        screen = _screen(pilot.app)
        await pilot.press("a")
        await pilot.press("l")
        await pilot.pause()
        assert screen._typeahead == "al"
        await pilot.press("backspace")
        await pilot.pause()
        assert screen._typeahead == "a"


async def test_typeahead_ignored_when_input_focused() -> None:
    aas_module._session_dir = None
    base = _make_tree("no-typeahead")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        inp = screen.query_one("#path-input", Input)
        inp.focus()
        await pilot.pause()
        await pilot.press("z")
        await pilot.pause()
        # Tree typeahead buffer stays empty; the key went to the Input.
        assert screen._typeahead == ""


async def test_backspace_without_buffer_is_noop() -> None:
    aas_module._session_dir = None
    base = _make_tree("bs-empty")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause()
        screen = _screen(pilot.app)
        assert screen._typeahead == ""
        await pilot.press("backspace")
        await pilot.pause()
        assert screen._typeahead == ""


async def test_typeahead_buffer_clears_after_timeout() -> None:
    aas_module._session_dir = None
    base = _make_tree("clear-timeout")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause()
        screen = _screen(pilot.app)

        # Capture the callback the debounce timer would fire so we can run it
        # immediately instead of sleeping for the full 0.8 s window.
        captured: list[object] = []
        real_set_timer = screen.set_timer

        def _capture(delay: float, callback: object = None, **kwargs: object) -> object:
            captured.append(callback)
            return real_set_timer(delay, callback, **kwargs)  # type: ignore[arg-type]

        screen.set_timer = _capture  # type: ignore[assignment,method-assign]

        await pilot.press("a")
        await pilot.pause()
        assert screen._typeahead == "a"
        assert captured, "typing a printable key should schedule a clear timer"

        # Run the captured callback now — the buffer should reset.
        clear_cb = captured[-1]
        assert callable(clear_cb)
        clear_cb()
        await pilot.pause()
        assert screen._typeahead == ""
        hint = screen.query_one("#hint-bar")
        assert "Enter=attach" in str(hint.render())


async def test_typeahead_no_match_keeps_cursor() -> None:
    """A query that matches nothing leaves the cursor where it was."""
    aas_module._session_dir = None
    base = _make_tree("no-match")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause()
        screen = _screen(pilot.app)
        tree = screen.query_one("#file-tree", DirectoryTree)
        before = tree.cursor_node
        # No entry starts with "zzz".
        await pilot.press("z")
        await pilot.press("z")
        await pilot.press("z")
        await pilot.pause()
        assert screen._typeahead == "zzz"
        assert tree.cursor_node is before


async def test_run_typeahead_empty_buffer_is_noop() -> None:
    """Calling the jump helper with an empty buffer returns immediately."""
    aas_module._session_dir = None
    base = _make_tree("empty-run")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        assert screen._typeahead == ""
        # Should be a clean no-op (covers the early return guard).
        screen._run_typeahead()
        await pilot.pause()
        assert screen._typeahead == ""


async def test_expanded_directory_is_walked_for_typeahead() -> None:
    """Expanding a subdirectory makes its children visible to typeahead."""
    aas_module._session_dir = None
    base = _make_tree("walk-expanded")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause()
        screen = _screen(pilot.app)
        tree = screen.query_one("#file-tree", DirectoryTree)
        # Find and expand the "subdir" node so its child becomes visible.
        for child in tree.root.children:
            if child.data and child.data.path.name == "subdir":
                child.expand()
                break
        for _ in range(5):
            await pilot.pause()
        visible = screen._visible_nodes(tree)
        names = {str(n.label).lower() for n in visible}
        assert "subdir" in names
        # gamma.txt lives inside the now-expanded subdir.
        assert any("gamma" in n for n in names)


async def test_auto_expand_to_nested_start_dir() -> None:
    """Starting in a nested home directory auto-expands the tree to it."""
    aas_module._session_dir = None
    base = _make_home_tree("auto-expand")
    try:
        async with _Host(base).run_test() as pilot:
            await pilot.pause()
            screen = _screen(pilot.app)
            assert screen._root == Path.home()
            # Pump refreshes so the recursive _expand_to_dir steps can run
            # as the async directory loader populates each level.
            for _ in range(40):
                await pilot.pause()
            tree = screen.query_one("#file-tree", DirectoryTree)
            # The screen should not have crashed and the tree is mounted.
            assert tree.is_mounted
    finally:
        # Clean up the home-side temp tree we created.
        import shutil

        shutil.rmtree(base, ignore_errors=True)


async def test_expand_to_dir_outside_root_is_noop() -> None:
    """A target outside the tree root short-circuits without error."""
    aas_module._session_dir = None
    base = _make_tree("expand-outside")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        # Root is `base`; pick an absolute path that is not under it.
        outside = Path(base.anchor) / "definitely-not-under-base"
        screen._expand_to_dir(outside)  # ValueError -> early return
        await pilot.pause()
        tree = screen.query_one("#file-tree", DirectoryTree)
        assert tree.is_mounted


async def test_expand_to_dir_equal_to_root_is_noop() -> None:
    """When the target is the root itself there are no parts to descend."""
    aas_module._session_dir = None
    base = _make_tree("expand-root")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        screen._expand_to_dir(base.resolve())  # empty parts -> early return
        await pilot.pause()
        tree = screen.query_one("#file-tree", DirectoryTree)
        assert tree.is_mounted


async def test_run_typeahead_with_no_cursor_is_noop() -> None:
    """The jump helper bails out when the tree has no cursor node."""
    aas_module._session_dir = None
    base = _make_tree("no-cursor")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        tree = screen.query_one("#file-tree", DirectoryTree)
        # Force a buffer, then make the tree report no cursor node via a stub
        # returned from query_one (avoids mutating the widget class).
        screen._typeahead = "a"

        class _NoCursorTree:
            cursor_node = None

        screen.query_one = lambda *_args, **_kw: _NoCursorTree()  # type: ignore[assignment,method-assign]
        try:
            screen._run_typeahead()  # cursor None -> early return, no crash
        finally:
            del screen.query_one  # restore the inherited method
        await pilot.pause()
        # query_one is back; sanity-check the real tree still resolves.
        assert screen.query_one("#file-tree", DirectoryTree) is tree
        assert screen._typeahead == "a"


async def test_run_typeahead_with_no_candidates_is_noop() -> None:
    """An empty visible-node list short-circuits the jump helper."""
    aas_module._session_dir = None
    base = _make_tree("no-candidates")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause()
        screen = _screen(pilot.app)
        screen._typeahead = "a"
        # Patch _visible_nodes to report nothing while a cursor still exists.
        screen._visible_nodes = lambda _tree: []  # type: ignore[assignment,method-assign]
        screen._run_typeahead()  # no candidates -> early return
        await pilot.pause()
        assert screen._typeahead == "a"


async def test_on_key_ignores_non_key_event() -> None:
    """A non-Key object passed to on_key is silently ignored."""
    aas_module._session_dir = None
    base = _make_tree("non-key")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        screen.on_key(object())  # not a textual Key -> early return
        await pilot.pause()
        assert screen._typeahead == ""


async def test_on_key_ignored_when_tree_not_focused() -> None:
    """on_key does nothing for printable keys while the tree is unfocused."""
    from textual.events import Key

    aas_module._session_dir = None
    base = _make_tree("not-focused")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        screen = _screen(pilot.app)
        inp = screen.query_one("#path-input", Input)
        inp.focus()
        await pilot.pause()
        screen.on_key(Key(key="q", character="q"))  # tree not focused
        await pilot.pause()
        assert screen._typeahead == ""


async def test_schedule_clear_ignores_stale_version() -> None:
    """A clear callback is a no-op once a newer keystroke bumped the version."""
    aas_module._session_dir = None
    base = _make_tree("stale-clear")
    async with _Host(base).run_test() as pilot:
        await pilot.pause()
        for _ in range(5):
            await pilot.pause()
        screen = _screen(pilot.app)

        captured: list[object] = []
        real_set_timer = screen.set_timer

        def _capture(delay: float, callback: object = None, **kwargs: object) -> object:
            captured.append(callback)
            return real_set_timer(delay, callback, **kwargs)  # type: ignore[arg-type]

        screen.set_timer = _capture  # type: ignore[assignment,method-assign]

        await pilot.press("a")
        await pilot.pause()
        stale_cb = captured[-1]
        assert callable(stale_cb)
        # A second keystroke bumps the version, making the first timer stale.
        await pilot.press("l")
        await pilot.pause()
        assert screen._typeahead == "al"
        stale_cb()  # should NOT clear, since the version moved on
        assert screen._typeahead == "al"
