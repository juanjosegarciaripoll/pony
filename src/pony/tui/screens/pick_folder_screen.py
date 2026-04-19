"""Modal folder picker: select one ``(account, folder)`` target.

Used by the TUI copy action.  The tree is built from each account's
:class:`MirrorRepository`, not from the index, so it includes every
folder the mirror currently exposes — notably folders on local
accounts, which have no ``folder_sync_state`` rows and are therefore
invisible to the main-screen :class:`FolderPanel`.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Label, Tree

from ...domain import AppConfig, FolderRef
from ...protocols import MirrorRepository


class PickFolderScreen(Screen["FolderRef | None"]):
    """Full-screen folder picker.

    Dismisses with the selected :class:`FolderRef` on ``enter`` / click,
    or ``None`` on ``escape``.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
        Binding("q", "cancel", "Cancel", show=False),
    ]

    CSS = """
    PickFolderScreen {
        align: center middle;
    }

    #dialog {
        width: 60%;
        height: 70%;
        border: solid $primary;
        padding: 1 2;
    }

    #title {
        text-style: bold;
        margin-bottom: 1;
    }

    Tree {
        height: 1fr;
    }
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        mirrors: dict[str, MirrorRepository],
        title: str = "Copy to folder",
        exclude: FolderRef | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._mirrors = mirrors
        self._title = title
        # Excluding the source folder prevents the user from picking a
        # no-op copy target.  ``None`` disables the guard.
        self._exclude = exclude

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            tree: Tree[FolderRef | None] = Tree("Accounts", id="folder-tree")
            yield tree
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#folder-tree", Tree)
        tree.focus()
        tree.root.expand()
        for account in self._config.accounts:
            account_node = tree.root.add(account.name, data=None, expand=True)
            mirror = self._mirrors.get(account.name)
            if mirror is None:
                continue
            folders = mirror.list_folders(account_name=account.name)
            # Pin INBOX to the top when present, otherwise alphabetical.
            names = sorted(f.folder_name for f in folders)
            if "INBOX" in names:
                names = ["INBOX"] + [n for n in names if n != "INBOX"]
            for name in names:
                ref = FolderRef(account_name=account.name, folder_name=name)
                if ref == self._exclude:
                    continue
                account_node.add_leaf(name, data=ref)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select(self) -> None:
        tree = self.query_one("#folder-tree", Tree)
        node = tree.cursor_node
        if node is None or node.data is None:
            # Cursor on an account (no folder selected) or on root —
            # nothing to dismiss with.
            return
        self.dismiss(node.data)

    def on_tree_node_selected(
        self, event: Tree.NodeSelected[FolderRef | None],
    ) -> None:
        event.stop()
        if event.node.data is None:
            return
        self.dismiss(event.node.data)
