"""Full-screen directory picker for saving message files.

Offers directory navigation (directories only), a ``[New Folder]`` button
that creates a subdirectory on the fly, a ``[Select]`` button to confirm,
and ``[Cancel]`` / Escape to abort.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DirectoryTree, Footer, Input, Label

from .floating_input_screen import FloatingInputScreen

# Persists the last-used directory across invocations within a session.
_session_dir: Path | None = None


# ---------------------------------------------------------------------------
# Directory-only tree
# ---------------------------------------------------------------------------


class _DirOnlyTree(DirectoryTree):
    """DirectoryTree variant that shows directories only."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths if p.is_dir()]


# ---------------------------------------------------------------------------
# Floating prompt for naming a new folder
# ---------------------------------------------------------------------------


class _CreateFolderScreen(FloatingInputScreen[str | None]):
    """One-line prompt for the new directory name."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="floating-bar"):
            yield Label("New folder name:", id="floating-label")
            yield Input(placeholder="folder name", id="floating-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip()
        self.dismiss(name if name else None)


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

    _DirOnlyTree {
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

    def __init__(self, start_dir: Path | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        initial = (start_dir or _session_dir or Path.home()).resolve()
        home = Path.home()
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
            yield _DirOnlyTree(self._root, id="dir-tree")
        with Horizontal(id="buttons"):
            yield Button("New Folder", id="new-folder")
            yield Button("Select", id="select", variant="success")
            yield Button("Cancel", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(_DirOnlyTree).focus()

    # ------------------------------------------------------------------
    # Tree events
    # ------------------------------------------------------------------

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        event.stop()
        self._selected = event.path
        self.query_one("#current-path", Label).update(str(event.path))

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
            tree = self.query_one(_DirOnlyTree)
            tree.reload()

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            _CreateFolderScreen(), _on_name
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)
