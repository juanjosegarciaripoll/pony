"""Contact browser: searchable list with mark, delete, merge, and edit."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input

from ...domain import Contact
from ...protocols import ContactRepository


class ContactBrowserScreen(Screen[None]):
    """Single-pane scrollable contact list with search and bulk operations."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", show=False),
        Binding("slash", "search", "Search"),
        Binding("s", "search", "Search", show=False),
        Binding("m", "mark_down", "Mark"),
        Binding("shift+down", "mark_down", "Mark ↓", show=False),
        Binding("shift+up", "mark_up", "Mark ↑", show=False),
        Binding("e", "edit", "Edit"),
        Binding("D", "delete_marked", "Delete marked"),
        Binding("M", "merge_marked", "Merge marked"),
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
        if self._filter:
            self._displayed = self._contacts.search_contacts(
                prefix=self._filter, limit=500,
            )
        else:
            self._displayed = self._contacts.list_all_contacts()
        self._displayed.sort(
            key=lambda c: (c.last_name.lower(), c.first_name.lower()),
        )
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        table = self.query_one("#contact-table", DataTable)
        prev_row = table.cursor_row
        prev_scroll_y = table.scroll_y
        table.clear()
        for contact in self._displayed:
            mark = "●" if contact.id in self._marked else " "
            emails = ", ".join(contact.emails[:3])
            if len(contact.emails) > 3:
                emails += f" (+{len(contact.emails) - 3})"
            table.add_row(
                mark,
                contact.last_name,
                contact.first_name,
                emails,
                contact.organization,
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
        self, event: DataTable.RowSelected,
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
            ContactDetailScreen(contact, self._contacts), _on_dismiss,
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
    # Edit (direct, without detail view)
    # ------------------------------------------------------------------

    def action_edit(self) -> None:
        contact = self._selected_contact()
        if contact is None:
            return
        from .contact_edit_screen import ContactEditScreen

        def _on_saved(updated: Contact | None) -> None:
            if updated is not None:
                self._refresh_list()

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ContactEditScreen(contact, self._contacts), _on_saved,
        )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def action_delete_marked(self) -> None:
        from .confirm_screen import ConfirmScreen

        if self._marked:
            ids = list(self._marked)
            names = []
            for c in self._displayed:
                if c.id in self._marked:
                    label = c.display_name or (
                        c.emails[0] if c.emails else "(no name)"
                    )
                    names.append(label)
            body = "\n".join(f"  - {n}" for n in names[:10])
            if len(names) > 10:
                body += f"\n  … and {len(names) - 10} more"

            def _on_confirm(yes: bool | None) -> None:
                if not yes:
                    return
                for cid in ids:
                    self._contacts.delete_contact(contact_id=cid)
                self._marked.clear()
                self._refresh_list()
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Deleted {len(ids)} contact(s).",
                )

            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                ConfirmScreen(
                    f"Delete {len(ids)} contact(s)?", body,
                ),
                _on_confirm,
            )
        else:
            # No marks — delete the selected contact.
            contact = self._selected_contact()
            if contact is None or contact.id is None:
                return
            cid = contact.id
            name = contact.display_name or "(no name)"

            def _on_confirm_single(yes: bool | None) -> None:
                if not yes:
                    return
                self._contacts.delete_contact(contact_id=cid)
                self._refresh_list()
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Deleted {name}.",
                )

            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                ConfirmScreen(
                    "Delete contact?",
                    f"  {name}",
                ),
                _on_confirm_single,
            )

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def action_merge_marked(self) -> None:
        if len(self._marked) < 2:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Mark at least 2 contacts to merge.",
                severity="warning",
            )
            return
        ids = sorted(self._marked)
        target = ids[0]
        sources = ids[1:]
        self._contacts.merge_contacts(
            target_id=target, source_ids=sources,
        )
        self._marked.clear()
        self._refresh_list()
        self.app.notify(  # pyright: ignore[reportUnknownMemberType]
            f"Merged {len(sources) + 1} contacts.",
        )

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)
