"""Contact browser: searchable list with mark, delete, merge, and edit."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input

from pony.calendar.outcome import Notice, Outcome
from pony.tui.calendar.reporting import report

from ...contact_actions import (
    MergePlan,
    delete_contacts,
    list_contacts,
    merge_contacts,
    plan_merge,
    summarize,
)
from ...domain import Contact
from ...keymap import MAIL_KEYMAP, Surface, for_surface
from ...protocols import ContactRepository
from ...views import contact_row
from ..bindings import MARK_BINDINGS, to_textual

_BLANK_CONTACT = Contact(id=None, first_name="", last_name="", emails=())


class ContactBrowserScreen(Screen[None]):
    """Single-pane scrollable contact list with search and bulk operations."""

    BINDINGS = [
        *to_textual(for_surface(MAIL_KEYMAP, Surface.CONTACTS)),
        # The marking vocabulary is the message list's; the cursor keys
        # are the table's own.
        *MARK_BINDINGS,
    ]

    CSS = """
    ContactBrowserScreen {
        layout: vertical;
    }

    #contact-search {
        dock: top;
        height: 3;
        display: none;
    }

    #contact-table {
        height: 1fr;
    }
    """

    def __init__(self, contacts: ContactRepository, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._contacts = contacts
        self._all_contacts: list[Contact] = []
        self._displayed: list[Contact] = []
        self._marked: set[int] = set()  # contact ids
        self._filter: str = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(
            placeholder="Filter contacts…",
            id="contact-search",
        )
        yield DataTable(id="contact-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#contact-table", DataTable)
        table.add_columns(" ", "Last name", "First name", "Email(s)", "Org")
        self._refresh_list()
        table.focus()

    def _refresh_list(self) -> None:
        self._displayed = list_contacts(self._contacts, query=self._filter)
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        table = self.query_one("#contact-table", DataTable)
        prev_row = table.cursor_row
        prev_scroll_y = table.scroll_y
        table.clear()
        for contact in self._displayed:
            row = contact_row(contact, marked=contact.id in self._marked)
            table.add_row(
                # The projection already decided the mark; inventing a
                # second one here is how the two front ends came to show
                # different glyphs for the same row.
                row.mark,
                row.last_name,
                row.first_name,
                row.emails,
                row.organization,
                key=str(contact.id),
            )
        if prev_row >= 0 and self._displayed:
            table.move_cursor(
                row=min(prev_row, len(self._displayed) - 1),
            )
        # Restore scroll position so the viewport doesn't jump.
        # Clamp to the new maximum in case rows were deleted.
        max_y = table.max_scroll_y
        table.scroll_to(y=min(prev_scroll_y, max_y), animate=False)

    def _selected_contact(self) -> Contact | None:
        table = self.query_one("#contact-table", DataTable)
        row_key = table.cursor_row
        if row_key < 0 or row_key >= len(self._displayed):
            return None
        return self._displayed[row_key]

    # ------------------------------------------------------------------
    # Detail view (Enter via DataTable row selection)
    # ------------------------------------------------------------------

    def on_data_table_row_selected(
        self,
        event: DataTable.RowSelected,
    ) -> None:
        """Show the detail screen when the user presses Enter on a row."""
        event.stop()
        contact = self._selected_contact()
        if contact is None:
            return
        from .contact_detail_screen import ContactDetailScreen

        def _on_dismiss(edited: bool | None) -> None:
            if edited:
                self._refresh_list()

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ContactDetailScreen(contact, self._contacts),
            _on_dismiss,
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def action_search(self) -> None:
        search = self.query_one("#contact-search", Input)
        search.display = True
        search.value = self._filter
        search.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._filter = event.value.strip()
        search = self.query_one("#contact-search", Input)
        search.display = False
        self._refresh_list()
        self.query_one("#contact-table", DataTable).focus()

    # ------------------------------------------------------------------
    # Mark / unmark
    # ------------------------------------------------------------------

    def _toggle_mark_current(self) -> None:
        from textual.coordinate import Coordinate

        table = self.query_one("#contact-table", DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(self._displayed):
            return
        contact = self._displayed[row]
        if contact.id is None:
            return
        if contact.id in self._marked:
            self._marked.discard(contact.id)
            mark = " "
        else:
            self._marked.add(contact.id)
            mark = "●"
        table.update_cell_at(Coordinate(row, 0), mark)

    def action_mark_down(self) -> None:
        self._toggle_mark_current()
        table = self.query_one("#contact-table", DataTable)
        table.action_cursor_down()

    def action_mark_up(self) -> None:
        self._toggle_mark_current()
        table = self.query_one("#contact-table", DataTable)
        table.action_cursor_up()

    # ------------------------------------------------------------------
    # New / Edit
    # ------------------------------------------------------------------

    def action_new_contact(self) -> None:
        from .contact_edit_screen import ContactEditScreen

        def _on_saved(saved: Contact | None) -> None:
            if saved is not None:
                self._refresh_list()

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ContactEditScreen(_BLANK_CONTACT, self._contacts),
            _on_saved,
        )

    def action_edit(self) -> None:
        contact = self._selected_contact()
        if contact is None:
            return
        from .contact_edit_screen import ContactEditScreen

        def _on_saved(updated: Contact | None) -> None:
            if updated is not None:
                self._refresh_list()

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ContactEditScreen(contact, self._contacts),
            _on_saved,
        )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def _report(self, outcome: Outcome) -> None:
        """Say everything an operation had to say."""
        report(self.app, outcome)

    def action_delete_marked(self) -> None:
        """Delete the marked contacts, or the cursor row when none are."""
        from .confirm_screen import ConfirmScreen

        if self._marked:
            ids = sorted(self._marked)
            labels = [
                contact.display_name
                or (contact.emails[0] if contact.emails else "(no name)")
                for contact in self._displayed
                if contact.id in self._marked
            ]
            prompt = f"Delete {len(ids)} contact(s)?"
            body = summarize(labels)
        else:
            contact = self._selected_contact()
            if contact is None or contact.id is None:
                return
            ids = [contact.id]
            prompt = "Delete contact?"
            body = f"  {contact.display_name or '(no name)'}"

        def _on_confirm(yes: bool | None) -> None:
            if not yes:
                return
            outcome = delete_contacts(self._contacts, ids)
            self._marked.clear()
            self._refresh_list()
            self._report(outcome)

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ConfirmScreen(prompt, body),
            _on_confirm,
        )

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def action_merge_marked(self) -> None:
        """Fold the marked contacts into the oldest of them.

        Which record survives is decided before the prompt goes up and
        named in it, because merging is irreversible and the marks do not
        convey who wins.
        """
        plan = plan_merge(self._contacts, sorted(self._marked))
        if isinstance(plan, Notice):
            self.app.notify(plan.text, severity=plan.severity)  # pyright: ignore[reportUnknownMemberType]
            return
        from .confirm_screen import ConfirmScreen

        def _on_confirm(yes: bool | None) -> None:
            if not yes:
                return
            self._apply_merge(plan)

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ConfirmScreen(
                f"Merge {plan.total} contacts into one?",
                f"  keeping: {plan.target_label}\n"
                + summarize(plan.source_labels, prefix="  merging in: "),
            ),
            _on_confirm,
        )

    def _apply_merge(self, plan: MergePlan) -> None:
        outcome = merge_contacts(self._contacts, plan)
        self._marked.clear()
        self._refresh_list()
        self._report(outcome)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)
