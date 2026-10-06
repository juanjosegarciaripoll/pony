from __future__ import annotations

from collections.abc import Callable
from datetime import date

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Footer

from chronos.domain import StoredComponent
from chronos.tui.bindings import detail_bindings
from chronos.tui.widgets.event_view import EventView


class EventDetailScreen(ModalScreen[None]):
    """Read-only modal showing one component's details.

    The body is a `VerticalScroll`, not a plain `Vertical`, because an
    invitation's notes can run to several pages — the minutes of a
    meeting, a wall of conferencing boilerplate — and a plain container
    clipped them at the dialog's height with no scrollbar, no key and no
    wheel that would reach the rest. It takes focus on mount so the
    scroll keys work without a click, and the screen's own `escape` / `e`
    still reach the screen because the scroll container does not bind
    them.
    """

    BINDINGS = detail_bindings()

    def __init__(
        self,
        component: StoredComponent,
        *,
        today: date,
        on_edit: Callable[[StoredComponent], None],
    ) -> None:
        super().__init__()
        self._component = component
        self._today = today
        self._on_edit = on_edit

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="event-detail", classes="dialog-box"):
            view = EventView()
            yield view
        yield Footer()

    def on_mount(self) -> None:
        view: EventView = self.query_one(EventView)
        view.show(self._component, today=self._today)
        self.query_one("#event-detail", VerticalScroll).focus()

    def on_click(self, event: Click) -> None:
        if event.widget is self:
            self.action_close()

    def action_close(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def action_edit(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
        self._on_edit(self._component)


__all__ = ["EventDetailScreen"]
