from __future__ import annotations

from datetime import date

from textual.widgets import Static

from chronos.domain import StoredComponent
from chronos.views import NOTES_LABEL, DetailField, event_detail_fields


class EventView(Static):
    """Read-only renderer for one VEvent or VTodo.

    What the pane says is `views.event_detail_fields`, a pure function.
    How wide the labels are is this widget's: the colons line up
    because the pane is monospace, which is a fact about a terminal and
    not about an event.
    """

    def show(self, component: StoredComponent | None, *, today: date) -> None:
        if component is None:
            self.update("(no event selected)")
            return
        self.update(render_fields(event_detail_fields(component, today)))


def render_fields(fields: tuple[DetailField, ...]) -> str:
    """The detail fields as lines, labels right-aligned to the widest.

    Padding to the longest label present rather than to a constant: the
    constant was ``len("Reminders:")``, which left every event without
    reminders indented for a label it does not have.  Notes come last
    and as a paragraph under their own heading, because a description
    is not a field.
    """
    notes = [field for field in fields if field.label == NOTES_LABEL]
    labelled = [field for field in fields if field.label != NOTES_LABEL]
    width = max((len(field.label) + 1 for field in labelled), default=0)
    lines = [f"{field.label + ':':>{width}} {field.value}" for field in labelled]
    for field in notes:
        lines.extend(["", field.label, field.value])
    return "\n".join(lines)


__all__ = ["EventView", "render_fields"]
