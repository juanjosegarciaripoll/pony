"""Writing and sending mail from the browser.

The prefilling is :mod:`pony.mail.composer`'s and the sending is
:func:`pony.mail.delivery.deliver_message`, both tested on their own; what is
checked here is the form around them — that a reply arrives addressed
and quoted, that Send reaches SMTP with what the fields said, that a
failed send keeps the text on screen, and that an attachment uploaded as
bytes is the file that goes out.
"""

from __future__ import annotations

import dataclasses
import re
from html import unescape
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch
from urllib.parse import urlencode

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
from web_helpers import form_fields

from pony.mail import delivery
from pony.mail.domain import FolderRef, MessageRef
from pony.web import WebServices, build_web_app

if TYPE_CHECKING:
    from email.message import EmailMessage

    from pony.mail.index_store import SqliteIndexRepository
    from pony.paths import AppPaths

INBOX = FolderRef(account_name="acct", folder_name="INBOX")


def _services(label: str) -> tuple[WebServices, SqliteIndexRepository]:
    """An account whose Sent and Drafts both land in the one real folder."""
    services, index, _paths = _services_and_paths(label)
    return services, index


def _services_and_paths(
    label: str,
) -> tuple[WebServices, SqliteIndexRepository, AppPaths]:
    """The same, with the paths, for what cares where things are written."""
    paths = make_tmp_paths(label)
    account = dataclasses.replace(
        make_test_account(paths),
        sent_folder="INBOX",
        drafts_folder="INBOX",
    )
    config = dataclasses.replace(
        make_test_config(accounts=(account,)),
        # The real thing declares one, and the staging directory hangs
        # off it; a config without one takes a different branch.
        data_dir=paths.data_dir,
    )
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
        paths,
    )


def _client(services: WebServices) -> TestClient:
    return TestClient(build_web_app(services))


def _only_message(index: SqliteIndexRepository) -> int:
    return index.list_folder_message_summaries(folder=INBOX)[0].message_ref.id


def _ref(message_id: int, *, folder: str = "INBOX") -> str:
    """A message reference as the form's hidden fields carry it.

    The same encoding `urls` uses for a link, which is what the page
    writes into `source`, `forward_of` and `answering`.
    """
    from pony.web.compose import _ref_value

    return _ref_value(
        MessageRef(account_name="acct", folder_name=folder, id=message_id)
    )


# ----------------------------------------------------------------------
# The form
# ----------------------------------------------------------------------


def test_a_hidden_message_reference_is_spelled_as_a_link_is() -> None:
    """One encoding for naming a message, not two.

    The hidden fields carried ``account\x1ffolder\x1fid``: a raw control
    character, which a browser does submit as ``%1F`` but which is an
    HTML5 parse error in an attribute value — and nothing submitted a
    compose form through a real browser until the browser test did. The
    fields are percent-encoded query parameters now, the same three
    `urls` puts in a link, so a folder with an ``&`` or an ``=`` in its
    name round-trips.
    """
    from pony.web.compose import _ref_value, parse_ref

    awkward = MessageRef(account_name="acct", folder_name="Archive&2024=x", id=7)
    value = _ref_value(awkward)

    assert "\x1f" not in value
    assert parse_ref(value) == awkward
    # The page writes it into the form, and the form is parseable HTML.
    assert "%26" in value
    # Nothing, and anything that is not a reference, is no reference.
    assert _ref_value(None) == ""
    assert parse_ref("") is None
    assert parse_ref("account=acct&folder=INBOX") is None
    assert parse_ref("account=acct&folder=INBOX&id=nope") is None


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
    message = cast("EmailMessage", call["msg"])
    assert message["To"] == "you@example.test"
    assert message["Subject"] == "Lunch"
    # The Sent copy is in the index with no UID, for the next sync.
    rows = index.list_folder_messages(folder=INBOX)
    assert any(row.uid is None and row.subject == "Lunch" for row in rows)


def test_a_failed_send_keeps_the_form_and_the_text() -> None:
    from pony.mail.smtp_sender import SMTPError

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


def test_sending_a_reply_returns_to_the_folder_it_was_read_in() -> None:
    """A reply carries its message as `answering`, not as `source`.

    The redirect only looked at `source` and `forward_of`, so a sent
    reply landed on whatever `/mail` resolves to -- the first account's
    inbox -- rather than where the user was reading.  Forwarding and
    resuming a draft were right; replying never was.
    """
    services, index = _services("compose-reply-back")
    source = _only_message(index)

    with (
        patch.object(delivery, "smtp_send", lambda **_kwargs: None),
        _client(services) as client,
    ):
        page = client.get(
            "/compose?"
            + urlencode(
                {"mode": "reply", "account": "acct", "folder": "INBOX", "id": source}
            )
        ).text
        # Exactly the fields a browser would send back.
        hidden = dict(
            re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page)
        )
        assert "answering" in hidden
        fields = {name: unescape(value) for name, value in hidden.items()}
        fields.update(
            {
                "account": "acct",
                "to": "you@example.test",
                "subject": "Re: hi",
                "body": "thanks",
                "send": "1",
            }
        )
        response = client.post("/compose", data=fields, follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert "account=acct" in location
    assert "folder=INBOX" in location


def test_discarding_a_reply_goes_back_where_sending_it_would() -> None:
    """Both routes ask the same function, so they cannot disagree."""
    services, index = _services("compose-reply-discard")
    source = _only_message(index)

    with _client(services) as client:
        page = client.get(
            "/compose?"
            + urlencode(
                {"mode": "reply", "account": "acct", "folder": "INBOX", "id": source}
            )
        ).text

    discard = re.search(r'<a href="([^"]*)"[^>]*>Discard</a>', page)
    assert discard is not None
    assert "account=acct" in unescape(discard.group(1))
    assert "folder=INBOX" in unescape(discard.group(1))


def test_sending_a_resumed_draft_retires_the_draft_it_came_from() -> None:
    services, index = _services("compose-send-draft")
    source = _only_message(index)
    source_ref = _ref(source)

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


def test_an_upload_over_the_limit_is_refused_politely() -> None:
    """It arrives as one byte string in memory, so it has to have a cap."""
    from pony.web.app import MAX_ATTACHMENT_BYTES

    services, _index = _services("compose-attachment-huge")

    with _client(services) as client:
        # Declared, which is how a browser sends it: refused without
        # reading the body at all.
        declared = client.post(
            "/compose/attachment?name=big.bin",
            content=b"x",
            headers={"content-length": str(MAX_ATTACHMENT_BYTES + 1)},
        )
        # And when the size is not declared, by counting what arrives.
        streamed = client.post(
            "/compose/attachment?name=big.bin",
            content=iter([b"x" * 4096] * ((MAX_ATTACHMENT_BYTES // 4096) + 2)),
        )

    for response in (declared, streamed):
        assert response.status_code == 413
        assert "larger than 25 MB" in response.json()["error"]


def test_a_staged_upload_lives_under_the_data_directory() -> None:
    """Not in ``$TMPDIR``, which is a share of RAM on many machines.

    It used to be a ``tempfile.mkdtemp`` outside ``data_dir``, outside
    ``PONY_ROOT``, and removed by nothing at all.
    """
    from pony.web.compose import staged_path

    services, _index, paths = _services_and_paths("compose-staging-home")

    with _client(services) as client:
        token = client.post(
            "/compose/attachment?name=notes.txt",
            content=b"some notes",
        ).json()["token"]

    staged = staged_path(services.config, token)
    assert staged is not None
    assert staged.is_relative_to(paths.data_dir)


def test_a_staged_upload_is_let_go_of_once_the_message_has_gone() -> None:
    """Nothing removed them, so every upload stayed for the machine's life."""
    from pony.web.compose import staged_path

    services, _index = _services("compose-staging-released")

    with (
        patch.object(delivery, "smtp_send", lambda **_kwargs: None),
        _client(services) as client,
    ):
        token = client.post(
            "/compose/attachment?name=notes.txt",
            content=b"some notes",
        ).json()["token"]
        assert staged_path(services.config, token) is not None

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

    assert staged_path(services.config, token) is None


def test_a_refused_send_keeps_the_upload_for_the_next_attempt() -> None:
    """The form comes back with the token in it, so the file has to stay."""
    from pony.mail.smtp_sender import SMTPError
    from pony.web.compose import staged_path

    services, _index = _services("compose-staging-kept")

    def _refuse(**_kwargs: object) -> None:
        raise SMTPError("nobody answered")

    with (
        patch.object(delivery, "smtp_send", _refuse),
        _client(services) as client,
    ):
        token = client.post(
            "/compose/attachment?name=notes.txt",
            content=b"some notes",
        ).json()["token"]
        refused = client.post(
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

    assert refused.status_code == 200
    assert staged_path(services.config, token) is not None


def test_forwarding_twice_does_not_leave_two_copies_behind() -> None:
    """The forwarded copy is written afresh each attempt, so it goes each time.

    Four failed sends of one forward left four whole copies of the
    message in the staging directory.
    """
    from pony.mail.smtp_sender import SMTPError
    from pony.web.compose import staging_dir

    services, index = _services("compose-forward-copies")
    message_id = _only_message(index)

    def _refuse(**_kwargs: object) -> None:
        raise SMTPError("nobody answered")

    with (
        patch.object(delivery, "smtp_send", _refuse),
        _client(services) as client,
    ):
        form = client.get(
            f"/compose?mode=forward&account=acct&folder=INBOX&id={message_id}",
        ).text
        fields = form_fields(form, "#compose-form")
        for _attempt in range(3):
            client.post(
                "/compose",
                data={**fields, "to": "you@example.test"},
                follow_redirects=False,
            )

    assert list(staging_dir(services.config).iterdir()) == []


def test_an_attachment_token_that_is_not_ours_is_not_opened() -> None:
    """A token addresses a file; it is not a path to one."""
    from pony.web.compose import staged_path

    services, _index = _services("compose-token")
    config = services.config

    assert staged_path(config, "../../etc/passwd") is None
    assert staged_path(config, "not-hex-at-all") is None
    assert staged_path(config, "0123456789abcdef") is None


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
                "forward_of": _ref(original),
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
                "forward_of": _ref(999),
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
    from pony.mail.domain import MessageFlag

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
                "answering": _ref(original.message_ref.id),
            },
        )

    answered = index.get_message(message_ref=original.message_ref)
    assert answered is not None
    assert MessageFlag.ANSWERED in answered.local_flags


def test_saving_a_resumed_draft_leaves_one_copy() -> None:
    from pony.mail.domain import MessageStatus

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
                "source": _ref(original.message_ref.id),
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
    config = dataclasses.replace(
        make_test_config(accounts=(account,)),
        # The real thing declares one, and the staging directory hangs
        # off it; a config without one takes a different branch.
        data_dir=paths.data_dir,
    )
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


def test_the_staging_directory_lives_under_the_data_dir_and_is_swept_once() -> None:
    """Where uploads wait, and what removes them when the process ends.

    The directory used to be a ``tempfile.mkdtemp`` outside everything
    ``PONY_ROOT`` relocates, removed by nothing; the lifetime that
    replaced it -- under ``data_dir``, with one ``atexit`` sweep per
    root however many applications are built -- had no test.
    """
    import pony.web.compose as compose_module

    services, _index, paths = _services_and_paths("compose-staging-lifetime")
    registered: list[tuple[Any, ...]] = []

    with (
        patch.object(compose_module, "_SWEPT", set()),
        patch.object(
            compose_module,
            "atexit",
            type(
                "_FakeAtexit",
                (),
                {"register": staticmethod(lambda *a, **_kw: registered.append(a))},
            ),
        ),
    ):
        root = compose_module.staging_dir(services.config)
        again = compose_module.staging_dir(services.config)
        token, path = compose_module.stage_attachment(
            services.config, "notes.txt", b"hello"
        )

    assert root == paths.data_dir / "web-attachments"
    assert again == root
    assert root.is_dir()
    # One sweep for the root, not one per call.
    assert len(registered) == 1
    assert registered[0][1] == root
    # And it really is the sweep: running it takes the staged file with it.
    assert path.is_file()
    registered[0][0](*registered[0][1:])
    assert not root.exists()
    assert compose_module.staged_path(services.config, token) is None


def test_a_config_with_no_data_dir_stages_in_one_private_root() -> None:
    """A token staged by one request has to be findable by the next.

    Reachable only by building a config in code, but the fallback has to
    be the *same* directory every time or the second request cannot find
    what the first wrote.
    """
    import pony.web.compose as compose_module

    services, _index, _paths = _services_and_paths("compose-staging-private")
    config = dataclasses.replace(services.config, data_dir=None)

    first = compose_module.staging_dir(config)
    token, path = compose_module.stage_attachment(config, "notes.txt", b"hello")
    second = compose_module.staging_dir(config)

    assert first == second
    assert compose_module.staged_path(config, token) == path
    compose_module.release_staged(config, [path])
    assert compose_module.staged_path(config, token) is None


def test_a_ten_megabyte_attachment_survives_the_whole_round_trip() -> None:
    """The size that is accepted, not only the size that is refused.

    The cap at 25 MB has a test on both sides of it; nothing carried a
    large file all the way through staging, the MIME build and the
    base64 transfer encoding to check that what arrives at SMTP is the
    bytes that were uploaded.  The body is pseudo-random rather than a
    repeated byte, so a truncation or a re-encode shows up.
    """
    import hashlib
    import random

    payload = random.Random(20261012).randbytes(10 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    services, _index = _services("compose-attachment-ten-mb")
    sent: list[dict[str, object]] = []

    with (
        patch.object(delivery, "smtp_send", lambda **kwargs: sent.append(kwargs)),
        _client(services) as client,
    ):
        staged = client.post("/compose/attachment?name=big.bin", content=payload)
        assert staged.status_code == 200, staged.text
        token = staged.json()["token"]
        posted = client.post(
            "/compose",
            data={
                "account": "acct",
                "to": "you@example.test",
                "subject": "Ten megabytes",
                "body": "Attached.",
                "attachment": token,
            },
            follow_redirects=False,
        )

    assert posted.status_code in (302, 303)
    (call,) = sent
    parts = [
        part
        for part in call["msg"].walk()  # type: ignore[union-attr]
        if part.get_filename() == "big.bin"
    ]
    assert len(parts) == 1
    arrived = parts[0].get_payload(decode=True)
    assert len(arrived) == len(payload)
    assert hashlib.sha256(arrived).hexdigest() == digest
