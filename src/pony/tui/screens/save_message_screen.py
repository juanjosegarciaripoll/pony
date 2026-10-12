"""Modal dialog for selecting which parts of a message to save to disk."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Footer, Input, Label

from ...message_renderer import (
    RenderedMessage,
    proposed_attachment_filename,
    proposed_filename,
)
from .dialog_screen import DialogScreen


@dataclass(frozen=True)
class SaveItem:
    """One user-confirmed item to write to disk.

    ``kind`` is ``"body"`` for the Markdown email body or
    ``"attachment:N"`` (1-based) for attachment *N*.
    ``filename`` is the user-edited destination filename.
    """

    kind: str
    filename: str


class SaveMessageScreen(DialogScreen[list[SaveItem] | None]):
    """Checklist modal: pick the email body and/or attachments to save.

    Dismissed with ``None`` on Cancel, or a (possibly empty) list of
    :class:`SaveItem` on Save.  The caller pushes a folder-picker next
    and writes the selected items to the chosen directory.
    """

    INHERIT_BINDINGS = False
    DEFAULT_BUTTON_ID = "save"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    CSS = """
    #dialog {
        width: 70;
    }

    #items {
        height: auto;
        margin-bottom: 1;
    }

    .save-row {
        height: 1;
        align: left middle;
        margin-bottom: 1;
    }

    .save-row Checkbox {
        width: auto;
        height: 1;
        border: none;
        padding: 0;
        background: transparent;
    }

    .save-row Input {
        width: 1fr;
        height: 1;
        border: none;
        background: $boost;
        margin-left: 1;
    }
    """

    def __init__(self, rendered: RenderedMessage) -> None:
        super().__init__()
        self._rendered = rendered

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Save message", id="title")
            with Vertical(id="items"):
                yield Horizontal(
                    Checkbox("", id="check-body", value=True),
                    Input(
                        proposed_filename(self._rendered, suffix=".md"),
                        id="name-body",
                        placeholder="filename.md",
                    ),
                    classes="save-row",
                )
                for att in self._rendered.attachments:
                    slug = f"att-{att.index}"
                    yield Horizontal(
                        Checkbox("", id=f"check-{slug}", value=True),
                        Input(
                            proposed_attachment_filename(att.filename, att.index),
                            id=f"name-{slug}",
                            placeholder=f"attachment-{att.index}",
                        ),
                        classes="save-row",
                    )
            with Horizontal(id="buttons"):
                yield Button("Save", id="save", variant="success")
                yield Button("Cancel", id="cancel")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "save":
            items: list[SaveItem] = []
            if self.query_one("#check-body", Checkbox).value:
                filename = self.query_one("#name-body", Input).value.strip()
                if filename:
                    items.append(SaveItem(kind="body", filename=filename))
            for att in self._rendered.attachments:
                slug = f"att-{att.index}"
                if self.query_one(f"#check-{slug}", Checkbox).value:
                    filename = self.query_one(f"#name-{slug}", Input).value.strip()
                    if filename:
                        items.append(
                            SaveItem(kind=f"attachment:{att.index}", filename=filename)
                        )
            self.dismiss(items)

    def action_cancel(self) -> None:
        self.dismiss(None)
