"""Full-screen directory picker for saving message files.

Offers directory navigation (directories only), a ``[New Folder]`` button
that creates a subdirectory on the fly, a ``[Select]`` button to confirm,
and ``[Cancel]`` / Escape to abort.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DirectoryTree, Footer, Label

from ...paths import contain
from ..dir_tree import DirOnlyTree
from .floating_input_screen import SimpleInputScreen

# Persists the last-used directory across invocations within a session.
_session_dir: Path | None = None


# ---------------------------------------------------------------------------
# Floating prompt for naming a new folder
# ---------------------------------------------------------------------------


class _CreateFolderScreen(SimpleInputScreen):
    """One-line prompt for the new directory name."""

    INPUT_LABEL = "New folder name:"
    INPUT_PLACEHOLDER = "folder name"


# ---------------------------------------------------------------------------
# Main picker screen
# ---------------------------------------------------------------------------


class SaveFolderPickerScreen(Screen[Path | None]):
    """Full-screen directory picker for choosing where to save message files.

    Navigation
    ----------
    - Arrow keys — move cursor in the tree
    - Enter      — expand/collapse directory
    - Tab        — cycle focus between tree and buttons
    - Esc        — cancel

    Dismissed with the selected :class:`Path` on ``[Select]``, or ``None``
    on Cancel / Escape.  ``[New Folder]`` creates a subdirectory inside the
    currently highlighted directory and immediately selects it.
    """

    INHERIT_BINDINGS = False

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    CSS = """
    SaveFolderPickerScreen {
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

    #current-path {
        width: 1fr;
        color: $text;
    }

    DirOnlyTree {
        height: 1fr;
    }

    #buttons {
        height: auto;
        align: center middle;
        padding: 0 1;
        background: $boost;
        border-top: solid $panel;
    }

    #buttons Button {
        margin: 0 1;
        height: 1;
        min-width: 0;
        border: none;
        padding: 0 1;
    }

    #buttons Button:focus {
        text-style: reverse bold;
    }
    """

    def __init__(
        self,
        start_dir: Path | None = None,
        *,
        default_dir: Path | None = None,
        **kwargs: object,
    ) -> None:
        """Open at *start_dir*, or where the reader last saved.

        *default_dir* is where to open when nothing is remembered yet
        -- the downloads directory, for the keys that write a file
        there anyway.  It is a fallback rather than a start dir so that
        a reader who picks somewhere else keeps it for the session,
        which is the whole point of remembering one.
        """
        super().__init__(**kwargs)  # type: ignore[arg-type]
        # `contain` rather than `Path.home()`: under `PONY_ROOT` the
        # tree browses the sandbox, which is what keeps the suite from
        # walking the real home directory -- where it found a symlink
        # into a share that was down and took the screen with it.
        home = contain(Path.home()).resolve()
        initial = (start_dir or _session_dir or default_dir or home).resolve()
        try:
            initial.relative_to(home)
            self._root = home
        except ValueError:
            self._root = initial
        self._selected: Path = initial

    def compose(self) -> ComposeResult:
        with Horizontal(id="path-bar"):
            yield Label("Folder:", id="path-label")
            yield Label(str(self._selected), id="current-path")
        with Vertical():
            yield DirOnlyTree(self._root, id="dir-tree")
        with Horizontal(id="buttons"):
            yield Button("New Folder", id="new-folder")
            yield Button("Select", id="select", variant="success")
            yield Button("Cancel", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DirOnlyTree).focus()

    # ------------------------------------------------------------------
    # Tree events
    # ------------------------------------------------------------------

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        event.stop()
        self._selected = event.path.resolve()
        self.query_one("#current-path", Label).update(str(self._selected))

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "select":
            global _session_dir
            _session_dir = self._selected
            self.dismiss(self._selected)
        elif event.button.id == "new-folder":
            self._prompt_new_folder()

    def _prompt_new_folder(self) -> None:
        def _on_name(name: str | None) -> None:
            if not name:
                return
            new_dir = self._selected / name
            try:
                new_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Could not create folder: {exc}", severity="error"
                )
                return
            self._selected = new_dir
            self.query_one("#current-path", Label).update(str(new_dir))
            tree = self.query_one(DirOnlyTree)
            tree.reload()

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            _CreateFolderScreen(), _on_name
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)
