"""Contact detail screen: read-only view of all contact fields."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from ...domain import Contact
from ...protocols import ContactRepository


class ContactDetailScreen(Screen[bool]):
    """Read-only display of a single contact.  Dismisses with True if edited."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", show=False),
        Binding("e", "edit", "Edit"),
    ]

    CSS = """
    ContactDetailScreen {
        layout: vertical;
    }

    #detail-scroll {
        height: 1fr;
        padding: 1 2;
    }

    .detail-label {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }

    .detail-value {
        margin-left: 2;
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
        self._edited = False

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="detail-scroll"):
            yield from self._render_fields()
        yield Footer()

    def _render_fields(self) -> list[Static]:
        c = self._contact
        widgets: list[Static] = []

        name = c.display_name or "(no name)"
        widgets.append(Static(f"[bold]{name}[/bold]", classes="detail-value"))

        if c.affix:
            widgets.append(Static("Affix", classes="detail-label"))
            widgets.append(Static(", ".join(c.affix), classes="detail-value"))

        if c.aliases:
            widgets.append(Static("Aliases", classes="detail-label"))
            widgets.append(
                Static(", ".join(c.aliases), classes="detail-value"),
            )

        if c.organization:
            widgets.append(Static("Organization", classes="detail-label"))
            widgets.append(Static(c.organization, classes="detail-value"))

        widgets.append(Static("Email addresses", classes="detail-label"))
        if c.emails:
            for email in c.emails:
                widgets.append(Static(f"  {email}", classes="detail-value"))
        else:
            widgets.append(Static("  (none)", classes="detail-value"))

        widgets.append(Static("Statistics", classes="detail-label"))
        widgets.append(
            Static(
                f"  {c.message_count} message(s)",
                classes="detail-value",
            ),
        )
        if c.last_seen:
            widgets.append(
                Static(
                    f"  Last seen: {c.last_seen:%Y-%m-%d %H:%M}",
                    classes="detail-value",
                ),
            )

        if c.notes:
            widgets.append(Static("Notes", classes="detail-label"))
            widgets.append(Static(c.notes, classes="detail-value"))

        widgets.append(Static("Timestamps", classes="detail-label"))
        widgets.append(
            Static(
                f"  Created: {c.created_at:%Y-%m-%d %H:%M}",
                classes="detail-value",
            ),
        )
        widgets.append(
            Static(
                f"  Updated: {c.updated_at:%Y-%m-%d %H:%M}",
                classes="detail-value",
            ),
        )

        return widgets

    def action_edit(self) -> None:
        from .contact_edit_screen import ContactEditScreen

        def _on_saved(updated: Contact | None) -> None:
            if updated is not None:
                self._contact = updated
                self._edited = True
                self._refresh_detail()

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ContactEditScreen(self._contact, self._contacts), _on_saved,
        )

    def _refresh_detail(self) -> None:
        scroll = self.query_one("#detail-scroll", VerticalScroll)
        scroll.remove_children()
        for widget in self._render_fields():
            scroll.mount(widget)

    def action_close(self) -> None:
        self.dismiss(self._edited)
