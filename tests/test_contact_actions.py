"""Contact and delivery operations, exercised without a user interface.

Harvesting an address, merging marked records, filing a sent copy: all of
this used to live inside the composer, the reader and the contact
browser, which is why an address was normalised one way in one and
another way in the next.  It lives in :mod:`pony.contact_actions` and
:mod:`pony.delivery` now, and these tests reach it directly.
"""

from __future__ import annotations

import dataclasses
from email.message import EmailMessage
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from corpus import plain_text
from tui_helpers import (
    make_credentials,
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
)

from pony import contact_actions, delivery
from pony.compose_utils import build_email_message
from pony.contact_actions import MergePlan
from pony.domain import AccountConfig, Contact, FolderRef
from pony.mailbox_actions import Mailboxes

if TYPE_CHECKING:
    from pony.index_store import SqliteIndexRepository

INBOX = FolderRef(account_name="acct", folder_name="INBOX")


def _contacts(label: str) -> SqliteIndexRepository:
    """The index doubles as the contact store, as it does in the app."""
    return make_index(make_tmp_paths(label))


def _add(store: SqliteIndexRepository, first: str, email: str) -> int:
    stored = store.upsert_contact(
        contact=Contact(id=None, first_name=first, last_name="", emails=(email,)),
    )
    assert stored.id is not None
    return stored.id


# ----------------------------------------------------------------------
# Harvesting
# ----------------------------------------------------------------------


def test_harvest_addresses_adds_the_new_ones_only() -> None:
    store = _contacts("harvest-new")
    _add(store, "Known", "known@example.com")

    outcome = contact_actions.harvest_addresses(
        store,
        "Known Person <KNOWN@example.com>, Fresh Face <fresh@example.com>",
        "Third <third@example.com>",
    )

    # The known address is matched case-insensitively and left alone, so
    # the name the user gave it survives contact with a header.
    assert outcome.changed == 2
    known = store.find_contact_by_email(email_address="known@example.com")
    assert known is not None
    assert known.first_name == "Known"
    fresh = store.find_contact_by_email(email_address="fresh@example.com")
    assert fresh is not None
    assert fresh.first_name == "Fresh"


def test_harvest_addresses_without_a_contact_store_is_a_noop() -> None:
    assert contact_actions.harvest_addresses(None, "someone@example.com") == (
        contact_actions.harvest_addresses(None)
    )
    assert not contact_actions.harvest_addresses(None, "a@example.com").acted


def test_harvest_addresses_ignores_an_entry_with_no_address() -> None:
    store = _contacts("harvest-addressless")

    outcome = contact_actions.harvest_addresses(store, "Someone <>")

    assert outcome.changed == 0
    assert store.list_all_contacts() == []


def test_harvest_folder_counts_the_messages_it_read() -> None:
    store = _contacts("harvest-folder")
    store.harvest_contacts = Mock()  # type: ignore[method-assign]

    outcome = contact_actions.harvest_folder(store, [], "INBOX")

    assert [notice.text for notice in outcome.notices] == [
        "Harvested contacts from 0 message(s) in INBOX.",
    ]
    assert contact_actions.harvest_folder(None, [], "INBOX").notices == ()


# ----------------------------------------------------------------------
# The record behind an address
# ----------------------------------------------------------------------


def test_contact_for_address_starts_a_record_when_there_is_none() -> None:
    store = _contacts("for-address-new")

    contact = contact_actions.contact_for_address(
        store,
        "Ada Lovelace",
        "  Ada@Example.COM ",
    )

    assert contact.id is None
    assert (contact.first_name, contact.last_name) == ("Ada", "Lovelace")
    assert contact.emails == ("ada@example.com",)


def test_contact_for_address_fills_in_a_name_the_record_lacks() -> None:
    store = _contacts("for-address-nameless")
    contact_id = _add(store, "", "ada@example.com")

    contact = contact_actions.contact_for_address(
        store,
        "Ada Lovelace",
        "ada@example.com",
    )

    # Same record — this is an edit of it, not a second one.
    assert contact.id == contact_id
    assert contact.display_name == "Ada Lovelace"


def test_contact_for_address_keeps_the_name_the_user_gave() -> None:
    """A header's claim about someone's name does not outrank the store."""
    store = _contacts("for-address-named")
    _add(store, "Ada", "ada@example.com")

    contact = contact_actions.contact_for_address(
        store,
        "A. A. Lovelace",
        "ada@example.com",
    )

    assert contact.first_name == "Ada"

    # And an empty display name never overwrites anything either.
    assert (
        contact_actions.contact_for_address(store, "   ", "ada@example.com").first_name
        == "Ada"
    )


# ----------------------------------------------------------------------
# Deleting and merging
# ----------------------------------------------------------------------


def test_delete_contacts_reports_a_count_and_says_nothing_for_none() -> None:
    store = _contacts("delete-contacts")
    first = _add(store, "One", "one@example.com")
    second = _add(store, "Two", "two@example.com")

    outcome = contact_actions.delete_contacts(store, [first, second])

    assert [notice.text for notice in outcome.notices] == ["Deleted 2 contact(s)."]
    assert store.find_contact_by_email(email_address="one@example.com") is None

    empty = contact_actions.delete_contacts(store, [])
    assert empty.notices == ()
    assert not empty.acted


def test_plan_merge_keeps_the_oldest_record() -> None:
    store = _contacts("merge-plan")
    first = _add(store, "Original", "one@example.com")
    second = _add(store, "Duplicate", "two@example.com")
    third = _add(store, "", "three@example.com")

    plan = contact_actions.plan_merge(store, [third, second, first])

    assert isinstance(plan, MergePlan)
    # Lowest id wins: anything already pointing at a contact keeps
    # pointing at the right one.
    assert plan.target == first
    assert plan.sources == (second, third)
    assert plan.target_label == "Original"
    # A record with no name is named by its address instead.
    assert plan.source_labels == ("Duplicate", "three@example.com")
    assert plan.total == 3


def test_plan_merge_refuses_fewer_than_two() -> None:
    store = _contacts("merge-too-few")
    only = _add(store, "Alone", "alone@example.com")

    refusal = contact_actions.plan_merge(store, [only])

    assert not isinstance(refusal, MergePlan)
    assert refusal.text == "Mark at least 2 contacts to merge."


def test_plan_merge_labels_an_id_that_is_no_longer_there() -> None:
    """A row deleted between marking and merging still gets named."""
    store = _contacts("merge-vanished")
    first = _add(store, "Original", "one@example.com")
    gone = _add(store, "Gone", "gone@example.com")
    store.delete_contact(contact_id=gone)

    plan = contact_actions.plan_merge(store, [first, gone])

    assert isinstance(plan, MergePlan)
    assert plan.source_labels == (f"contact {gone}",)


def test_merge_contacts_folds_the_sources_in() -> None:
    store = _contacts("merge-apply")
    first = _add(store, "Original", "one@example.com")
    second = _add(store, "Duplicate", "two@example.com")
    plan = contact_actions.plan_merge(store, [first, second])
    assert isinstance(plan, MergePlan)

    outcome = contact_actions.merge_contacts(store, plan)

    assert [notice.text for notice in outcome.notices] == ["Merged 2 contacts."]
    survivor = store.find_contact_by_email(email_address="two@example.com")
    assert survivor is not None
    assert survivor.id == first


def test_summarize_stops_after_ten_and_counts_the_rest() -> None:
    labels = [f"person {n}" for n in range(14)]

    body = contact_actions.summarize(labels)

    assert body.count("\n") == contact_actions.MAX_NAMED
    assert body.endswith("… and 4 more")
    assert body.startswith("  - person 0")
    assert "person 10" not in body

    # A short list says nothing about a remainder.
    assert contact_actions.summarize(["only"], prefix="  merging in: ") == (
        "  merging in: only"
    )


# ----------------------------------------------------------------------
# Filing what was written or sent
# ----------------------------------------------------------------------


def _mailboxes(
    label: str,
    *,
    sent_folder: str | None = None,
) -> tuple[Mailboxes, AccountConfig]:
    paths = make_tmp_paths(label)
    account = dataclasses.replace(
        make_test_account(paths),
        sent_folder=sent_folder,
    )
    config = make_test_config(accounts=(account,))
    mailboxes = Mailboxes(
        config=config,
        index=make_index(paths),
        mirrors=make_mirrors(config),
    )
    return mailboxes, account


def test_file_message_inserts_a_row_with_no_uid() -> None:
    """That is what makes the next sync append it to the server."""
    mailboxes, account = _mailboxes("file-message")

    filed = delivery.file_message(
        mailboxes,
        plain_text(),
        account,
        folder_hint="INBOX",
        override=None,
    )

    assert filed.message is not None
    assert filed.message.uid is None
    assert filed.outcome.changed == 1
    assert filed.outcome.notices == ()


def test_file_message_reports_a_folder_it_cannot_find() -> None:
    """A fresh mirror has only INBOX, so there is nowhere to file a copy."""
    mailboxes, account = _mailboxes("file-message-missing")

    filed = delivery.file_message(
        mailboxes,
        plain_text(),
        account,
        folder_hint="Sent",
        override=None,
    )

    assert filed.message is None
    assert [notice.text for notice in filed.outcome.notices] == [
        "Could not find 'Sent' folder for acct",
    ]


def test_file_message_without_a_mirror_says_nothing_to_the_user() -> None:
    """A log line is the right place for it: no action asked for this."""
    mailboxes, account = _mailboxes("file-message-no-mirror")
    bare = Mailboxes(
        config=mailboxes.config,
        index=mailboxes.index,
        mirrors={},
    )

    filed = delivery.file_message(
        bare,
        plain_text(),
        account,
        folder_hint="Drafts",
        override=None,
    )

    assert filed.message is None
    assert filed.outcome.notices == ()


def test_file_message_reports_a_mirror_that_will_not_write() -> None:
    mailboxes, account = _mailboxes("file-message-unwritable")
    mailboxes.mirrors["acct"].store_message = Mock(  # type: ignore[union-attr]
        side_effect=OSError("unwritable"),
    )

    filed = delivery.file_message(
        mailboxes,
        plain_text(),
        account,
        folder_hint="INBOX",
        override=None,
    )

    assert filed.message is None
    (notice,) = filed.outcome.notices
    assert notice.severity == "warning"
    assert notice.text == "Could not save to INBOX: unwritable"


def test_record_sent_retires_the_draft_files_the_copy_and_harvests() -> None:
    mailboxes, account = _mailboxes("record-sent", sent_folder="INBOX")
    raw = plain_text()
    draft = delivery.file_message(
        mailboxes,
        raw,
        account,
        folder_hint="INBOX",
        override="INBOX",
    ).message
    assert draft is not None
    contacts = mailboxes.index

    outcome = delivery.record_sent(
        mailboxes,
        raw,
        account,
        draft=draft,
        contacts=contacts,
        recipients=("Someone <someone@example.com>", ""),
    )

    # The pre-send draft is gone rather than sitting beside the sent copy.
    assert mailboxes.index.get_message(message_ref=draft.message_ref) is None
    # One copy filed, one address harvested.
    assert outcome.changed == 2
    assert outcome.notices == ()
    assert len(list(mailboxes.index.list_folder_messages(folder=INBOX))) == 1
    assert contacts.find_contact_by_email(email_address="someone@example.com")


def test_record_sent_without_a_draft_or_contacts_still_files_the_copy() -> None:
    mailboxes, account = _mailboxes("record-sent-no-draft", sent_folder="INBOX")

    outcome = delivery.record_sent(
        mailboxes,
        plain_text(),
        account,
        draft=None,
        contacts=None,
        recipients=(),
    )

    assert outcome.changed == 1
    assert len(list(mailboxes.index.list_folder_messages(folder=INBOX))) == 1


# ----------------------------------------------------------------------
# The password a send needs
# ----------------------------------------------------------------------


def test_resolve_send_password_prefers_the_credentials_provider() -> None:
    paths = make_tmp_paths("send-password")
    account = make_test_account(paths, password="s3cret")
    config = make_test_config(accounts=(account,))

    found = delivery.resolve_send_password(make_credentials(config), account)

    assert found == "s3cret"


def test_resolve_send_password_falls_back_to_the_config_field() -> None:
    """A bare composer in a test has no provider wired in."""
    paths = make_tmp_paths("send-password-fallback")
    account = make_test_account(paths, password="plain")

    assert delivery.resolve_send_password(None, account) == "plain"


def test_resolve_send_password_says_when_there_is_none() -> None:
    paths = make_tmp_paths("send-password-missing")
    account = make_test_account(paths, password=None)

    missing = delivery.resolve_send_password(None, account)

    assert not isinstance(missing, str)
    assert missing.severity == "error"
    assert "No password available" in missing.text


def test_resolve_send_password_reports_a_provider_that_raises() -> None:
    paths = make_tmp_paths("send-password-broken")
    account = make_test_account(paths)
    broken = Mock()
    broken.get_password = Mock(side_effect=RuntimeError("keyring is locked"))

    failed = delivery.resolve_send_password(broken, account)

    assert not isinstance(failed, str)
    assert failed.severity == "error"
    assert "keyring is locked" in failed.text


# ----------------------------------------------------------------------
# Sending: the whole sequence, with the socket replaced
# ----------------------------------------------------------------------


def _outgoing(subject: str = "Hello") -> EmailMessage:
    return build_email_message(
        from_address="me@example.test",
        to="you@example.test",
        cc="",
        bcc="",
        subject=subject,
        body="Body text.",
        attachment_paths=[],
    )


def _sendable(
    label: str,
    *,
    password: str | None = "secret",
) -> tuple[Mailboxes, AccountConfig]:
    """An account whose Sent and Drafts both land in the one real folder.

    A fresh maildir has only INBOX, so pointing both at it is what lets
    a test see the draft appear and then be replaced by the sent copy.
    """
    paths = make_tmp_paths(label)
    account = dataclasses.replace(
        make_test_account(paths, password=password),
        sent_folder="INBOX",
        drafts_folder="INBOX",
    )
    config = make_test_config(accounts=(account,))
    return (
        Mailboxes(
            config=config,
            index=make_index(paths),
            mirrors=make_mirrors(config),
        ),
        account,
    )


def test_deliver_message_drafts_then_sends_then_files_the_sent_copy() -> None:
    mailboxes, account = _sendable("deliver-happy")
    sent: list[object] = []

    with patch.object(delivery, "smtp_send", lambda **kwargs: sent.append(kwargs)):
        outcome = delivery.deliver_message(
            mailboxes,
            _outgoing(),
            account,
            credentials=None,
            contacts=mailboxes.index,
            to="you@example.test",
            connect_timeout=1.0,
        )

    assert sent, "the message never reached SMTP"
    assert not outcome.failed
    assert outcome.notices[-1].text == (
        "Message sent to you@example.test via smtp.example.com."
    )
    # The pre-send draft was retired and the Sent copy filed in its
    # place, so exactly one row is left, waiting for a UID.
    rows = list(mailboxes.index.list_folder_messages(folder=INBOX))
    assert [row.uid for row in rows] == [None]
    # The address it went to is a contact now.
    assert mailboxes.index.find_contact_by_email(email_address="you@example.test")


def test_deliver_message_leaves_a_failed_send_in_drafts() -> None:
    from pony.smtp_sender import SMTPError

    mailboxes, account = _sendable("deliver-smtp-fails")

    def _refuse(**_kwargs: object) -> None:
        raise SMTPError("smtp.example.com said no")

    with patch.object(delivery, "smtp_send", _refuse):
        outcome = delivery.deliver_message(
            mailboxes,
            _outgoing(),
            account,
            credentials=None,
            contacts=None,
            to="you@example.test",
            connect_timeout=1.0,
        )

    assert outcome.failed
    assert outcome.changed == 0
    (notice,) = outcome.notices
    assert "smtp.example.com said no" in notice.text
    assert "The message is in Drafts." in notice.text
    # And it really is there.
    assert len(list(mailboxes.index.list_folder_messages(folder=INBOX))) == 1


def test_deliver_message_refuses_without_a_password() -> None:
    mailboxes, account = _sendable("deliver-no-password", password=None)

    outcome = delivery.deliver_message(
        mailboxes,
        _outgoing(),
        account,
        credentials=None,
        contacts=None,
        to="you@example.test",
        connect_timeout=1.0,
    )

    assert outcome.failed
    assert "No password available" in outcome.notices[0].text
    # Nothing was filed: there was never a send to protect.
    assert list(mailboxes.index.list_folder_messages(folder=INBOX)) == []


def test_deliver_message_retires_the_draft_it_was_composed_from() -> None:
    """Finishing a saved draft must not leave the saved copy behind."""
    mailboxes, account = _sendable("deliver-source-draft")
    source = delivery.file_message(
        mailboxes,
        plain_text(),
        account,
        folder_hint="Drafts",
        override="INBOX",
    ).message
    assert source is not None

    with patch.object(delivery, "smtp_send", lambda **_kwargs: None):
        outcome = delivery.deliver_message(
            mailboxes,
            _outgoing(),
            account,
            credentials=None,
            contacts=None,
            to="you@example.test",
            connect_timeout=1.0,
            source_draft=source,
        )

    assert not outcome.failed
    remaining = list(mailboxes.index.list_folder_messages(folder=INBOX))
    assert [row.message_ref.id for row in remaining] != [source.message_ref.id]
    assert len(remaining) == 1


def test_deliver_message_refuses_an_account_that_cannot_send() -> None:
    mailboxes, account = _sendable("deliver-no-smtp")
    local = dataclasses.replace(account, smtp=None)

    outcome = delivery.deliver_message(
        mailboxes,
        _outgoing(),
        local,
        credentials=None,
        contacts=None,
        to="you@example.test",
        connect_timeout=1.0,
    )

    assert outcome.failed
    assert "no SMTP server configured" in outcome.notices[0].text


# ----------------------------------------------------------------------
# The contact form
# ----------------------------------------------------------------------


def test_a_contact_edit_round_trips_through_its_text_fields() -> None:
    contact = Contact(
        id=None,
        first_name="Ada",
        last_name="Lovelace",
        emails=("ada@example.com", "al@example.com"),
        affix=("Dr.",),
        aliases=("Ada",),
        organization="Analytical",
        notes="Notes.",
    )

    edit = contact_actions.ContactEdit.of(contact)

    assert edit.emails == "ada@example.com, al@example.com"
    assert edit.affix == "Dr."
    assert edit.applied_to(contact) == contact


def test_saving_a_contact_normalises_its_addresses() -> None:
    store = _contacts("contact-save")

    saved, outcome = contact_actions.save_contact(
        store,
        Contact(id=None, first_name="", last_name="", emails=()),
        contact_actions.ContactEdit(
            first_name="Grace",
            last_name="Hopper",
            emails=" Grace@Example.COM , second@example.com ",
        ),
    )

    assert saved is not None
    assert saved.emails == ("grace@example.com", "second@example.com")
    assert outcome.notices[0].text == "Saved Grace Hopper."
    assert store.find_contact_by_email(email_address="grace@example.com") is not None


def test_saving_a_contact_with_no_name_and_no_address_is_refused() -> None:
    store = _contacts("contact-save-empty")

    saved, outcome = contact_actions.save_contact(
        store,
        Contact(id=None, first_name="", last_name="", emails=()),
        contact_actions.ContactEdit(notes="just a note"),
    )

    assert saved is None
    assert outcome.notices[0].text == "A contact needs a name or an address."


def test_saving_an_address_that_belongs_to_someone_else_says_so() -> None:
    """An address belongs to exactly one contact, so this is a merge."""
    store = _contacts("contact-save-conflict")
    _add(store, "Owner", "shared@example.com")
    other = store.upsert_contact(
        contact=Contact(id=None, first_name="Other", last_name="", emails=()),
    )

    saved, outcome = contact_actions.save_contact(
        store,
        other,
        contact_actions.ContactEdit(first_name="Other", emails="shared@example.com"),
    )

    assert saved is None
    assert outcome.failed
    assert "Merge the two contacts instead." in outcome.notices[0].text


def test_listing_contacts_sorts_by_surname_and_filters_by_query() -> None:
    store = _contacts("contact-list")
    store.upsert_contact(
        contact=Contact(
            id=None,
            first_name="Ada",
            last_name="Zeta",
            emails=("ada@example.com",),
        ),
    )
    store.upsert_contact(
        contact=Contact(
            id=None,
            first_name="Bob",
            last_name="Alpha",
            emails=("bob@example.com",),
        ),
    )

    everyone = contact_actions.list_contacts(store)
    filtered = contact_actions.list_contacts(store, query="ada")

    assert [c.last_name for c in everyone] == ["Alpha", "Zeta"]
    assert [c.first_name for c in filtered] == ["Ada"]
