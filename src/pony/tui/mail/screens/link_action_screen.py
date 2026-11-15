"""Dialog for acting on an inline message link (Open / Compose / Copy / Cancel)."""

from __future__ import annotations

import webbrowser

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Static

from .dialog_screen import DialogScreen


class LinkActionScreen(DialogScreen):
    """Modal shown when the user clicks a link in a message body.

    A web link offers to open it in a browser; a ``mailto:`` address
    offers to write to it, and answers with ``True`` so the screen that
    owns the composer opens one -- composing needs the accounts and the
    configuration, which a dialog has no business reaching for.

    Both kinds show the target before anything happens.  The words an
    anchor displays and the place it goes are each the sender's to
    choose and need not agree -- ``<a href="mailto:billing@elsewhere.test"
    >accounts@yourbank.test</a>`` reads as one address and would write to
    another -- so the dialog is where the reader finds out which. For an
    address it is also the step that keeps a stray click from replacing
    the message being read with a composer.
    """

    # `c` composes and `y` copies, as they do in the reader behind this
    # dialog -- `c` is its compose key and `Y` its copy key.  Copy was `c`
    # here while nothing in the dialog composed; now that something does,
    # the reader's own two letters are the ones that will be guessed.
    # `w` would have been the obvious letter for "write" and is not
    # available: the reader spends it on opening a message in a browser,
    # and `tests/test_keymap.py` refuses a second meaning for it.
    BINDINGS = [
        Binding("o", "open_link", "Open", show=False),
        Binding("c", "compose_to_link", "Compose", show=False),
        Binding("y", "copy_link", "Copy", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    CSS = """
    #dialog {
        width: 70;
    }

    #body {
        margin-bottom: 1;
        overflow: hidden auto;
        max-height: 5;
    }
    """

    def __init__(self, target: str, *, kind: str = "web", **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._url = target
        self._is_web = kind != "mail"
        self.DEFAULT_BUTTON_ID = "open" if self._is_web else "compose"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Link" if self._is_web else "Address", id="title")
            yield Static(self._url, id="body")
            with Horizontal(id="buttons"):
                if self._is_web:
                    yield Button("Open [O]", id="open", variant="success")
                else:
                    yield Button("Compose [C]", id="compose", variant="success")
                yield Button("Copy [Y]", id="copy")
                yield Button("Cancel [Esc]", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open":
            self.action_open_link()
        elif event.button.id == "compose":
            self.action_compose_to_link()
        elif event.button.id == "copy":
            self.action_copy_link()
        else:
            self.dismiss(False)

    def action_open_link(self) -> None:
        if not self._is_web:
            return
        try:
            webbrowser.open(self._url)
        except OSError as exc:
            self.notify(str(exc), severity="error")
            return
        self.dismiss(False)

    def action_compose_to_link(self) -> None:
        """Answer that the address is to be written to.

        The caller composes: see `MainScreen.action_compose_link`.
        """
        if self._is_web:
            return
        self.dismiss(True)

    def action_copy_link(self) -> None:
        self.app.copy_to_clipboard(self._url)  # pyright: ignore[reportUnknownMemberType]
        self.notify("Address copied" if not self._is_web else "Link copied")
        self.dismiss(False)
