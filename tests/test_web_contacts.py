"""The contacts browser in a browser.

The operations are :mod:`pony.mail.contact_actions`, tested there; what is
checked here is that the page offers all four of them, that the form
round-trips a record, and that a clash is reported rather than
swallowed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.testclient import TestClient
from tui_helpers import (
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
)

from pony.mail.domain import Contact
from pony.web import WebServices, build_web_app

if TYPE_CHECKING:
    from pony.mail.index_store import SqliteIndexRepository


def _services(label: str) -> tuple[WebServices, SqliteIndexRepository]:
    paths = make_tmp_paths(label)
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    return (
        WebServices(
            config=config,
            index=index,
            mirrors=make_mirrors(config),
            contacts=index,
        ),
        index,
    )


def _client(services: WebServices) -> TestClient:
    return TestClient(build_web_app(services))


def _add(store: SqliteIndexRepository, first: str, last: str, email: str) -> int:
    stored = store.upsert_contact(
        contact=Contact(id=None, first_name=first, last_name=last, emails=(email,)),
    )
    assert stored.id is not None
    return stored.id


# ----------------------------------------------------------------------
# The list
# ----------------------------------------------------------------------


def test_the_list_shows_the_same_columns_the_terminal_shows() -> None:
    services, store = _services("contacts-list")
    _add(store, "Ada", "Lovelace", "ada@example.test")
    _add(store, "Bob", "Alpha", "bob@example.test")

    with _client(services) as client:
        body = client.get("/contacts").text

    assert "Last name" in body
    assert "Email(s)" in body
    assert "Organisation" in body
    # By surname, as `list_contacts` orders them.
    assert body.index("Alpha") < body.index("Lovelace")
    assert "2 contacts" in body


def test_the_list_can_be_filtered() -> None:
    services, store = _services("contacts-filter")
    _add(store, "Ada", "Lovelace", "ada@example.test")
    _add(store, "Bob", "Alpha", "bob@example.test")

    with _client(services) as client:
        body = client.get("/contacts?q=ada").text

    assert "Lovelace" in body
    assert "Alpha" not in body
    assert "Search: ada" in body


def test_the_list_leads_to_the_blank_form_without_a_keyboard() -> None:
    """Keeping an address has to be reachable from the page itself.

    The terminal has ``c`` for it and harvests a folder with ``H``; the
    browser serves the same keys, and this is the control beside them --
    so an address met in the browser can be kept without going to the
    terminal.
    """
    from web_helpers import link_to

    services, _store = _services("contacts-new-entry")

    with _client(services) as client:
        body = client.get("/contacts").text
        blank = client.get(link_to(body, "New")).text

    assert link_to(body, "New") == "/contacts/edit"
    assert "New contact" in blank


def test_an_empty_store_says_so() -> None:
    services, _store = _services("contacts-empty")

    with _client(services) as client:
        assert "No contacts yet." in client.get("/contacts").text


def test_a_front_end_without_a_contact_store_says_that_instead() -> None:
    import dataclasses

    services, _store = _services("contacts-none")
    bare = dataclasses.replace(services, contacts=None)

    with _client(bare) as client:
        body = client.get("/contacts").text

    assert "No contact store is open." in body


# ----------------------------------------------------------------------
# The form
# ----------------------------------------------------------------------


def test_the_form_opens_on_an_existing_contact() -> None:
    services, store = _services("contacts-form")
    contact_id = _add(store, "Ada", "Lovelace", "ada@example.test")

    with _client(services) as client:
        body = client.get(f"/contacts/edit?id={contact_id}").text

    assert 'value="Ada"' in body
    assert 'value="ada@example.test"' in body
    assert "Ada Lovelace" in body


def test_the_form_opens_blank_for_a_new_contact() -> None:
    services, _store = _services("contacts-form-new")

    with _client(services) as client:
        body = client.get("/contacts/edit").text

    assert "New contact" in body
    assert 'name="first_name"' in body


def test_a_contact_that_is_no_longer_there_is_a_404() -> None:
    services, _store = _services("contacts-form-missing")

    with _client(services) as client:
        assert client.get("/contacts/edit?id=404").status_code == 404


def test_saving_the_form_stores_the_contact() -> None:
    services, store = _services("contacts-save")

    with _client(services) as client:
        response = client.post(
            "/contacts/edit",
            data={
                "id": "",
                "first_name": "Grace",
                "last_name": "Hopper",
                "emails": "Grace@Example.test, g@example.test",
                "organization": "Navy",
                "notes": "Compilers.",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Saved+Grace+Hopper." in response.headers["location"]
    saved = store.find_contact_by_email(email_address="grace@example.test")
    assert saved is not None
    assert saved.emails == ("grace@example.test", "g@example.test")
    assert saved.organization == "Navy"


def test_saving_an_address_that_belongs_to_someone_else_keeps_the_form() -> None:
    services, store = _services("contacts-conflict")
    _add(store, "Owner", "", "shared@example.test")
    other = _add(store, "Other", "", "other@example.test")

    with _client(services) as client:
        response = client.post(
            "/contacts/edit",
            data={
                "id": str(other),
                "first_name": "Other",
                "emails": "shared@example.test",
            },
        )

    assert response.status_code == 200
    assert "Merge the two contacts instead." in response.text
    # Still a form, holding what was typed.
    assert 'id="contact-form"' in response.text
    assert "shared@example.test" in response.text


# ----------------------------------------------------------------------
# The marks
# ----------------------------------------------------------------------


def test_marked_contacts_can_be_deleted() -> None:
    services, store = _services("contacts-delete")
    first = _add(store, "Ada", "Lovelace", "ada@example.test")
    second = _add(store, "Bob", "Alpha", "bob@example.test")

    with _client(services) as client:
        response = client.post(
            "/action/delete_marked",
            # Answered, as the page answers it: the question comes back
            # first and the same action is re-sent with `confirm`.
            data={"id": [str(first), str(second)], "confirm": "1"},
        )

    assert response.json()["changed"] == 2
    assert store.list_all_contacts() == []


def test_deleting_contacts_asks_first_and_changes_nothing_until_answered() -> None:
    """Irreversible in the terminal behind a `ConfirmScreen`; here too."""
    services, store = _services("contacts-delete-confirm")
    first = _add(store, "Ada", "Lovelace", "ada@example.test")
    second = _add(store, "Bob", "Alpha", "bob@example.test")

    with _client(services) as client:
        asked = client.post(
            "/action/delete_marked",
            data={"id": [str(first), str(second)]},
        ).json()

    assert asked["changed"] == 0
    assert "cannot be undone" in asked["confirm"]["question"]
    assert asked["confirm"]["fields"] == {"confirm": "1"}
    # Nothing happened, which is the whole point of asking.
    assert len(store.list_all_contacts()) == 2


def test_deleting_with_nothing_marked_says_so() -> None:
    services, _store = _services("contacts-delete-none")

    with _client(services) as client:
        response = client.post("/action/delete_marked", data={})

    assert response.json()["notices"][0]["text"] == "Mark the contacts to delete."


def test_marked_contacts_can_be_merged_into_the_oldest() -> None:
    services, store = _services("contacts-merge")
    first = _add(store, "Ada", "Lovelace", "ada@example.test")
    second = _add(store, "A.", "Lovelace", "ada.l@example.test")

    with _client(services) as client:
        response = client.post(
            "/action/merge_marked",
            data={"id": [str(second), str(first)], "confirm": "1"},
        )

    assert response.json()["changed"] == 2
    (survivor,) = store.list_all_contacts()
    assert survivor.id == first
    assert set(survivor.emails) == {"ada@example.test", "ada.l@example.test"}


def test_merging_one_contact_is_refused_with_a_reason() -> None:
    services, store = _services("contacts-merge-one")
    only = _add(store, "Ada", "Lovelace", "ada@example.test")

    with _client(services) as client:
        response = client.post("/action/merge_marked", data={"id": str(only)})

    assert response.json()["notices"][0]["text"] == "Mark at least 2 contacts to merge."


def test_merging_contacts_says_it_is_irreversible_before_doing_it() -> None:
    """Which record survives is named in the question, as in the terminal."""
    services, store = _services("contacts-merge-confirm")
    first = _add(store, "Ada", "Lovelace", "ada@example.test")
    second = _add(store, "A.", "Lovelace", "ada.l@example.test")

    with _client(services) as client:
        asked = client.post(
            "/action/merge_marked",
            data={"id": [str(second), str(first)]},
        ).json()

    assert asked["changed"] == 0
    question = asked["confirm"]["question"]
    assert "Merging is irreversible." in question
    assert "Ada Lovelace" in question
    assert len(store.list_all_contacts()) == 2


def test_merging_with_nothing_marked_says_so() -> None:
    services, _store = _services("contacts-merge-none")

    with _client(services) as client:
        response = client.post("/action/merge_marked", data={})

    assert response.json()["notices"][0]["text"] == "Mark the contacts to merge."


def test_the_list_offers_buttons_for_its_bulk_operations() -> None:
    """The keyboard has `D` and `M`; a phone needs something to tap."""
    services, store = _services("contacts-tools")
    _add(store, "Ada", "Lovelace", "ada@example.test")

    with _client(services) as client:
        body = client.get("/contacts").text

    assert 'data-action="delete_marked"' in body
    assert 'data-action="merge_marked"' in body
    assert "/contacts/edit" in body
    assert 'type="search"' in body


def test_tapping_delete_marked_on_a_fresh_list_deletes_nobody() -> None:
    """The cursor starts on the first row, and must not stand in for a mark.

    So "Delete marked" on a list nobody has marked anything in used to
    delete the first contact, silently.  Both halves say no: the page
    sends the marks alone for these two actions, and the action refuses
    an empty set.
    """
    from web_helpers import dispatch

    services, store = _services("contacts-fresh-delete")
    _add(store, "Ada", "Lovelace", "ada@example.test")
    _add(store, "Bob", "Alpha", "bob@example.test")

    with _client(services) as client:
        page = client.get("/contacts").text
        # No `ids`: the page has just loaded and nothing is marked.
        refused = dispatch(client, page, "delete_marked")

    assert refused.changed == 0
    assert refused.texts == ("Mark the contacts to delete.",)
    assert len(store.list_all_contacts()) == 2
