"""Modal dialog for selecting which parts of a message to save to disk."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Footer, Input, Label

from ..message_renderer import RenderedMessage
from .dialog_screen import DialogScreen

# Maximum length (chars) for the subject slug in a proposed filename.
_SLUG_MAX = 50

# Characters that are valid in cross-platform filenames.
_SAFE_FILENAME_RE = re.compile(r"[^\w\-.]")


def _sanitize_attachment_filename(filename: str) -> str:
    """Strip path components and dangerous chars from an untrusted filename."""
    # Drop any directory component supplied by the sender.
    name = Path(filename).name
    # Belt-and-suspenders: replace separators that survive cross-platform extraction.
    name = name.replace("/", "_").replace("\\", "_")
    # Drop control characters (NUL, etc.).
    name = re.sub(r"[\x00-\x1f]", "", name)
    # Avoid bare dot-only names that act as directory references.
    if name in ("", ".", ".."):
        name = "attachment"
    return name[:255]


def _subject_slug(subject: str) -> str:
    """Convert a subject line into a safe filename component."""
    slug = subject.lower().replace(" ", "-")
    slug = _SAFE_FILENAME_RE.sub("", slug)
    # Collapse multi-dot sequences so `..` cannot act as a parent-dir reference.
    slug = re.sub(r"\.{2,}", ".", slug)
    return slug[:_SLUG_MAX] or "message"


def _proposed_body_filename(rendered: RenderedMessage) -> str:
    """Build the default .md filename from the message date and subject."""
    date_prefix = ""
    if rendered.date:
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(rendered.date)
            date_prefix = dt.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            pass
    slug = _subject_slug(rendered.subject) if rendered.subject else "message"
    if date_prefix:
        return f"{date_prefix}_{slug}.md"
    return f"{slug}.md"


def _proposed_attachment_filename(filename: str, index: int) -> str:
    """Return a sanitized attachment filename, or a numeric fallback."""
    safe = _sanitize_attachment_filename(filename) if filename else ""
    return safe if safe else f"attachment-{index}"


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
                        _proposed_body_filename(self._rendered),
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
                            _proposed_attachment_filename(att.filename, att.index),
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
