"""Sync confirmation screen: show plan, ask user to proceed or cancel."""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, Footer, Label, ProgressBar, Static

from ...sync import (
    ProgressInfo,
    SyncPlan,
    describe_halt,
    format_plan_detail,
    format_plan_summary,
)
from .dialog_screen import DialogScreen


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

    #confirm-warning {
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
                summary = format_plan_summary(self._plan)
                yield Label("Sync Plan", id="title")
                if summary:
                    yield Static(summary, id="summary")
                yield Static(
                    format_plan_detail(self._plan),
                    id="detail",
                )
                skipped = self._skipped_text()
                if skipped:
                    yield Static(skipped, id="skipped")
                warning = self._confirm_warning_text()
                if warning:
                    yield Static(warning, id="confirm-warning")
                with Horizontal(id="buttons"):
                    yield Button(
                        self._proceed_label(),
                        id="proceed",
                        variant="success",
                    )
                    yield Button("Cancel [N]", id="cancel")
        yield Footer()

    def show_plan(self, plan: SyncPlan) -> None:
        """Transition from planning mode to confirm mode."""
        self._plan = plan
        self._planning = False
        self.query_one("#title", Label).update("Sync Plan")
        self.query_one("#detail", Static).update(format_plan_detail(plan))
        with contextlib.suppress(Exception):
            self.query_one("#progress-bar", ProgressBar).remove()
        # Add buttons dynamically.
        dialog = self.query_one("#dialog", Vertical)
        summary = format_plan_summary(plan)
        if summary:
            dialog.mount(
                Static(summary, id="summary"),
                before=self.query_one("#detail"),
            )
        warning = self._confirm_warning_text()
        if warning:
            dialog.mount(Static(warning, id="confirm-warning"))
        buttons = Horizontal(
            Button(self._proceed_label(), id="proceed", variant="success"),
            Button("Cancel [N]", id="cancel", variant="error"),
            id="buttons",
        )
        dialog.mount(buttons)
        # After the refresh, not now.  `mount` is asynchronous, so these
        # Buttons are still only constructor children of a `Horizontal`
        # that is not in the DOM yet, and `query_one` raised `NoMatches`
        # every single time -- out of `MainScreen.on_worker_state_changed`
        # and into the application's exception handler, which stopped the
        # reader on any non-empty plan.
        self.call_after_refresh(self._focus_proceed)

    def _focus_proceed(self) -> None:
        # The screen can be dismissed between the mount and the refresh,
        # and then there is nothing to focus and nothing wrong.
        with contextlib.suppress(NoMatches):
            self.query_one("#proceed", Button).focus()

    def _confirm_warning_text(self) -> str:
        """Why the plan is held, in the words both front ends use.

        `describe_halt` is the one description of a halt: it names the
        account and folder, and says whether the folder would delete or
        *upload*. This screen used to write its own, which said
        deletions and percentages whatever the halt was -- so a folder
        held for a mass upload, which is every folder of a freshly
        reindexed mirror, was described to the terminal user as a
        deletion of mail.
        """
        if self._plan is None:
            return ""
        reason = describe_halt(self._plan)
        if not reason:
            return ""
        return (
            f"WARNING: {reason} Review the lines marked [CONFIRM: ...]"
            " above. Pressing Y applies the plan, those folders"
            " included."
        )

    def _proceed_label(self) -> str:
        if self._plan is not None and self._plan.folders_needing_confirmation():
            return "Proceed (incl. flagged) [Y]"
        return "Proceed [Y]"

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
        """Proceed, or back out.

        Backing out works while planning too.  It used to return early
        on ``_planning``, which swallowed ``escape``, ``q`` and ``n``
        alike — and if anything had gone wrong with the plan arriving,
        the only key left that did anything was the application's
        ++shift+q++, which quits.  There is nothing to interrupt while a
        plan is being drawn up: the worker's result is simply dropped.

        ``_syncing`` is different, and still refuses: a half-applied
        sync is not something this dialog can abandon.
        """
        from textual.events import Key

        if not isinstance(event, Key):
            return
        if self._syncing:
            return
        if event.key in ("n", "escape", "q"):
            event.stop()
            self.dismiss(False)
            return
        if self._planning:
            return
        if event.key == "y":
            self._enter_syncing()

    def _enter_syncing(self) -> None:
        self._syncing = True
        self.mark_busy("proceed", "Syncing…")
        self.query_one("#title", Label).update("Syncing…")
        self.query_one("#detail", Static).update("Starting…")
        if self._on_confirm is not None:
            self._on_confirm()
