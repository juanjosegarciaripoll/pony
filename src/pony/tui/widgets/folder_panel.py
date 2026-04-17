"""Folder panel widget — collapsible per-account folder tree (left pane)."""

from __future__ import annotations

from dataclasses import dataclass

from textual.binding import Binding
from textual.message import Message
from textual.widgets import Tree
from textual.widgets._tree import TreeNode

from ...domain import AppConfig, FolderRef
from ...protocols import IndexRepository


class FolderPanel(Tree[FolderRef | None]):
    """Collapsible folder tree with per-account sections.

    Each root node represents one account (collapsible).  Each child node
    represents one folder, labelled with the folder name and unread count.

    Posting ``FolderPanel.FolderSelected`` when the user activates a node.
    """

    BORDER_TITLE = "Folders"

    BINDINGS = [
        Binding("n", "cursor_down", "Next", show=False),
        Binding("p", "cursor_up", "Prev", show=False),
        Binding("N", "next_inbox", "Next inbox", show=False),
        Binding("P", "prev_inbox", "Prev inbox", show=False),
    ]

    @dataclass
    class FolderSelected(Message):
        """Posted when the user selects a folder."""
        folder_ref: FolderRef

    def __init__(
        self,
        config: AppConfig,
        index: IndexRepository,
        **kwargs: object,
    ) -> None:
        super().__init__("Accounts", **kwargs)  # type: ignore[arg-type]
        self._config = config
        self._index = index
        self._inbox_nodes: list[TreeNode[FolderRef | None]] = []

    def on_mount(self) -> None:
        self.refresh_folders()
        self.call_after_refresh(self._select_first_inbox)

    def _select_first_inbox(self) -> None:
        if self._inbox_nodes:
            self.move_cursor(self._inbox_nodes[0])

    def refresh_folders(self) -> None:
        """Rebuild the tree from the current index state."""
        self._inbox_nodes = []
        self.clear()
        for account in self._config.accounts:
            account_node = self.root.add(account.name, data=None, expand=True)
            self._populate_account(account_node, account.name)
        self.root.expand()

    def _populate_account(
        self, account_node: TreeNode[FolderRef | None], account_name: str
    ) -> None:
        unread_counts = self._get_unread_counts(account_name)
        folder_names = sorted(unread_counts.keys())
        # Ensure INBOX is always first.
        if "INBOX" in folder_names:
            folder_names = ["INBOX"] + [f for f in folder_names if f != "INBOX"]
        for folder_name in folder_names:
            unread = unread_counts.get(folder_name, 0)
            label = f"{folder_name} ({unread})" if unread else folder_name
            folder_ref = FolderRef(
                account_name=account_name, folder_name=folder_name
            )
            node = account_node.add_leaf(label, data=folder_ref)
            if folder_name.lower() == "inbox":
                self._inbox_nodes.append(node)

    def _get_unread_counts(self, account_name: str) -> dict[str, int]:
        """Return {folder_name: unread_count} from the index."""
        from ...domain import MessageFlag, MessageStatus

        counts: dict[str, int] = {}
        sync_states = self._index.list_folder_sync_states(account_name=account_name)
        for state in sync_states:
            counts.setdefault(state.folder_name, 0)
        for folder_name in list(counts.keys()):
            folder_ref = FolderRef(account_name=account_name, folder_name=folder_name)
            msgs = self._index.list_folder_messages(folder=folder_ref)
            counts[folder_name] = sum(
                1
                for m in msgs
                if MessageFlag.SEEN not in m.local_flags
                and m.local_status == MessageStatus.ACTIVE
            )
        return counts

    def action_next_inbox(self) -> None:
        self._jump_inbox(delta=1)

    def action_prev_inbox(self) -> None:
        self._jump_inbox(delta=-1)

    def _jump_inbox(self, delta: int) -> None:
        if not self._inbox_nodes:
            return
        current = self.cursor_node
        try:
            idx = self._inbox_nodes.index(current) if current is not None else -2
        except ValueError:
            idx = -1 if delta > 0 else len(self._inbox_nodes)
        new_idx = idx + delta
        if 0 <= new_idx < len(self._inbox_nodes):
            self.move_cursor(self._inbox_nodes[new_idx])

    def get_search_scope(self) -> tuple[str | None, FolderRef | None]:
        """Return ``(account_name, folder_ref)`` for the current cursor position.

        - Folder node  → (account_name, FolderRef)  [folder-scoped search]
        - Account node → (account_name, None)        [account-wide search]
        - Nothing selected or root → (None, None)
        """
        node = self.cursor_node
        if node is None or node is self.root:
            return (None, None)
        data = node.data
        if data is not None:
            return (data.account_name, data)
        # Account node: parent is the tree root.
        if node.parent is self.root:
            return (str(node.label), None)
        return (None, None)

    def on_tree_node_selected(self, event: Tree.NodeSelected[FolderRef | None]) -> None:
        event.stop()
        if event.node.data is not None:
            self.post_message(self.FolderSelected(folder_ref=event.node.data))
