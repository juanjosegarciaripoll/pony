from __future__ import annotations

from collections.abc import Callable, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from chronos.domain import AccountConfig


class SyncConfirmScreen(ModalScreen[None]):
    """Modal dialog asking the user to confirm a sync run.

    Keyboard accelerators (`y` / `n` / `escape`) and on-screen
    `Button` widgets reach the same actions, so users can drive the
    dialog from either input method. The screen subclasses
    `ModalScreen` so it overlays `MainScreen` instead of replacing it
    — pressing Esc or clicking Cancel returns the user to whatever
    they were doing without stealing focus management.
    """

    BINDINGS = [
        Binding("y", "confirm", "Sync"),
        Binding("n", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        accounts: Sequence[AccountConfig],
        on_confirm: Callable[[], None],
        *,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        """`on_cancel` runs when the dialog is dismissed without syncing.

        The caller claims the periodic sync's slot before putting this
        dialog up, so it needs telling when the user changes their mind
        and the slot has to go back.
        """
        super().__init__()
        self._accounts = tuple(accounts)
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel

    def compose(self) -> ComposeResult:
        with Vertical(id="sync-confirm-box", classes="dialog-box"):
            yield Label("Sync the following accounts?", classes="dialog-title")
            if not self._accounts:
                yield Label("(no accounts configured)", classes="dialog-empty")
            else:
                for account in self._accounts:
                    yield Label(f"  • {account.name} ({account.url})")
            with Horizontal(classes="dialog-actions"):
                yield Button("Sync", id="sync-confirm-yes", variant="primary")
                yield Button("Cancel", id="sync-confirm-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sync-confirm-yes":
            self.action_confirm()
        else:
            self.action_cancel()

    def action_confirm(self) -> None:
        # Pop first, then trigger sync. The runner spawns a worker
        # that posts notifications back to whatever screen is active;
        # leaving this dialog on the stack would route them here.
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
        self._on_confirm()

    def action_cancel(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
        if self._on_cancel is not None:
            self._on_cancel()


__all__ = ["SyncConfirmScreen"]
