"""Folder panel widget — collapsible per-account folder tree (left pane)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic

from rich.markup import escape as markup_escape
from textual.binding import Binding
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Tree
from textual.widgets._tree import TreeNode

from ...domain import AppConfig, FolderRef
from ...protocols import IndexRepository, MirrorRepository
from ...views import FolderTreeNode, build_folder_tree, has_inbox_mail
from .edge_drag import DraggableEdgeMixin

ACCOUNT_MAIL_SUFFIX = "✉"

# Clock face marking a *scheduled* (not running) periodic sync.  A plain
# geometric glyph rather than an emoji clock: emoji presentation is
# double-width in some terminals and would shift the border title.
SCHEDULED_SYNC_MARK = "◷"

# Granularity of the scheduled-sync countdown: it repaints this often, and
# is rounded down to the same step so the title never claims precision it
# has not got.  A per-second repaint of a border title nobody reads to the
# second is work the event loop does not need to do.
COUNTDOWN_STEP_SECONDS = 30


type FolderPanelNodeData = FolderRef | str | None


def format_countdown(seconds: float) -> str:
    """Render *seconds* remaining as ``M:SS``, or ``H:MM:SS`` past an hour.

    Rounded down to ``COUNTDOWN_STEP_SECONDS``, the interval the display
    repaints at, so a value on screen is never stale in the direction that
    would overstate the wait.  A sync that is already due (or overdue,
    because the event loop was busy when the timer fired) reads ``0:00``
    rather than a negative number.
    """
    total = max(0, int(seconds))
    total -= total % COUNTDOWN_STEP_SECONDS
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_account_label(account_name: str, *, has_mail: bool) -> str:
    """Return the Rich-markup-safe account label shown in the folder tree."""
    label = markup_escape(account_name)
    if has_mail:
        return f"{label} {ACCOUNT_MAIL_SUFFIX}"
    return label


class FolderPanel(DraggableEdgeMixin, Tree[FolderPanelNodeData]):
    """Collapsible folder tree with per-account sections.

    Each root node represents one account (collapsible).  Each child node
    represents one folder, labelled with the folder name and unread count.

    Posting ``FolderPanel.FolderSelected`` when the user activates a node.
    """

    BORDER_TITLE = "Folders"

    # Deterministic spinner frames cycled by an advancing index (no
    # time/random source) so the syncing indicator is reproducible in tests.
    _SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    # The right border doubles as the resize handle for this pane.
    DRAG_EDGE = "right"

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
        self._inbox_nodes: list[TreeNode[FolderPanelNodeData]] = []
        self._spinner_timer: Timer | None = None
        self._spinner_index = 0
        self._syncing = False
        # ``time.monotonic()`` deadline of the next automatic sync, or None
        # while no periodic sync is scheduled.
        self._next_sync_deadline: float | None = None
        self._countdown_timer: Timer | None = None

    def on_mount(self) -> None:
        self.refresh_folders()
        self.call_after_refresh(self._select_first_inbox)

    def set_syncing(self, active: bool) -> None:
        """Show or hide a spinner on the border title while a sync runs.

        ``active=True`` starts a ~0.2s timer cycling a fixed spinner-frame
        tuple by an advancing index; ``active=False`` stops it and falls
        back to whatever the idle title is — the scheduled-sync countdown
        if one is armed, the plain ``"Folders"`` otherwise.  Idempotent in
        both directions.
        """
        if active:
            if self._spinner_timer is not None:
                return
            self._syncing = True
            self._spinner_index = 0
            self._refresh_border_title()
            self._spinner_timer = self.set_interval(0.2, self._advance_spinner)
        else:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self._syncing = False
            self._refresh_border_title()

    def set_next_sync(self, deadline: float) -> None:
        """Show a live countdown to the next scheduled periodic sync.

        *deadline* is a ``time.monotonic()`` value.  ``MainScreen`` calls
        this every time it arms the periodic timer and on every tick, so
        the countdown restarts in step with the schedule it describes.
        The repaint timer is installed on first use and then left
        running: once periodic sync is on there is no way to turn it
        back off, so there is nothing to tear down.
        """
        self._next_sync_deadline = deadline
        if self._countdown_timer is None:
            self._countdown_timer = self.set_interval(
                COUNTDOWN_STEP_SECONDS, self._refresh_border_title
            )
        self._refresh_border_title()

    def _advance_spinner(self) -> None:
        self._spinner_index += 1
        self._refresh_border_title()

    def _refresh_border_title(self) -> None:
        """Compose the border title from the two indicators.

        A sync in flight outranks the countdown: while it runs, when the
        *next* one is due is not what the user is waiting to read.
        """
        if self._syncing:
            frame = self._SPINNER_FRAMES[
                self._spinner_index % len(self._SPINNER_FRAMES)
            ]
            self.border_title = f"Folders {frame} syncing…"
        elif self._next_sync_deadline is not None:
            remaining = format_countdown(self._next_sync_deadline - monotonic())
            self.border_title = f"Folders {SCHEDULED_SYNC_MARK} {remaining}"
        else:
            self.border_title = "Folders"

    def _select_first_inbox(self) -> None:
        if self._inbox_nodes:
            node = self._inbox_nodes[0]
            self.move_cursor(node)
            if isinstance(node.data, FolderRef):
                self.post_message(self.FolderSelected(folder_ref=node.data))

    def refresh_folders(self) -> None:
        """Rebuild the tree from the current index state."""
        self._inbox_nodes = []
        self.clear()
        for account in self._config.accounts:
            unread_counts = self._get_unread_counts(account.name)
            account_node = self.root.add(
                format_account_label(
                    account.name,
                    has_mail=has_inbox_mail(unread_counts),
                ),
                data=account.name,
                expand=True,
            )
            self._populate_account(account_node, account.name, unread_counts)
        self.root.expand()

    def _populate_account(
        self,
        account_node: TreeNode[FolderPanelNodeData],
        account_name: str,
        unread_counts: Mapping[str, int],
    ) -> None:
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
        parent: TreeNode[FolderPanelNodeData],
        entry: FolderTreeNode,
    ) -> TreeNode[FolderPanelNodeData]:
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
        """``{folder_name: unread_count}`` for one account.

        Which folders exist is a question about the stores, not about
        this widget: see :func:`pony.mailbox_actions.folder_unread_counts`,
        which the browser's folder pane asks as well.
        """
        from ...mailbox_actions import Mailboxes, folder_unread_counts

        return folder_unread_counts(
            Mailboxes(
                config=self._config,
                index=self._index,
                mirrors=self._mirrors,
            ),
            account_name,
        )

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
        if isinstance(data, FolderRef):
            return (data.account_name, data)
        if isinstance(data, str):
            return (data, None)
        return (None, None)

    def select_folder_ref(self, target: FolderRef) -> None:
        """Move the tree cursor to *target* and post FolderSelected."""
        node = self._find_node_by_ref(self.root, target)
        if node is not None:
            self.move_cursor(node)
        self.post_message(self.FolderSelected(folder_ref=target))

    def _find_node_by_ref(
        self,
        parent: TreeNode[FolderPanelNodeData],
        target: FolderRef,
    ) -> TreeNode[FolderPanelNodeData] | None:
        for node in parent.children:
            if node.data == target:
                return node
            found = self._find_node_by_ref(node, target)
            if found is not None:
                return found
        return None

    def on_tree_node_selected(
        self, event: Tree.NodeSelected[FolderPanelNodeData]
    ) -> None:
        event.stop()
        if isinstance(event.node.data, FolderRef):
            self.post_message(self.FolderSelected(folder_ref=event.node.data))
