"""Contact edit screen: form with all contact fields."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, TextArea

from pony.tui.calendar.reporting import report

from ...contact_actions import ContactEdit, save_contact
from ...domain import Contact
from ...protocols import ContactRepository


class ContactEditScreen(Screen[Contact | None]):
    """Edit all fields of a single contact."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    CSS = """
    ContactEditScreen {
        layout: vertical;
    }

    #edit-form {
        height: 1fr;
        padding: 1 2;
    }

    .field-label {
        margin-top: 1;
        color: $accent;
    }

    .field-input {
        height: 3;
    }

    #notes-area {
        height: 6;
    }
    """

    def __init__(
        self,
        contact: Contact,
        contacts: ContactRepository,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._contact = contact
        self._contacts = contacts

    def compose(self) -> ComposeResult:
        c = self._contact
        yield Header()
        with VerticalScroll(id="edit-form"):
            yield Label("First name", classes="field-label")
            yield Input(c.first_name, id="first-name", classes="field-input")
            yield Label("Last name", classes="field-label")
            yield Input(c.last_name, id="last-name", classes="field-input")
            yield Label("Affix (comma-separated: Dr., Jr.)", classes="field-label")
            yield Input(
                ", ".join(c.affix),
                id="affix",
                classes="field-input",
            )
            yield Label("Organization", classes="field-label")
            yield Input(c.organization, id="organization", classes="field-input")
            yield Label(
                "Email addresses (comma-separated)",
                classes="field-label",
            )
            yield Input(
                ", ".join(c.emails),
                id="emails",
                classes="field-input",
            )
            yield Label(
                "Aliases / nicknames (comma-separated)",
                classes="field-label",
            )
            yield Input(
                ", ".join(c.aliases),
                id="aliases",
                classes="field-input",
            )
            yield Label("Notes", classes="field-label")
            yield TextArea(c.notes, id="notes-area")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#first-name", Input).focus()

    def action_save(self) -> None:
        edit = ContactEdit(
            first_name=self.query_one("#first-name", Input).value,
            last_name=self.query_one("#last-name", Input).value,
            affix=self.query_one("#affix", Input).value,
            organization=self.query_one("#organization", Input).value,
            emails=self.query_one("#emails", Input).value,
            aliases=self.query_one("#aliases", Input).value,
            notes=self.query_one("#notes-area", TextArea).text,
        )
        saved, outcome = save_contact(self._contacts, self._contact, edit)
        if saved is None:
            report(self, outcome)
            return
        self.dismiss(saved)

    def action_cancel(self) -> None:
        self.dismiss(None)
