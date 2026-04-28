"""Goto-folder dialog — fuzzy search to jump to any folder by name."""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.screen import Screen
from textual.widgets import Input, Label, ListItem, ListView

from ...domain import FolderRef


def _fuzzy_filter(query: str, folders: list[FolderRef]) -> list[FolderRef]:
    """Return folders matching *query* as a subsequence, scored by match span.

    Each character in *query* must appear in the search text in order,
    equivalent to the regex ``q[0].*?q[1].*?…``.  Shorter spans rank higher;
    ties are broken alphabetically by folder name.
    """
    pattern = re.compile(".*?".join(re.escape(ch) for ch in query.lower()))
    scored: list[tuple[int, str, FolderRef]] = []
    for ref in folders:
        text = f"{ref.account_name} {ref.folder_name}".lower()
        m = pattern.search(text)
        if m:
            scored.append((m.end() - m.start(), ref.folder_name, ref))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [ref for _, _, ref in scored]


class GotoFolderScreen(Screen[FolderRef | None]):
    """Floating fuzzy-search dialog for jumping to a folder.

    Dismissed with the chosen ``FolderRef`` or ``None`` on cancel.
    """

    INHERIT_BINDINGS = False

    CSS = """
    GotoFolderScreen {
        align: center middle;
    }

    #goto-container {
        width: 60%;
        height: 50%;
        max-height: 30;
        background: $boost;
        border: solid $primary;
        border-title-color: $accent;
        padding: 0 1;
    }

    #goto-input {
        width: 1fr;
        height: 1;
        border: none;
        background: $boost;
        margin-bottom: 1;
    }

    #goto-list {
        width: 1fr;
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, folders: list[FolderRef]) -> None:
        super().__init__()
        self._folders = folders
        self._matches: list[FolderRef] = list(folders)

    def compose(self) -> ComposeResult:
        with Vertical(id="goto-container") as v:
            v.border_title = "Go to folder"
            yield Input(placeholder="type to filter folders…", id="goto-input")
            yield ListView(id="goto-list")

    def on_mount(self) -> None:
        self._rebuild_list(self._folders)
        self.query_one(Input).focus()

    def _rebuild_list(self, folders: list[FolderRef]) -> None:
        lv = self.query_one(ListView)
        lv.clear()
        for ref in folders:
            lv.append(ListItem(Label(f"{ref.account_name}  /  {ref.folder_name}")))

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        self._matches = (
            _fuzzy_filter(query, self._folders) if query else list(self._folders)
        )
        self._rebuild_list(self._matches)

    def on_key(self, event: Key) -> None:
        lv = self.query_one(ListView)
        inp = self.query_one(Input)
        if event.key == "down" and self.focused is inp:
            lv.focus()
            event.stop()
        elif event.key == "up" and self.focused is lv and lv.index == 0:
            inp.focus()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._confirm()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self._confirm()

    def _confirm(self) -> None:
        idx = self.query_one(ListView).index
        if idx is not None and 0 <= idx < len(self._matches):
            self.dismiss(self._matches[idx])
        elif self._matches:
            self.dismiss(self._matches[0])
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
