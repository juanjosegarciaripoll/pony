"""Contact edit screen: form with all contact fields."""

from __future__ import annotations

import dataclasses

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, TextArea

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
        first = self.query_one("#first-name", Input).value.strip()
        last = self.query_one("#last-name", Input).value.strip()
        affix_raw = self.query_one("#affix", Input).value
        affix = tuple(s.strip() for s in affix_raw.split(",") if s.strip())
        org = self.query_one("#organization", Input).value.strip()
        emails_raw = self.query_one("#emails", Input).value
        emails = tuple(s.strip().lower() for s in emails_raw.split(",") if s.strip())
        aliases_raw = self.query_one("#aliases", Input).value
        aliases = tuple(s.strip() for s in aliases_raw.split(",") if s.strip())
        notes = self.query_one("#notes-area", TextArea).text.strip()

        updated = dataclasses.replace(
            self._contact,
            first_name=first,
            last_name=last,
            affix=affix,
            organization=org,
            emails=emails,
            aliases=aliases,
            notes=notes,
        )
        saved = self._contacts.upsert_contact(contact=updated)
        self.dismiss(saved)

    def action_cancel(self) -> None:
        self.dismiss(None)
