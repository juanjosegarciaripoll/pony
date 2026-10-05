"""Answer a meeting invitation that arrived in the mail."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Select

from chronos.domain import CalendarRef

from ...invitation import (
    PARTSTAT_ACCEPTED,
    PARTSTAT_DECLINED,
    PARTSTAT_TENTATIVE,
    Invitation,
)
from .dialog_screen import DialogScreen


@dataclass(frozen=True, slots=True)
class InvitationChoice:
    """What the user decided about an invitation.

    ``partstat`` is None for "file it but tell nobody", which is the
    honest option when the message announces an event rather than asking
    about one, or when the user does not want to answer yet.
    """

    calendar: CalendarRef
    partstat: str | None


class InvitationScreen(DialogScreen[InvitationChoice | None]):
    """Accept, decline or merely file an invitation.

    The dialog settles two things at once — which calendar the event goes
    in, and what the organizer is told — because they are one decision
    for the user even though they are two operations underneath.

    Dismisses with the :class:`InvitationChoice`, or None when the user
    backs out or there is nowhere to file it.
    """

    DEFAULT_BUTTON_ID = "invitation-accept"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("a", "respond('ACCEPTED')", "Accept", show=False),
        Binding("t", "respond('TENTATIVE')", "Tentative", show=False),
        Binding("d", "respond('DECLINED')", "Decline", show=False),
    ]

    DEFAULT_CSS = """
    InvitationScreen #dialog {
        width: 70;
    }

    InvitationScreen .invitation-detail {
        color: $text-muted;
    }

    InvitationScreen #invitation-calendar {
        margin-top: 1;
        margin-bottom: 1;
    }
    """

    def __init__(
        self,
        invitation: Invitation,
        calendars: Sequence[CalendarRef],
        *,
        can_reply: bool = True,
    ) -> None:
        super().__init__()
        self._invitation = invitation
        self._calendars = tuple(calendars)
        self._can_reply = can_reply
        if not can_reply:
            self.DEFAULT_BUTTON_ID = "invitation-add"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            lines = self._invitation.describe()
            yield Label(lines[0], id="title")
            for line in lines[1:]:
                yield Label(line, classes="invitation-detail")
            if self._calendars:
                yield Select(
                    ((self._label(c), c) for c in self._calendars),
                    value=self._calendars[0],
                    allow_blank=False,
                    id="invitation-calendar",
                )
            else:
                yield Label("No calendar to file this in.", classes="invitation-detail")
            with Horizontal(id="buttons"):
                if self._can_reply:
                    yield Button("accept", id="invitation-accept")
                    yield Button("tentative", id="invitation-tentative")
                    yield Button("decline", id="invitation-decline")
                yield Button("add only", id="invitation-add")
                yield Button("cancel", id="invitation-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        answers = {
            "invitation-accept": PARTSTAT_ACCEPTED,
            "invitation-tentative": PARTSTAT_TENTATIVE,
            "invitation-decline": PARTSTAT_DECLINED,
        }
        if event.button.id in answers:
            self._decide(answers[event.button.id])
            return
        if event.button.id == "invitation-add":
            self._decide(None)
            return
        self.action_cancel()

    def action_respond(self, partstat: str) -> None:
        """Keyboard shortcut for the three answers."""
        if not self._can_reply:
            return
        self._decide(partstat)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _decide(self, partstat: str | None) -> None:
        if not self._calendars:
            self.dismiss(None)
            return
        self.dismiss(
            InvitationChoice(calendar=self._selected_calendar(), partstat=partstat)
        )

    def _selected_calendar(self) -> CalendarRef:
        select = self.query_one("#invitation-calendar", Select)
        value = cast("object", select.value)
        if not isinstance(value, CalendarRef):
            return self._calendars[0]
        return value

    @staticmethod
    def _label(ref: CalendarRef) -> str:
        return f"{ref.account_name} / {ref.calendar_name}"


__all__ = ["InvitationChoice", "InvitationScreen"]
