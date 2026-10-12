"""The contacts browser in a browser.

One list with the same columns the terminal shows, the same marking
vocabulary, and the same four operations over the marks: new, edit,
delete and merge.  Each is a call into :mod:`pony.contact_actions`,
which is also where the form's fields are split and joined, so a
contact edited here and a contact edited in the terminal end up with
the same record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chronos.outcome import Outcome, warn

from ..contact_actions import (
    ContactEdit,
    contact_by_id,
    delete_contacts,
    label_for,
    list_contacts,
    merge_contacts,
    plan_merge,
    save_contact,
)
from ..domain import Contact
from ..views import contact_row
from . import urls
from .actions import held_for_confirmation
from .html import Raw, element, fragment, join
from .layout import PageSurface, Tab, columns, page, pane

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chronos.outcome import Notice

    from ..protocols import ContactRepository
    from .actions import Action, ActionContext, Reply
    from .app import WebServices


def contacts_page(
    services: WebServices,
    *,
    query: str = "",
    notices: Sequence[Notice] = (),
) -> str:
    """The list, filtered by *query* when there is one."""
    contacts = services.contacts
    found = [] if contacts is None else list_contacts(contacts, query=query)
    return page(
        title="Contacts — Pony Express",
        surface=PageSurface.CONTACTS,
        tab=Tab.CONTACTS,
        status=_status(found, query=query, missing=contacts is None),
        panes=pane(
            name="list",
            classes="contacts-pane",
            heading=fragment(
                columns(
                    ("mark", ""),
                    ("last-name", "Last name"),
                    ("first-name", "First name"),
                    ("emails", "Email(s)"),
                    ("org", "Organisation"),
                ),
                _tools(query),
            ),
            body=_rows(found) if contacts is not None else _no_store(),
        ),
        data={"query": query},
        notices=notices,
    )


def _tools(query: str) -> Raw:
    """What the keys do, for a screen with no keyboard.

    The two bulk operations act on the marked rows, which a tap on the
    mark column sets, so a phone can do what ++m++ does.
    """
    return element(
        "span",
        element(
            "form",
            element(
                "input",
                type="search",
                name="q",
                value=query,
                placeholder="Search contacts",
                aria_label="Search contacts",
            ),
            method="get",
            action=urls.CONTACTS,
            class_="search-form inline",
        ),
        element("a", "New", href=urls.contact_form_url(), class_="action"),
        element(
            "button",
            "Delete marked",
            type="button",
            class_="action",
            data_action="delete_marked",
        ),
        element(
            "button",
            "Merge marked",
            type="button",
            class_="action",
            data_action="merge_marked",
        ),
        class_="actions",
    )


def _status(
    found: Sequence[Contact],
    *,
    query: str,
    missing: bool,
) -> str:
    if missing:
        return "No contact store"
    if query:
        return f"Search: {query} — {len(found)} found"
    return f"{len(found)} contacts"


def _no_store() -> Raw:
    return element("p", "No contact store is open.", class_="empty")


def _rows(contacts: Sequence[Contact]) -> Raw:
    if not contacts:
        return element("p", "No contacts yet.", class_="empty", id="rows")
    return element(
        "ol",
        join(_row(contact) for contact in contacts),
        id="rows",
        class_="rows contacts",
    )


def _row(contact: Contact) -> Raw:
    row = contact_row(contact)
    return element(
        "li",
        element(
            "a",
            element("span", row.mark.strip(), class_="mark"),
            element("span", row.last_name, class_="last-name"),
            element("span", row.first_name, class_="first-name"),
            element("span", row.emails, class_="emails"),
            element("span", row.organization, class_="org"),
            href=urls.contact_form_url(contact.id),
        ),
        class_="row",
        data_id=str(contact.id),
        aria_selected="false",
    )


# ----------------------------------------------------------------------
# The form
# ----------------------------------------------------------------------


def contact_form_page(
    services: WebServices,
    *,
    contact: Contact,
    edit: ContactEdit | None = None,
    notices: Sequence[Notice] = (),
) -> str:
    """One contact's fields, new or existing."""
    del services
    fields = edit or ContactEdit.of(contact)
    heading = label_for(contact) if contact.id is not None else "New contact"
    return page(
        title=f"{heading} — Pony Express",
        surface=PageSurface.CONTACTS,
        tab=Tab.CONTACTS,
        status=heading,
        panes=pane(
            name="compose",
            heading=fragment(
                element("span", heading, class_="title"),
                element(
                    "span",
                    element(
                        "button",
                        "Save",
                        type="submit",
                        form="contact-form",
                        class_="action primary",
                    ),
                    element("a", "Cancel", href=urls.contacts_url(), class_="action"),
                    class_="actions",
                ),
            ),
            body=_form(contact, fields),
        ),
        notices=notices,
    )


def _form(contact: Contact, fields: ContactEdit) -> Raw:
    rows = (
        ("First name", "first_name", fields.first_name),
        ("Last name", "last_name", fields.last_name),
        ("Affix (Dr., Jr.)", "affix", fields.affix),
        ("Organisation", "organization", fields.organization),
        ("Email addresses", "emails", fields.emails),
        ("Aliases", "aliases", fields.aliases),
    )
    return element(
        "form",
        element(
            "input",
            type="hidden",
            name="id",
            value="" if contact.id is None else str(contact.id),
        ),
        join(
            element(
                "div",
                element("label", label),
                element(
                    "input",
                    type="text",
                    name=name,
                    id=name,
                    value=value,
                    autocomplete="off",
                    autofocus=name == "first_name",
                ),
                class_="field",
            )
            for label, name, value in rows
        ),
        element(
            "div",
            element("label", "Notes"),
            element("textarea", fields.notes, name="notes", id="notes", rows="8"),
            class_="field",
        ),
        id="contact-form",
        method="post",
        action=urls.CONTACT_FORM,
    )


def edit_from_fields(fields: dict[str, list[str]]) -> ContactEdit:
    def one(name: str) -> str:
        values = fields.get(name, [])
        return values[0] if values else ""

    return ContactEdit(
        first_name=one("first_name"),
        last_name=one("last_name"),
        affix=one("affix"),
        organization=one("organization"),
        emails=one("emails"),
        aliases=one("aliases"),
        notes=one("notes"),
    )


def blank_contact() -> Contact:
    return Contact(id=None, first_name="", last_name="", emails=())


def store_contact(
    contacts: ContactRepository,
    *,
    contact_id: int | None,
    edit: ContactEdit,
) -> tuple[Contact | None, Outcome]:
    """Save *edit* over the contact *contact_id* names, or a new one."""
    existing = blank_contact()
    if contact_id is not None:
        found = contact_by_id(contacts, contact_id)
        if found is None:
            return None, Outcome.refused(warn("That contact is no longer here."))
        existing = found
    return save_contact(contacts, existing, edit)


# ----------------------------------------------------------------------
# Actions over the marks
# ----------------------------------------------------------------------


def _delete_marked(context: ActionContext) -> Outcome | Reply:
    """Delete the marked contacts, once the user has said so.

    Deleting a contact cannot be undone -- there is no trash for them,
    as there is for mail -- so the terminal puts a ``ConfirmScreen`` in
    front of this and so does the browser.
    """
    contacts = context.services.contacts
    if contacts is None:
        return Outcome.refused(warn("No contact store is open."))
    marked = context.ids
    if not marked:
        return Outcome.refused(warn("Mark the contacts to delete."))
    held = held_for_confirmation(
        context,
        f"Delete {len(marked)} contact(s)? This cannot be undone.",
    )
    if held is not None:
        return held
    return delete_contacts(contacts, marked)


def _merge_marked(context: ActionContext) -> Outcome | Reply:
    """Merge the marked contacts into one, once the user has said so."""
    from chronos.outcome import Notice

    contacts = context.services.contacts
    if contacts is None:
        return Outcome.refused(warn("No contact store is open."))
    if not context.ids:
        return Outcome.refused(warn("Mark the contacts to merge."))
    plan = plan_merge(contacts, context.ids)
    if isinstance(plan, Notice):
        return Outcome.refused(plan)
    held = held_for_confirmation(
        context,
        f"Merge {plan.total} contacts into {plan.target_label}?\n"
        f"Merging in: {', '.join(plan.source_labels)}.\n"
        "Merging is irreversible.",
    )
    if held is not None:
        return held
    return merge_contacts(contacts, plan)


CONTACT_ACTIONS: dict[str, Action] = {
    "delete_marked": _delete_marked,
    "merge_marked": _merge_marked,
}


__all__ = [
    "CONTACT_ACTIONS",
    "blank_contact",
    "contact_form_page",
    "contacts_page",
    "edit_from_fields",
    "store_contact",
]
