"""Sync confirmation screen: show plan, ask user to proceed or cancel."""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Label, ProgressBar, Static

from ...sync import (
    FetchNewOp,
    MergeFlagsOp,
    ProgressInfo,
    PullFlagsOp,
    PurgeLocalOp,
    PushAppendOp,
    PushDeleteOp,
    PushFlagsOp,
    PushMoveOp,
    RestoreOp,
    ReUploadOp,
    ServerDeleteOp,
    SyncOp,
    SyncPlan,
)
from .dialog_screen import DialogScreen


def _categorize(ops: tuple[SyncOp, ...]) -> dict[str, int]:
    """Group ops into named categories with counts."""
    counts: dict[str, int] = {}
    for op in ops:
        if isinstance(op, FetchNewOp):
            key = "download"
        elif isinstance(op, ServerDeleteOp):
            key = "server_delete"
        elif isinstance(op, PushMoveOp):
            key = "push_move"
        elif isinstance(op, PushAppendOp):
            key = "push_append"
        elif isinstance(op, PushDeleteOp):
            key = "push_delete"
        elif isinstance(op, PullFlagsOp):
            key = "pull_flags"
        elif isinstance(op, PushFlagsOp):
            key = "push_flags"
        elif isinstance(op, MergeFlagsOp):
            key = "merge_flags"
        elif isinstance(op, RestoreOp):
            key = "restore"
        elif isinstance(op, ReUploadOp):
            key = "reupload"
        elif isinstance(op, PurgeLocalOp):
            key = "purge"
        else:
            key = "other"
        counts[key] = counts.get(key, 0) + 1
    return counts


_LABELS: tuple[tuple[str, str], ...] = (
    ("download", "{n} new message(s) to download"),
    ("server_delete", "{n} deleted on server (move to trash)"),
    ("push_move", "{n} moved locally (push to server)"),
    ("push_append", "{n} new local message(s) to upload"),
    ("push_delete", "{n} local deletion(s) to expunge"),
    ("pull_flags", "{n} flag update(s) from server"),
    ("push_flags", "{n} flag update(s) to push"),
    ("merge_flags", "{n} flag conflict(s) to merge"),
    ("restore", "{n} locally-trashed message(s) to restore"),
    ("reupload", "{n} message(s) to re-upload"),
    ("purge", "{n} local-only row(s) to drop"),
)


def _format_counts(counts: dict[str, int]) -> list[str]:
    return [
        template.format(n=counts[key]) for key, template in _LABELS if counts.get(key)
    ]


def _plan_summary(plan: SyncPlan) -> str:
    """One-line summary of total operations across all folders."""
    totals: dict[str, int] = {}
    for acct in plan.accounts:
        for folder in acct.folders:
            for key, n in _categorize(folder.ops).items():
                totals[key] = totals.get(key, 0) + n
    parts = _format_counts(totals)
    return ", ".join(parts)


def _plan_detail(plan: SyncPlan) -> str:
    """Multi-line detail: per-account, per-folder human counts."""
    lines: list[str] = []
    for acct in plan.accounts:
        header = f"  {acct.account_name}"
        if acct.skipped_folders:
            header += f"  (skipped: {', '.join(acct.skipped_folders)})"
        lines.append(header)
        if acct.creates:
            lines.append(f"    create folder(s): {', '.join(acct.creates)}")
        for folder in acct.folders:
            counts = _categorize(folder.ops)
            parts = _format_counts(counts)
            confirm = " [needs confirmation]" if folder.needs_confirmation else ""
            if parts:
                lines.append(f"    {folder.folder_name}: " + ", ".join(parts) + confirm)
            elif folder.is_new:
                lines.append(f"    {folder.folder_name}: new folder")
    return "\n".join(lines) if lines else "  (nothing to do)"


class SyncConfirmScreen(DialogScreen):
    """Presents the sync plan and returns True (proceed) or False (cancel).

    Dismisses with True when the user confirms, False when cancelled.
    """

    CSS = """
    #dialog {
        width: 90%;
        max-height: 80%;
    }

    #summary {
        margin-bottom: 1;
    }

    #detail {
        height: auto;
        max-height: 20;
        overflow-y: auto;
        border: solid $panel;
        padding: 0 1;
        margin-bottom: 1;
    }

    #skipped {
        color: $warning;
        margin-bottom: 1;
    }

    #progress-bar {
        height: auto;
        margin-bottom: 1;
        display: none;
    }
    """

    def __init__(
        self,
        plan: SyncPlan | None = None,
        on_confirm: Callable[[], None] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._plan = plan
        self._syncing = False
        self._planning = plan is None
        self._on_confirm = on_confirm

    @classmethod
    def planning(
        cls,
        on_confirm: Callable[[], None] | None = None,
    ) -> SyncConfirmScreen:
        """Create a screen in planning-progress mode (no plan yet)."""
        return cls(plan=None, on_confirm=on_confirm)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            if self._planning:
                yield Label("Planning sync…", id="title")
                yield Static("Connecting…", id="detail")
                yield ProgressBar(id="progress-bar", total=100, show_eta=False)
            else:
                assert self._plan is not None
                summary = _plan_summary(self._plan)
                yield Label("Sync Plan", id="title")
                if summary:
                    yield Static(summary, id="summary")
                yield Static(
                    _plan_detail(self._plan),
                    id="detail",
                )
                skipped = self._skipped_text()
                if skipped:
                    yield Static(skipped, id="skipped")
                with Horizontal(id="buttons"):
                    yield Button(
                        "Proceed [Y]",
                        id="proceed",
                        variant="success",
                    )
                    yield Button(
                        "Cancel [N]",
                        id="cancel",
                        variant="error",
                    )
        yield Footer()

    def show_plan(self, plan: SyncPlan) -> None:
        """Transition from planning mode to confirm mode."""
        self._plan = plan
        self._planning = False
        self.query_one("#title", Label).update("Sync Plan")
        self.query_one("#detail", Static).update(_plan_detail(plan))
        with contextlib.suppress(Exception):
            self.query_one("#progress-bar", ProgressBar).remove()
        # Add buttons dynamically.
        dialog = self.query_one("#dialog", Vertical)
        summary = _plan_summary(plan)
        if summary:
            dialog.mount(
                Static(summary, id="summary"),
                before=self.query_one("#detail"),
            )
        buttons = Horizontal(
            Button("Proceed [Y]", id="proceed", variant="success"),
            Button("Cancel [N]", id="cancel", variant="error"),
            id="buttons",
        )
        dialog.mount(buttons)

    def _skipped_text(self) -> str:
        if self._plan is None:
            return ""
        lines: list[str] = []
        for acct in self._plan.accounts:
            if acct.skipped_folders:
                skipped = ", ".join(acct.skipped_folders)
                lines.append(f"Skipped in {acct.account_name}: {skipped}")
        return "\n".join(lines)

    def update_status(self, msg: str) -> None:
        """Replace the detail area with a progress message."""
        with contextlib.suppress(Exception):
            self.query_one("#detail", Static).update(msg)

    def update_progress(self, info: ProgressInfo) -> None:
        """Update the detail area and progress bar from a ProgressInfo."""
        with contextlib.suppress(Exception):
            self.query_one("#detail", Static).update(info.message)
        if info.total > 0:
            with contextlib.suppress(Exception):
                bar = self.query_one("#progress-bar", ProgressBar)
                bar.display = True
                bar.update(total=info.total, progress=info.current)
        else:
            with contextlib.suppress(Exception):
                self.query_one("#progress-bar", ProgressBar).display = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._syncing or self._planning:
            return
        if event.button.id == "proceed":
            self._enter_syncing()
        else:
            self.dismiss(False)

    def on_key(self, event: object) -> None:
        from textual.events import Key

        if not isinstance(event, Key):
            return
        if self._syncing or self._planning:
            return
        if event.key in ("y", "enter"):
            self._enter_syncing()
        elif event.key in ("n", "escape", "q"):
            self.dismiss(False)

    def _enter_syncing(self) -> None:
        self._syncing = True
        self.query_one("#title", Label).update("Syncing…")
        self.query_one("#proceed", Button).display = False
        self.query_one("#cancel", Button).display = False
        self.query_one("#detail", Static).update("Starting…")
        if self._on_confirm is not None:
            self._on_confirm()
