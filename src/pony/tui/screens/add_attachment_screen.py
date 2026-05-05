"""File-picker screen for selecting an attachment."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DirectoryTree, Footer, Input, Label
from textual.widgets._directory_tree import DirEntry
from textual.widgets.tree import TreeNode

# Persists the last-used directory across picker invocations within a session.
# None means "not yet used"; the picker then falls back to cwd at launch time.
_session_dir: Path | None = None


class AddAttachmentScreen(Screen[str | None]):
    """Full-screen file browser for picking an attachment.

    Navigation
    ----------
    - Arrow keys / n,p  — move cursor
    - Enter             — expand directory or attach file
    - ctrl+l            — jump focus to path bar (type a directory and Enter)
    - Esc               — cancel

    The tree is rooted at the user's home directory when possible so that
    parent folders are always visible and directly navigable.  On open the
    tree auto-expands to the last-used (or initial) directory.

    Typeahead
    ---------
    While the tree has focus, typing printable characters builds a search
    buffer and the cursor jumps to the first sibling whose name starts with
    that prefix (case-insensitive).  The buffer clears automatically after
    0.8 s of inactivity; Backspace trims one character.
    """

    INHERIT_BINDINGS = False

    CSS = """
    AddAttachmentScreen {
        layout: vertical;
    }

    #path-bar {
        height: 3;
        align: left middle;
        padding: 0 1;
        background: $boost;
        border-bottom: solid $primary;
    }

    #path-label {
        width: auto;
        color: $text-muted;
        margin-right: 1;
    }

    #path-input {
        width: 1fr;
        height: 1;
        border: none;
        background: $boost;
    }

    #tree-area {
        height: 1fr;
    }

    DirectoryTree {
        height: 1fr;
    }

    #hint-bar {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $boost;
        border-top: solid $panel;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+l", "focus_path", "Edit path", show=False),
    ]

    def __init__(self, start_dir: Path | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        initial = (start_dir or _session_dir or Path.cwd()).resolve()
        home = Path.home()
        # Root at home so parent directories are navigable, unless the initial
        # directory is outside home (e.g. a separate drive on Windows).
        try:
            initial.relative_to(home)
            self._root = home
        except ValueError:
            self._root = initial
        self._initial_dir = initial
        self._typeahead: str = ""
        self._typeahead_version: int = 0  # incremented each keystroke to debounce

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="path-bar"):
            yield Label("Path:", id="path-label")
            yield Input(str(self._initial_dir), id="path-input")
        with Vertical(id="tree-area"):
            yield DirectoryTree(self._root, id="file-tree")
        yield Label(
            "Enter=attach  ctrl+l=edit path  Esc=cancel",
            id="hint-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#file-tree", DirectoryTree)
        tree.focus()
        self._expand_to_dir(self._initial_dir)

    # ------------------------------------------------------------------
    # Tree events
    # ------------------------------------------------------------------

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        global _session_dir
        event.stop()
        _session_dir = event.path.parent
        self.dismiss(str(event.path))

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        """Keep the path bar in sync with the highlighted directory."""
        event.stop()
        self.query_one("#path-input", Input).value = str(event.path)

    # ------------------------------------------------------------------
    # Auto-expansion
    # ------------------------------------------------------------------

    def _expand_to_dir(self, target: Path) -> None:
        """Expand the tree and move the cursor to `target`."""
        try:
            parts = list(target.relative_to(self._root).parts)
        except ValueError:
            return
        if not parts:
            return

        tree = self.query_one("#file-tree", DirectoryTree)

        def step(node: TreeNode[DirEntry], remaining: list[str], retries: int) -> None:
            if not remaining:
                tree.move_cursor(node)
                tree.scroll_to_node(node)
                return
            if retries >= 30:
                return

            part = remaining[0]
            rest = remaining[1:]
            for child in node.children:
                if child.data and child.data.path.name.lower() == part.lower():
                    if rest:
                        child.expand()
                    tree.call_after_refresh(lambda c=child, r=rest: step(c, r, 0))
                    return

            # Children not yet loaded by the async loader — retry.
            tree.call_after_refresh(
                lambda n=node, rem=remaining, ret=retries: step(n, rem, ret + 1)
            )

        step(tree.root, parts, 0)

    # ------------------------------------------------------------------
    # Typeahead
    # ------------------------------------------------------------------

    def on_key(self, event: object) -> None:
        """Intercept printable keys while the tree is focused for typeahead."""
        from textual.events import Key

        if not isinstance(event, Key):
            return

        tree = self.query_one("#file-tree", DirectoryTree)
        if self.focused is not tree:
            return

        char = event.character
        if event.key == "backspace":
            if self._typeahead:
                event.prevent_default()
                event.stop()
                self._typeahead = self._typeahead[:-1]
                self._run_typeahead()
                self._update_hint()
            return

        if char is None or not char.isprintable() or len(char) != 1:
            return

        event.prevent_default()
        event.stop()
        self._typeahead += char
        self._run_typeahead()
        self._update_hint()
        self._schedule_clear()

    def _visible_nodes(self, tree: DirectoryTree) -> list[TreeNode[DirEntry]]:
        """Return all visible tree nodes in depth-first display order."""
        result: list[TreeNode[DirEntry]] = []

        def _walk(node: TreeNode[DirEntry]) -> None:
            result.append(node)
            if node._expanded:  # pyright: ignore[reportPrivateUsage]
                for child in node.children:
                    _walk(child)

        for child in tree.root.children:
            _walk(child)
        return result

    def _run_typeahead(self) -> None:
        """Jump to the first visible node whose name starts with the buffer."""
        if not self._typeahead:
            return
        tree = self.query_one("#file-tree", DirectoryTree)
        cursor = tree.cursor_node
        if cursor is None:
            return

        candidates = self._visible_nodes(tree)
        if not candidates:
            return

        query = self._typeahead.lower()
        try:
            start = candidates.index(cursor)
        except ValueError:
            start = 0

        # Search from the node after cursor, wrapping around.
        ordered = candidates[start + 1 :] + candidates[: start + 1]
        for node in ordered:
            label = str(node.label).lower()
            if label.startswith(query):
                tree.move_cursor(node)
                return

    def _update_hint(self) -> None:
        hint = self.query_one("#hint-bar", Label)
        if self._typeahead:
            hint.update(f"Search: {self._typeahead}▌  Backspace=trim  Esc=cancel")
        else:
            hint.update("Enter=attach  ctrl+l=edit path  Esc=cancel")

    def _schedule_clear(self) -> None:
        """Clear the typeahead buffer 0.8 s after the last keystroke."""
        self._typeahead_version += 1
        version = self._typeahead_version

        def _maybe_clear() -> None:
            if self._typeahead_version == version:
                self._typeahead = ""
                self._update_hint()

        self.set_timer(0.8, _maybe_clear)

    # ------------------------------------------------------------------
    # Path bar
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Jump the tree to a typed directory path."""
        event.stop()
        raw = event.value.strip()
        target = Path(raw).expanduser().resolve()
        if not target.is_dir():
            self.notify(f"Not a directory: {raw}", severity="error")
            return
        self._navigate_to(target)

    def _navigate_to(self, target: Path) -> None:
        global _session_dir
        _session_dir = target
        self._root = target
        self.query_one("#path-input", Input).value = str(target)
        tree = self.query_one("#file-tree", DirectoryTree)
        tree.path = target
        tree.focus()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_focus_path(self) -> None:
        inp = self.query_one("#path-input", Input)
        inp.focus()
        inp.select_all()
