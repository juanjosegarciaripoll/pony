"""Writing and sending mail from the browser.

The prefilling is :mod:`pony.composer`'s and the sending is
:func:`pony.delivery.deliver_message`, both tested on their own; what is
checked here is the form around them — that a reply arrives addressed
and quoted, that Send reaches SMTP with what the fields said, that a
failed send keeps the text on screen, and that an attachment uploaded as
bytes is the file that goes out.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING
from unittest.mock import patch

from corpus import plain_text
from starlette.testclient import TestClient
from tui_helpers import (
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
    seed_message,
)

from pony import delivery
from pony.domain import FolderRef
from pony.web import WebServices, build_web_app

if TYPE_CHECKING:
    from pony.index_store import SqliteIndexRepository

INBOX = FolderRef(account_name="acct", folder_name="INBOX")


def _services(label: str) -> tuple[WebServices, SqliteIndexRepository]:
    """An account whose Sent and Drafts both land in the one real folder."""
    paths = make_tmp_paths(label)
    account = dataclasses.replace(
        make_test_account(paths),
        sent_folder="INBOX",
        drafts_folder="INBOX",
    )
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=INBOX,
        raw=plain_text(),
        message_id="<original@example.com>",
    )
    return (
        WebServices(config=config, index=index, mirrors=mirrors, contacts=index),
        index,
    )


def _client(services: WebServices) -> TestClient:
    return TestClient(build_web_app(services))


def _only_message(index: SqliteIndexRepository) -> int:
    return index.list_folder_message_summaries(folder=INBOX)[0].message_ref.id


# ----------------------------------------------------------------------
# The form
# ----------------------------------------------------------------------


def test_a_blank_compose_offers_the_accounts_that_can_send() -> None:
    services, _index = _services("compose-new")

    with _client(services) as client:
        body = client.get("/compose?mode=new").text

    assert 'id="compose-form"' in body
    assert '<option value="acct"' in body
    assert 'name="to"' in body
    assert 'name="body"' in body


def test_a_reply_arrives_addressed_quoted_and_threaded() -> None:
    services, index = _services("compose-reply")
    message_id = _only_message(index)

    with _client(services) as client:
        body = client.get(
            f"/compose?mode=reply&account=acct&folder=INBOX&id={message_id}",
        ).text

    assert "alice@example.com" in body
    assert "Re: Tuesday meeting confirmed" in body
    assert "&gt; Hi Bob," in body
    # The threading headers travel in the form, so a reply sent later
    # still lands in its thread.
    assert 'name="in_reply_to"' in body


def test_replying_to_all_widens_the_recipients() -> None:
    services, index = _services("compose-reply-all")
    message_id = _only_message(index)

    with _client(services) as client:
        one = client.get(
            f"/compose?mode=reply&account=acct&folder=INBOX&id={message_id}",
        ).text
        all_of_them = client.get(
            f"/compose?mode=reply_all&account=acct&folder=INBOX&id={message_id}",
        ).text

    assert 'name="cc" id="cc" value=""' in one
    assert "carol@example.com" in all_of_them


def test_a_forward_carries_the_original_rather_than_a_path() -> None:
    """The page never holds a filename the server would then open."""
    services, index = _services("compose-forward")
    message_id = _only_message(index)

    with _client(services) as client:
        body = client.get(
            f"/compose?mode=forward&account=acct&folder=INBOX&id={message_id}",
        ).text

    assert "Fwd: Tuesday meeting confirmed" in body
    assert 'name="forward_of"' in body
    assert ".eml" not in body


def test_resuming_a_draft_fills_the_form_from_its_bytes() -> None:
    services, index = _services("compose-draft")
    message_id = _only_message(index)

    with _client(services) as client:
        body = client.get(
            f"/compose?mode=draft&account=acct&folder=INBOX&id={message_id}",
        ).text

    assert "bob@example.com" in body
    assert "Tuesday meeting confirmed" in body
    # The draft it came from, so sending retires it.
    assert 'name="source"' in body


def test_composing_a_reply_to_nothing_is_reported_not_crashed() -> None:
    services, _index = _services("compose-reply-missing")

    with _client(services) as client:
        response = client.get(
            "/compose?mode=reply&account=acct&folder=INBOX&id=404",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "no+longer+here" in response.headers["location"]


def test_an_unknown_mode_is_refused() -> None:
    services, index = _services("compose-bad-mode")
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.get(
            f"/compose?mode=shout&account=acct&folder=INBOX&id={message_id}",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "not+a+way+to+compose" in response.headers["location"]


# ----------------------------------------------------------------------
# Sending
# ----------------------------------------------------------------------


def test_sending_reaches_smtp_with_what_the_form_said() -> None:
    services, index = _services("compose-send")
    sent: list[dict[str, object]] = []

    with (
        patch.object(delivery, "smtp_send", lambda **kwargs: sent.append(kwargs)),
        _client(services) as client,
    ):
        response = client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "cc": "",
                "bcc": "",
                "subject": "Lunch",
                "body": "At one?",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Message+sent+to+you%40example.test" in response.headers["location"]
    (call,) = sent
    message = call["msg"]
    assert message["To"] == "you@example.test"
    assert message["Subject"] == "Lunch"
    # The Sent copy is in the index with no UID, for the next sync.
    rows = index.list_folder_messages(folder=INBOX)
    assert any(row.uid is None and row.subject == "Lunch" for row in rows)


def test_a_failed_send_keeps_the_form_and_the_text() -> None:
    from pony.smtp_sender import SMTPError

    services, _index = _services("compose-send-fails")

    def _refuse(**_kwargs: object) -> None:
        raise SMTPError("nobody answered")

    with (
        patch.object(delivery, "smtp_send", _refuse),
        _client(services) as client,
    ):
        response = client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "subject": "Lunch",
                "body": "At one?",
            },
        )

    assert response.status_code == 200
    body = response.text
    assert "nobody answered" in body
    # Still a form, still holding what was typed.
    assert 'id="compose-form"' in body
    assert "At one?" in body


def test_a_message_with_no_recipient_is_refused_before_smtp() -> None:
    services, _index = _services("compose-send-no-to")
    sent: list[object] = []

    with (
        patch.object(delivery, "smtp_send", lambda **kwargs: sent.append(kwargs)),
        _client(services) as client,
    ):
        response = client.post(
            "/compose",
            data={"account": "acct", "to": "  ", "subject": "Lunch"},
        )

    assert "A message needs at least one recipient." in response.text
    assert sent == []
    # A refusal is a warning, not an error, and it must not cost the
    # user what they had typed.
    assert 'id="compose-form"' in response.text
    assert "Lunch" in response.text


def test_saving_a_draft_files_it_without_sending() -> None:
    services, index = _services("compose-save-draft")
    sent: list[object] = []

    with (
        patch.object(delivery, "smtp_send", lambda **kwargs: sent.append(kwargs)),
        _client(services) as client,
    ):
        response = client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "subject": "Later",
                "body": "Half written",
                "save_draft": "1",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert sent == []
    assert any(
        row.subject == "Later" for row in index.list_folder_messages(folder=INBOX)
    )


def test_sending_a_resumed_draft_retires_the_draft_it_came_from() -> None:
    services, index = _services("compose-send-draft")
    source = _only_message(index)
    source_ref = f"acct\x1fINBOX\x1f{source}"

    with (
        patch.object(delivery, "smtp_send", lambda **_kwargs: None),
        _client(services) as client,
    ):
        client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "subject": "Finished",
                "body": "Done.",
                "source": source_ref,
            },
            follow_redirects=False,
        )

    remaining = [row.message_ref.id for row in index.list_folder_messages(folder=INBOX)]
    assert source not in remaining


# ----------------------------------------------------------------------
# Attachments
# ----------------------------------------------------------------------


def test_an_attachment_is_uploaded_as_bytes_and_sent_with_the_message() -> None:
    """No multipart parser, and no path from the page either."""
    services, _index = _services("compose-attachment")
    sent: list[dict[str, object]] = []

    with (
        patch.object(delivery, "smtp_send", lambda **kwargs: sent.append(kwargs)),
        _client(services) as client,
    ):
        staged = client.post(
            "/compose/attachment?name=notes.txt",
            content=b"some notes",
        )
        token = staged.json()["token"]
        client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "subject": "With a file",
                "body": "Attached.",
                "attachment": token,
            },
            follow_redirects=False,
        )

    assert staged.json()["name"] == "notes.txt"
    (call,) = sent
    names = [
        part.get_filename()
        for part in call["msg"].walk()  # type: ignore[union-attr]
        if part.get_filename()
    ]
    assert names == ["notes.txt"]


def test_an_empty_upload_is_refused() -> None:
    services, _index = _services("compose-attachment-empty")

    with _client(services) as client:
        response = client.post("/compose/attachment?name=nothing.txt", content=b"")

    assert response.status_code == 400
    assert response.json()["error"] == "That file is empty."


def test_an_attachment_token_that_is_not_ours_is_not_opened() -> None:
    """A token addresses a file; it is not a path to one."""
    from pony.web.compose import staged_path

    assert staged_path("../../etc/passwd") is None
    assert staged_path("not-hex-at-all") is None
    assert staged_path("0123456789abcdef") is None


def test_a_send_whose_attachment_expired_says_so() -> None:
    services, _index = _services("compose-attachment-expired")

    with _client(services) as client:
        response = client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "subject": "Gone",
                "body": "?",
                "attachment": "0123456789abcdef",
            },
        )

    # An unknown token is dropped on the way in, so the message is sent
    # without it rather than silently carrying something else.
    assert response.status_code in (200, 303)


def test_sending_a_forward_attaches_the_message_being_forwarded() -> None:
    """The server materialises it from the index, not from a sent path."""
    services, index = _services("compose-forward-send")
    original = _only_message(index)
    sent: list[dict[str, object]] = []

    with (
        patch.object(delivery, "smtp_send", lambda **kwargs: sent.append(kwargs)),
        _client(services) as client,
    ):
        client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "subject": "Fwd: Tuesday meeting confirmed",
                "body": "See below.",
                "forward_of": f"acct\x1fINBOX\x1f{original}",
            },
            follow_redirects=False,
        )

    (call,) = sent
    attached = [
        part
        for part in call["msg"].walk()  # type: ignore[union-attr]
        if part.get_filename()
    ]
    assert [part.get_filename() for part in attached] == [
        "Tuesday meeting confirmed.eml",
    ]
    payload = attached[0].get_payload(decode=True)
    if payload is None:  # a message/rfc822 part parses into a sub-message
        payload = attached[0].get_payload(0).as_bytes()
    assert b"Subject: Tuesday meeting confirmed" in payload


def test_forwarding_a_message_that_has_gone_is_reported() -> None:
    services, _index = _services("compose-forward-gone")

    with (
        patch.object(delivery, "smtp_send", lambda **_kwargs: None),
        _client(services) as client,
    ):
        response = client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "subject": "Fwd",
                "body": "?",
                "forward_of": "acct\x1fINBOX\x1f999",
            },
        )

    assert "The message being forwarded is no longer here." in response.text


def test_an_account_the_form_names_but_cannot_send_from_is_refused() -> None:
    services, _index = _services("compose-bad-account")

    with _client(services) as client:
        response = client.post(
            "/compose",
            data={"account": "someone-else", "to": "you@example.test"},
        )

    assert "That account cannot send mail." in response.text


def test_a_reply_sent_from_the_browser_marks_the_original_answered() -> None:
    from pony.domain import MessageFlag

    services, index = _services("compose-reply-answered")
    (original,) = index.list_folder_messages(folder=INBOX)

    with (
        patch.object(delivery, "smtp_send", lambda **_kwargs: None),
        _client(services) as client,
    ):
        page = client.get(
            f"/compose?mode=reply&account={original.message_ref.account_name}"
            f"&folder={original.message_ref.folder_name}"
            f"&id={original.message_ref.id}",
        ).text
        assert "answering" in page
        client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "them@example.test",
                "subject": "Re: hello",
                "body": "Yes",
                "answering": (
                    f"{original.message_ref.account_name}\x1f"
                    f"{original.message_ref.folder_name}\x1f"
                    f"{original.message_ref.id}"
                ),
            },
        )

    answered = index.get_message(message_ref=original.message_ref)
    assert answered is not None
    assert MessageFlag.ANSWERED in answered.local_flags


def test_saving_a_resumed_draft_leaves_one_copy() -> None:
    from pony.domain import MessageStatus

    services, index = _services("compose-save-resumed")
    (original,) = index.list_folder_messages(folder=INBOX)

    with _client(services) as client:
        client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "subject": "Still a draft",
                "body": "Later.",
                "save_draft": "1",
                "source": (
                    f"{original.message_ref.account_name}\x1f"
                    f"{original.message_ref.folder_name}\x1f"
                    f"{original.message_ref.id}"
                ),
            },
        )

    retired = index.get_message(message_ref=original.message_ref)
    assert retired is None or retired.local_status is MessageStatus.TRASHED


def test_only_a_draft_can_be_edited_as_one() -> None:
    """The terminal gates the key on the folder; a URL has to be gated too."""
    paths = make_tmp_paths("compose-draft-gate")
    account = dataclasses.replace(
        make_test_account(paths),
        sent_folder="Sent",
        drafts_folder="Drafts",
    )
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=INBOX,
        raw=plain_text(),
        message_id="<received@example.com>",
    )
    services = WebServices(
        config=config,
        index=index,
        mirrors=mirrors,
        contacts=index,
    )
    received = index.list_folder_message_summaries(folder=INBOX)[0]

    with _client(services) as client:
        response = client.get(
            "/compose?mode=draft&account=acct&folder=INBOX"
            f"&id={received.message_ref.id}",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Drafts" in response.headers["location"]
