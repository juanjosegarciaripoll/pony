"""Folder panel widget — collapsible per-account folder tree (left pane)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from rich.markup import escape as markup_escape
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Tree
from textual.widgets._tree import TreeNode

from ...domain import AppConfig, FolderRef
from ...protocols import IndexRepository, MirrorRepository


@dataclass(frozen=True, slots=True)
class FolderTreeNode:
    """One node in the nested folder display tree.

    ``folder_ref`` is ``None`` for *synthetic* parents — tree levels
    that exist only because a child folder name was hierarchical
    (``Archives.2026`` with no ``Archives`` folder on the server).
    The stored folder_name on the server is always the *full* path
    (``Archives.2026``); nesting is a display concern only.
    """

    label: str                                # last path segment
    folder_ref: FolderRef | None              # None for synthetic parents
    own_unread: int                           # 0 for synthetic parents
    children: tuple[FolderTreeNode, ...] = field(default_factory=tuple)

    def descendant_unread(self) -> int:
        """Total unread in this node and all descendants.

        Used to decide whether a synthetic parent is dim (all quiet) or
        bright (at least one descendant has unread).
        """
        return self.own_unread + sum(
            c.descendant_unread() for c in self.children
        )


def _split_folder_name(name: str) -> tuple[str, ...]:
    """Split *name* on the first ``.`` or ``/`` delimiter it contains.

    Per-folder detection (rather than a single account-wide delimiter)
    handles the rare case where different folders on the same server
    use different conventions.  ``INBOX`` is never split.  Empty
    segments (leading / trailing / doubled delimiters) are dropped.
    """
    if name == "INBOX":
        return (name,)
    dot = name.find(".")
    slash = name.find("/")
    candidates = [pos for pos in (dot, slash) if pos != -1]
    if not candidates:
        return (name,)
    first = min(candidates)
    delim = name[first]
    segments = tuple(seg for seg in name.split(delim) if seg)
    return segments or (name,)


def build_folder_tree(
    *,
    folder_names: Sequence[str],
    unread_counts: Mapping[str, int],
    account_name: str,
) -> tuple[FolderTreeNode, ...]:
    """Build a nested display tree from flat folder names.

    The returned nodes are roots under the account.  ``INBOX`` (if
    present) is pinned first; remaining siblings are sorted
    alphabetically at every level.
    """
    # Collect every distinct path prefix: ancestors are created on
    # demand, a real folder's full segmentation is the exact path.
    real_by_path: dict[tuple[str, ...], str] = {}
    all_paths: set[tuple[str, ...]] = set()
    for name in folder_names:
        segments = _split_folder_name(name)
        real_by_path[segments] = name
        for i in range(1, len(segments) + 1):
            all_paths.add(segments[:i])

    def _build_at(prefix: tuple[str, ...]) -> tuple[FolderTreeNode, ...]:
        direct = sorted({
            p[len(prefix)]
            for p in all_paths
            if len(p) == len(prefix) + 1 and p[: len(prefix)] == prefix
        })
        nodes: list[FolderTreeNode] = []
        for seg in direct:
            child_path = (*prefix, seg)
            real_name = real_by_path.get(child_path)
            folder_ref = (
                FolderRef(account_name=account_name, folder_name=real_name)
                if real_name is not None else None
            )
            own_unread = (
                unread_counts.get(real_name, 0) if real_name else 0
            )
            nodes.append(FolderTreeNode(
                label=seg,
                folder_ref=folder_ref,
                own_unread=own_unread,
                children=_build_at(child_path),
            ))
        return tuple(nodes)

    roots = list(_build_at(()))
    inbox = [n for n in roots if n.label == "INBOX"]
    others = [n for n in roots if n.label != "INBOX"]
    return tuple(inbox + others)


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
        mirrors: dict[str, MirrorRepository],
        **kwargs: object,
    ) -> None:
        super().__init__("Accounts", **kwargs)  # type: ignore[arg-type]
        self._config = config
        self._index = index
        self._mirrors = mirrors
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
        tree = build_folder_tree(
            folder_names=tuple(unread_counts.keys()),
            unread_counts=unread_counts,
            account_name=account_name,
        )
        for root in tree:
            node = self._attach_tree_node(account_node, root)
            if root.label == "INBOX":
                self._inbox_nodes.append(node)

    def _attach_tree_node(
        self,
        parent: TreeNode[FolderRef | None],
        entry: FolderTreeNode,
    ) -> TreeNode[FolderRef | None]:
        """Attach one FolderTreeNode (and its subtree) under *parent*.

        Leaves show ``name (N)`` when N > 0 and ``name`` otherwise.
        A node (leaf or branch) is rendered ``[dim]…[/dim]`` when no
        descendant has unread — that's the at-a-glance signal the user
        asked for.
        """
        label_text = entry.label
        if entry.folder_ref is not None and entry.own_unread > 0:
            label_text = f"{entry.label} ({entry.own_unread})"
        markup = markup_escape(label_text)
        if entry.descendant_unread() == 0:
            markup = f"[dim]{markup}[/dim]"

        if entry.children:
            node = parent.add(markup, data=entry.folder_ref, expand=True)
            for child in entry.children:
                self._attach_tree_node(node, child)
        else:
            node = parent.add_leaf(markup, data=entry.folder_ref)
        return node

    def _get_unread_counts(self, account_name: str) -> dict[str, int]:
        """Return ``{folder_name: unread_count}`` for one account.

        Folders come from the account's mirror — the single source of
        truth that covers both IMAP accounts (where the mirror tracks
        the server) and local accounts (where the mirror *is* the
        account).  Discovering folders from ``folder_sync_state`` alone
        used to hide local accounts entirely from the tree, since sync
        never writes a state row for them.

        Unread counts come from a single ``GROUP BY`` against the
        ``messages`` table — previously we materialised every
        ``IndexedMessage`` for every folder just to count, which for a
        big account (tens of thousands of rows across dozens of
        folders) dominated tree-rebuild time.
        """
        counts: dict[str, int] = {}
        mirror = self._mirrors.get(account_name)
        if mirror is not None:
            for ref in mirror.list_folders(account_name=account_name):
                counts.setdefault(ref.folder_name, 0)
        # Also honour any sync-state folder entry that isn't on disk yet
        # (e.g. empty remote folders the sync engine has recorded but
        # the mirror hasn't materialised on this host).
        for state in self._index.list_folder_sync_states(account_name=account_name):
            counts.setdefault(state.folder_name, 0)
        unread = self._index.unread_counts_by_folder(account_name=account_name)
        for folder_name, n in unread.items():
            # Folders from the index that aren't on disk still belong
            # in the tree — they might be hidden remote folders.
            counts[folder_name] = n
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
