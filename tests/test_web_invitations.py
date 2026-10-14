"""Answering an invitation from the browser's reader.

The two operations are :mod:`pony.invitation_actions`, tested there; what
is checked here is the block the reader shows — what it says when there
is no calendar to file into, which answers it offers — and that pressing
one of them files the event and tells the organizer.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from starlette.testclient import TestClient
from tui_helpers import (
    make_calendar_runtime,
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
    seed_message,
)

from pony.domain import FolderRef
from pony.web import WebServices, build_web_app

if TYPE_CHECKING:
    from pony.calendar_runtime import CalendarRuntime
    from pony.index_store import SqliteIndexRepository

INBOX = FolderRef(account_name="acct", folder_name="INBOX")
ACCOUNT = "personal"
CALENDAR = "work"


def _ics(*, method: str = "REQUEST", organizer: bool = True) -> bytes:
    start = datetime.now(UTC) + timedelta(days=2)
    end = start + timedelta(hours=1)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Example//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        "UID:design-review@example.com",
        f"DTSTAMP:{start:%Y%m%dT%H%M%SZ}",
        f"DTSTART:{start:%Y%m%dT%H%M%SZ}",
        f"DTEND:{end:%Y%m%dT%H%M%SZ}",
        "SUMMARY:Design review",
        "LOCATION:Room 3",
        "SEQUENCE:0",
    ]
    if organizer:
        lines.append("ORGANIZER:mailto:bob@example.com")
    lines.extend(
        (
            "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:acct@example.com",
            "END:VEVENT",
            "END:VCALENDAR",
        ),
    )
    return ("\r\n".join(lines) + "\r\n").encode()


def _mail(ics: bytes) -> bytes:
    message = EmailMessage()
    message["From"] = "bob@example.com"
    message["To"] = "acct@example.com"
    message["Subject"] = "Invitation: Design review"
    message["Date"] = "Mon, 02 Mar 2026 09:00:00 +0000"
    message["Message-ID"] = "<invite-web@example.com>"
    message.set_content("Please join.")
    message.add_alternative(
        ics.decode(),
        subtype="calendar",
        params={"method": "REQUEST"},
    )
    return message.as_bytes()


def _calendar(label: str) -> CalendarRuntime:
    runtime = make_calendar_runtime(
        make_tmp_paths(label),
        account_name=ACCOUNT,
        calendar_name=CALENDAR,
    )
    (runtime.mirror.root / ACCOUNT / CALENDAR).mkdir(parents=True, exist_ok=True)
    return runtime


def _services(
    label: str,
    *,
    ics: bytes | None = None,
    calendar: CalendarRuntime | None = None,
) -> tuple[WebServices, SqliteIndexRepository]:
    paths = make_tmp_paths(label)
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=INBOX,
        raw=_mail(ics if ics is not None else _ics()),
        message_id="<invite-web@example.com>",
    )
    return (
        WebServices(
            config=config,
            index=index,
            mirrors=mirrors,
            contacts=index,
            calendar=calendar,
        ),
        index,
    )


def _client(services: WebServices) -> TestClient:
    return TestClient(build_web_app(services))


def _message_url(index: SqliteIndexRepository) -> str:
    (summary,) = index.list_folder_message_summaries(folder=INBOX)
    return "/message?" + urlencode(
        {"account": "acct", "folder": "INBOX", "id": summary.message_ref.id},
    )


def _message_id(index: SqliteIndexRepository) -> str:
    (summary,) = index.list_folder_message_summaries(folder=INBOX)
    return str(summary.message_ref.id)


# ----------------------------------------------------------------------
# The block in the reader
# ----------------------------------------------------------------------


def test_the_reader_names_what_the_invitation_proposes() -> None:
    services, index = _services("web-invite", calendar=_calendar("web-invite-cal"))

    with _client(services) as client:
        body = client.get(_message_url(index)).text

    assert "Invitation: Design review" in body
    assert "Room 3" in body
    assert "bob@example.com" in body
    # Three answers and a calendar to file it in.
    for answer in ("accept", "decline", "tentative", "file"):
        assert f'data-answer="{answer}"' in body
    assert f'value="{ACCOUNT}/{CALENDAR}"' in body


def test_an_invitation_is_not_also_offered_as_a_file_to_import() -> None:
    """One set of bytes, one offer.

    A ``text/calendar`` part counts as an attachment, so the same
    invitation was offered twice — once to answer and once to import —
    and importing files the event without answering the organizer,
    which is the half that matters.
    """
    services, index = _services("web-invite-once", calendar=_calendar("web-once-cal"))

    with _client(services) as client:
        body = client.get(_message_url(index)).text

    # The answers are there, so the invitation really is answerable.
    assert 'data-answer="accept"' in body
    # And the duplicate is not.
    assert 'data-action="import_ics"' not in body


def test_a_calendar_predicate_is_the_one_invitation_py_owns() -> None:
    """Two answers to "can the calendar read this?" is one too many.

    The web half had a four-member set of its own where
    ``pony.invitation`` had two, so an invitation sent as
    ``text/x-vcalendar`` got the generic Import button and no answers.
    Both spellings live in that constant now and the web half is that
    constant, so the two cannot drift apart again.
    """
    from pony.invitation import CALENDAR_CONTENT_TYPES
    from pony.web.mail import _CALENDAR_TYPES

    assert _CALENDAR_TYPES == CALENDAR_CONTENT_TYPES
    assert "text/x-vcalendar" in _CALENDAR_TYPES


def test_an_invitation_with_no_calendar_configured_says_so() -> None:
    services, index = _services("web-invite-no-calendar")

    with _client(services) as client:
        body = client.get(_message_url(index)).text

    assert "No calendar configured, so this cannot be filed." in body
    assert 'data-answer="accept"' not in body


def test_an_invitation_with_no_calendars_yet_says_to_sync() -> None:
    paths = make_tmp_paths("web-invite-empty-cal")
    runtime = make_calendar_runtime(paths, account_name=ACCOUNT, calendar_name=CALENDAR)
    services, index = _services("web-invite-empty", calendar=runtime)

    with _client(services) as client:
        body = client.get(_message_url(index)).text

    assert "No calendars yet" in body


def test_an_announcement_with_no_organizer_offers_only_filing() -> None:
    services, index = _services(
        "web-invite-anonymous",
        ics=_ics(organizer=False),
        calendar=_calendar("web-invite-anonymous-cal"),
    )

    with _client(services) as client:
        body = client.get(_message_url(index)).text

    assert 'data-answer="file"' in body
    assert 'data-answer="accept"' not in body


# ----------------------------------------------------------------------
# Answering
# ----------------------------------------------------------------------


def test_accepting_files_the_event_and_answers_the_organizer() -> None:
    runtime = _calendar("web-invite-accept-cal")
    services, index = _services("web-invite-accept", calendar=runtime)
    sent = Mock()

    with patch("pony.smtp_sender.send_message", sent), _client(services) as client:
        response = client.post(
            "/action/respond_invitation",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": _message_id(index),
                "answer": "accept",
                "calendar": f"{ACCOUNT}/{CALENDAR}",
            },
        )

    texts = [notice["text"] for notice in response.json()["notices"]]
    assert texts == ["Filed in work.", "Accepted; the organizer was told."]
    assert list(runtime.index.list_components_by_uid("design-review@example.com"))
    assert sent.call_args.kwargs["msg"]["To"] == "bob@example.com"


def test_filing_only_tells_nobody() -> None:
    runtime = _calendar("web-invite-file-cal")
    services, index = _services("web-invite-file", calendar=runtime)
    sent = Mock()

    with patch("pony.smtp_sender.send_message", sent), _client(services) as client:
        response = client.post(
            "/action/respond_invitation",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": _message_id(index),
                "answer": "file",
                "calendar": f"{ACCOUNT}/{CALENDAR}",
            },
        )

    assert [n["text"] for n in response.json()["notices"]] == ["Filed in work."]
    assert not sent.called


def test_answering_without_a_calendar_chosen_is_refused() -> None:
    services, index = _services(
        "web-invite-no-target",
        calendar=_calendar("web-invite-no-target-cal"),
    )

    with _client(services) as client:
        response = client.post(
            "/action/respond_invitation",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": _message_id(index),
                "answer": "accept",
            },
        )

    assert response.json()["notices"][0]["text"] == (
        "Choose a calendar to file this in."
    )


def test_an_answer_that_is_not_one_of_the_four_is_refused() -> None:
    services, index = _services(
        "web-invite-bad-answer",
        calendar=_calendar("web-invite-bad-answer-cal"),
    )

    with _client(services) as client:
        response = client.post(
            "/action/respond_invitation",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": _message_id(index),
                "answer": "maybe-later",
                "calendar": f"{ACCOUNT}/{CALENDAR}",
            },
        )

    assert "'maybe-later' is not an answer." in response.json()["notices"][0]["text"]


def test_answering_with_no_calendar_configured_says_so() -> None:
    services, index = _services("web-invite-answer-no-calendar")

    with _client(services) as client:
        response = client.post(
            "/action/respond_invitation",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": _message_id(index),
                "answer": "accept",
            },
        )

    assert "No calendar configured" in response.json()["notices"][0]["text"]


def test_answering_with_nothing_selected_says_to_open_a_message() -> None:
    services, _index = _services(
        "web-invite-nothing-open",
        calendar=_calendar("web-invite-nothing-open-cal"),
    )

    with _client(services) as client:
        response = client.post(
            "/action/respond_invitation",
            data={"account": "acct", "folder": "INBOX"},
        )

    assert response.json()["notices"][0]["text"] == "Open the message first."


def test_a_message_that_carries_no_invitation_says_that() -> None:
    from corpus import plain_text

    paths = make_tmp_paths("web-invite-plain")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=INBOX,
        raw=plain_text(),
        message_id="<plain-web@example.com>",
    )
    services = WebServices(
        config=config,
        index=index,
        mirrors=mirrors,
        calendar=_calendar("web-invite-plain-cal"),
    )

    with _client(services) as client:
        page = client.get(_message_url(index))
        response = client.post(
            "/action/respond_invitation",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": _message_id(index),
                "answer": "file",
                "calendar": f"{ACCOUNT}/{CALENDAR}",
            },
        )

    # No block in the reader, and nothing to answer if asked anyway.
    assert "invitation-answers" not in page.text
    assert response.json()["notices"][0]["text"] == (
        "This message carries no invitation."
    )


def test_a_reply_from_an_account_that_cannot_send_is_reported() -> None:
    runtime = _calendar("web-invite-nosend-cal")
    services, index = _services("web-invite-nosend", calendar=runtime)
    nothing_sends = dataclasses.replace(
        services,
        config=dataclasses.replace(services.config, accounts=()),
    )

    with _client(nothing_sends) as client:
        response = client.post(
            "/action/respond_invitation",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": _message_id(index),
                "answer": "accept",
                "calendar": f"{ACCOUNT}/{CALENDAR}",
            },
        )

    texts = [notice["text"] for notice in response.json()["notices"]]
    assert texts[-1] == "No account configured for sending — nothing was answered."
