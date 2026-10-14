"""A user's path through the browser front end, step by step.

The route tests check one request each.  These check the paths a user
walks, because the defects that survive per-route tests are the ones
*between* the steps: a message that opens but keeps its unread mark, a
form that holds its text after a refusal and then cannot be sent again,
a search that narrows the list and loses itself on the way into a
message, an event that is created and then cannot be found to delete.

Every step goes through what the previous page offered.  Links are read
out of the markup with :mod:`web_helpers`, forms are submitted with the
fields they were rendered with, and an action is posted the way
``server()`` in ``static/pony.js`` posts it — out of the page's own
``data-*`` and its rows.  A test that wrote its own URLs instead would
pass with every link on the page broken.

What is *not* exercised here is the script itself: the request each
press would make is reproduced in Python, and
``tests/test_web_dispatch.py`` pins the script to that shape.  The
script's own behaviour — the prompt, the mark toggling, the fragment
swap — needs a browser, which is ``tests/test_web_browser.py``,
opt-in through ``PONY_BROWSER_UI=1``.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from corpus import plain_text
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
from web_helpers import (
    attribute_values,
    dispatch,
    dispatch_agreeing,
    element_data,
    form_fields,
    form_target,
    link_by_id,
    link_to,
    links,
    page_data,
    row_ids,
    row_link,
    select_value,
)

from pony.mail import delivery
from pony.mail.domain import FolderRef, MessageFlag
from pony.web import WebServices, build_web_app

if TYPE_CHECKING:
    from pony.calendar_runtime import CalendarRuntime
    from pony.mail.index_store import SqliteIndexRepository

INBOX = FolderRef(account_name="acct", folder_name="INBOX")
ARCHIVE = FolderRef(account_name="acct", folder_name="Archive")
SENT = FolderRef(account_name="acct", folder_name="Sent")
MAIL = "/mail?account=acct&folder=INBOX"
ACCOUNT = "personal"
CALENDAR = "work"


def _second_message() -> bytes:
    """A second message, so a search has something to leave out."""
    return (
        b"From: bob@example.test\r\n"
        b"To: you@example.test\r\n"
        b"Subject: Invoice 4821\r\n"
        b"Date: Mon, 5 May 2025 09:00:00 +0000\r\n"
        b"Message-ID: <invoice@example.test>\r\n"
        b"\r\n"
        b"Payment is due on Friday.\r\n"
    )


def _third_message() -> bytes:
    """A third message, so the cursor can start somewhere else."""
    return (
        b"From: carol@example.test\r\n"
        b"To: you@example.test\r\n"
        b"Subject: Third thing\r\n"
        b"Date: Mon, 5 May 2025 11:00:00 +0000\r\n"
        b"Message-ID: <third@example.test>\r\n"
        b"\r\n"
        b"The last of the three.\r\n"
    )


def _invitation_mail() -> bytes:
    """A message carrying an invitation the reader will offer to answer."""
    start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    ics = "\r\n".join(
        (
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//tests//EN",
            "METHOD:REQUEST",
            "BEGIN:VEVENT",
            "UID:flow-invite@example.com",
            f"DTSTAMP:{start:%Y%m%dT%H%M%SZ}",
            f"DTSTART:{start:%Y%m%dT%H%M%SZ}",
            f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%SZ}",
            "SUMMARY:Design review",
            "ORGANIZER;CN=Bob:mailto:bob@example.com",
            "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:acct@example.com",
            "END:VEVENT",
            "END:VCALENDAR",
        ),
    )
    return (
        "From: bob@example.com\r\n"
        "To: acct@example.com\r\n"
        "Subject: Design review\r\n"
        'Content-Type: text/calendar; method=REQUEST; charset="utf-8"\r\n'
        "\r\n"
        f"{ics}\r\n"
    ).encode()


def _services(
    label: str,
    *,
    seed: tuple[bytes, ...] = (plain_text(),),
    calendar: bool = False,
) -> tuple[WebServices, SqliteIndexRepository, CalendarRuntime | None]:
    """A front end over real stores, with the calendar when asked for."""
    paths = make_tmp_paths(label)
    account = dataclasses.replace(
        make_test_account(paths, archive_folder="Archive"),
        sent_folder="Sent",
        drafts_folder="Drafts",
    )
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    for position, raw in enumerate(seed):
        seed_message(
            index=index,
            mirror=mirrors["acct"],
            folder=INBOX,
            raw=raw,
            message_id=f"<flow-{position}@example.com>",
        )
    runtime: CalendarRuntime | None = None
    if calendar:
        runtime = make_calendar_runtime(
            make_tmp_paths(f"{label}-cal"),
            account_name=ACCOUNT,
            calendar_name=CALENDAR,
        )
        (runtime.mirror.root / ACCOUNT / CALENDAR).mkdir(parents=True, exist_ok=True)
    services = WebServices(
        config=config,
        index=index,
        mirrors=mirrors,
        contacts=index,
        calendar=runtime,
    )
    return services, index, runtime


def _client(services: WebServices) -> TestClient:
    return TestClient(build_web_app(services))


def _refreshed_rows(client: TestClient, page: str) -> str:
    """The list fragment ``refresh()`` refetches after an action.

    Built from the page's own ``data-*``, which is what ``mailParams()``
    does, so this is the request the script makes rather than a URL
    written out again here.
    """
    data = page_data(page)
    return client.get(
        "/mail/rows",
        params={
            "account": data["account"],
            "folder": data["folder"],
            "q": data.get("query", ""),
            # The open message, so the fragment comes back with the
            # cursor where the page left it.
            "id": data.get("message", ""),
        },
    ).text


def _after_refresh(client: TestClient, page: str) -> str:
    """*page* with its list replaced, the way ``refresh()`` replaces it.

    The script swaps ``#rows`` for the fragment and leaves everything
    else — the ``<body>`` dataset above all — exactly where it was, so a
    press that follows an action is reading that same mixture.  A test
    that re-fetches the whole page instead would quietly paper over
    whatever the fragment forgot to carry.
    """
    import re

    fresh = _refreshed_rows(client, page)
    opening = re.search(r"<(ol|p)\b[^>]*\bid=\"rows\"[^>]*>", page)
    assert opening is not None, "the page has no list to replace"
    closing = page.index(f"</{opening.group(1)}>", opening.end())
    return page[: opening.start()] + fresh + page[closing + len(opening.group(1)) + 3 :]


# ----------------------------------------------------------------------
# Reading mail
# ----------------------------------------------------------------------


def test_opening_a_message_then_archiving_it_takes_it_out_of_the_folder() -> None:
    """The three steps of the commonest path, and what each one changes."""
    services, index, _calendar = _services("flow-archive")

    with _client(services) as client:
        folder = client.get(MAIL).text
        assert "Tuesday meeting confirmed" in folder
        assert 'class="row unread"' in folder

        # Step one: open the row, through the link the row carries.
        opened = client.get(row_link(folder)).text
        assert "Just a quick note to confirm" in opened
        assert page_data(opened)["message"] == row_ids(folder)[0]
        # The list the script would refresh into has lost the mark.
        assert "unread" not in _refreshed_rows(client, opened)

        # Step two: archive it, with the request a press on `A` makes.
        outcome = dispatch(client, opened, "archive")
        assert outcome.changed == 1
        assert outcome.texts == ("Archived to Archive.",)

        # Step three: the row is gone from the folder it was in.
        emptied = _refreshed_rows(client, opened)
        assert row_ids(emptied) == []
        assert "Tuesday meeting confirmed" not in emptied

    (summary,) = index.list_folder_message_summaries(folder=ARCHIVE)
    assert MessageFlag.SEEN in summary.local_flags
    (row,) = index.list_folder_messages(folder=ARCHIVE)
    # No UID: the next sync is what will move it on the server.
    assert row.uid is None


def test_a_second_action_still_acts_on_the_message_that_is_open() -> None:
    """The refreshed list must not move the cursor off the open message.

    Open the last of three, press ``u``, press ``A``.  The fragment used
    to come back with no selection in it at all, so the page put the
    cursor back on the first row and archived somebody else's mail —
    and Archive, Trash and Move are the three that cannot be taken
    back.  Asserted against the store, not against the markup.
    """
    services, index, _calendar = _services(
        "flow-open-id",
        seed=(plain_text(), _second_message(), _third_message()),
    )

    with _client(services) as client:
        folder = client.get(MAIL).text
        listed = row_ids(folder)
        assert len(listed) == 3
        # The one the cursor does *not* start on.
        wanted = listed[-1]
        opened = client.get(row_link(folder, wanted)).text
        assert page_data(opened)["message"] == wanted

        first = dispatch(client, opened, "mark_unread")
        assert first.changed == 1

        # What the page is looking at now: the same page, a new list.
        refreshed = _after_refresh(client, opened)
        # The fragment knows which row is open, so the cue survives too.
        assert f'data-id="{wanted}"' in refreshed
        assert 'aria-selected="true"' in refreshed
        assert any(
            "open" in value.split() for value in attribute_values(refreshed, "class")
        )

        second = dispatch(client, refreshed, "archive")
        assert second.changed == 1

    # The row that was open is the row that moved. `_move_in_place`
    # keeps the row, so the id is the one the page was showing.
    (archived,) = index.list_folder_message_summaries(folder=ARCHIVE)
    assert str(archived.message_ref.id) == wanted
    # And the other two are still where they were.
    assert len(index.list_folder_message_summaries(folder=INBOX)) == 2


def test_a_press_after_the_open_row_has_gone_moves_nobody_elses_mail() -> None:
    """The second press, once the row it acted on has left the folder.

    Archive takes the row out of the list, so there is nothing for the
    cursor to sit on; reading the list for a target then found the
    *first* row and archived that.  ``targets()`` asks the page what is
    open instead, so the second press refuses rather than moving the
    wrong mail.
    """
    services, index, _calendar = _services(
        "flow-gone-row",
        seed=(plain_text(), _second_message(), _third_message()),
    )

    with _client(services) as client:
        folder = client.get(MAIL).text
        listed = row_ids(folder)
        wanted = listed[-1]
        opened = client.get(row_link(folder, wanted)).text

        assert dispatch(client, opened, "archive").changed == 1
        refreshed = _after_refresh(client, opened)
        # The row it acted on is gone, and the first row is somebody
        # else's: this is the press that used to archive them.
        assert wanted not in row_ids(refreshed)

        again = dispatch(client, refreshed, "archive")

    assert again.changed == 0
    assert len(index.list_folder_message_summaries(folder=ARCHIVE)) == 1
    assert len(index.list_folder_message_summaries(folder=INBOX)) == 2


def test_marking_two_rows_archives_both_in_one_action() -> None:
    """What `m` `m` `A` does, over the ids the marks would hold."""
    services, index, _calendar = _services(
        "flow-marks",
        seed=(plain_text(), _second_message()),
    )

    with _client(services) as client:
        folder = client.get(MAIL).text
        marked = row_ids(folder)
        assert len(marked) == 2

        outcome = dispatch(client, folder, "archive", ids=marked)
        assert outcome.changed == 2

        assert row_ids(_refreshed_rows(client, folder)) == []

    assert len(index.list_folder_message_summaries(folder=ARCHIVE)) == 2


def test_searching_narrows_the_list_and_keeps_itself_through_a_message() -> None:
    """The query lives in the URL, so the back of a message still has it."""
    services, _index, _calendar = _services(
        "flow-search",
        seed=(plain_text(), _second_message()),
    )

    with _client(services) as client:
        folder = client.get(MAIL).text
        assert len(row_ids(folder)) == 2

        # The search field is a form of its own; submit it as rendered.
        method, action = form_target(folder, ".search-form")
        assert method == "get"
        fields = form_fields(folder, ".search-form")
        narrowed = client.get(action, params={**fields, "q": "invoice"}).text

        assert len(row_ids(narrowed)) == 1
        assert "Invoice 4821" in narrowed
        assert "Tuesday meeting confirmed" not in narrowed
        # The page says what it is showing, which is what the script
        # then posts and refreshes with.
        assert page_data(narrowed)["query"] == "invoice"
        assert 'value="invoice"' in narrowed

        # Into the message and back out: the search is still on.
        opened = client.get(row_link(narrowed)).text
        assert page_data(opened)["query"] == "invoice"
        assert len(row_ids(opened)) == 1
        # Every row beside the open message still carries the query, so
        # moving on through the results stays inside the search.
        assert "q=invoice" in row_link(opened)

        # Submitting it empty is how a search is left.
        full = client.get(action, params={**fields, "q": ""}).text
        assert len(row_ids(full)) == 2


# ----------------------------------------------------------------------
# Writing mail
# ----------------------------------------------------------------------


def test_writing_from_the_list_and_sending_files_the_sent_copy() -> None:
    services, index, _calendar = _services("flow-send")
    sent: list[dict[str, object]] = []

    with (
        patch.object(delivery, "smtp_send", lambda **kwargs: sent.append(kwargs)),
        _client(services) as client,
    ):
        folder = client.get(MAIL).text
        form_page = client.get(link_to(folder, "Write")).text
        assert page_data(form_page)["surface"] == "compose"

        fields = form_fields(form_page, "#compose-form")
        # The account is chosen for the user, not left to be typed.
        assert fields["account"] == "acct"
        method, action = form_target(form_page, "#compose-form")
        assert method == "post"

        done = client.post(
            action,
            data={
                **fields,
                "to": "you@example.test",
                "subject": "Lunch",
                "body": "At one?",
            },
        ).text

    # The form is gone, replaced by the list and a notice saying so.
    assert "Message sent to you@example.test" in done
    assert 'id="compose-form"' not in done
    assert page_data(done)["surface"] == "reader"

    (call,) = sent
    message = call["msg"]
    assert isinstance(message, Message)
    assert message["To"] == "you@example.test"
    assert message["Subject"] == "Lunch"
    # The Sent copy is filed, with no UID, for the next sync to push.
    (filed,) = index.list_folder_messages(folder=SENT)
    assert (filed.subject, filed.uid) == ("Lunch", None)


def test_a_refused_send_keeps_the_form_and_the_next_attempt_sends_it() -> None:
    """A refusal is not a lost message: the form comes back usable."""
    from pony.mail.smtp_sender import SMTPError

    services, index, _calendar = _services("flow-send-refused")
    sent: list[dict[str, object]] = []

    def _refuse(**_kwargs: object) -> None:
        message = "nobody answered"
        raise SMTPError(message)

    with _client(services) as client:
        form_page = client.get("/compose?mode=new&account=acct").text
        fields = {
            **form_fields(form_page, "#compose-form"),
            "to": "you@example.test",
            "subject": "Lunch",
            "body": "At one?",
        }

        with patch.object(delivery, "smtp_send", _refuse):
            refused = client.post("/compose", data=fields).text

        assert "nobody answered" in refused
        assert 'id="compose-form"' in refused
        # Still a compose surface, so the keys are still the form's.
        assert page_data(refused)["surface"] == "compose"
        # And still holding every word that was typed.
        kept = form_fields(refused, "#compose-form")
        assert kept["to"] == "you@example.test"
        assert kept["subject"] == "Lunch"
        assert kept["body"] == "At one?"
        assert not index.list_folder_messages(folder=SENT)

        # Pressing send again on that same form sends it.
        with patch.object(
            delivery,
            "smtp_send",
            lambda **kwargs: sent.append(kwargs),
        ):
            method, action = form_target(refused, "#compose-form")
            done = client.post(action, data=kept).text

    assert "Message sent to you@example.test" in done
    assert len(sent) == 1
    assert [row.subject for row in index.list_folder_messages(folder=SENT)] == ["Lunch"]


# ----------------------------------------------------------------------
# The calendar
# ----------------------------------------------------------------------


def test_the_calendar_walks_from_the_agenda_to_the_month_and_back() -> None:
    """Switching view keeps the date; stepping and stepping back returns."""
    services, _index, _runtime = _services("flow-calendar-nav", calendar=True)

    with _client(services) as client:
        agenda = client.get("/calendar").text
        assert page_data(agenda)["view"] == "agenda"
        today = page_data(agenda)["date"]

        month = client.get(link_to(agenda, "Month")).text
        assert page_data(month)["view"] == "month"
        assert page_data(month)["date"] == today
        # A month grid, not a list of the next few days.
        assert 'class="month"' in month

        later = client.get(link_by_id(month, "next-date")).text
        assert page_data(later)["view"] == "month"
        assert page_data(later)["date"] != today

        back = client.get(link_by_id(later, "prev-date")).text
        assert page_data(back)["date"] == today

        # And the agenda is one link away again, on the same day.
        returned = client.get(link_to(back, "Agenda")).text
        assert page_data(returned)["view"] == "agenda"
        assert page_data(returned)["date"] == today


def test_an_event_is_created_from_the_calendar_and_deleted_again() -> None:
    services, _index, runtime = _services("flow-event", calendar=True)
    assert runtime is not None

    with _client(services) as client:
        agenda = client.get("/calendar").text
        assert row_ids(agenda) == []

        # New event, on the day being viewed.
        blank = client.get(link_to(agenda, "New event")).text
        fields = form_fields(blank, "#event-form")
        assert fields["uid"] == ""
        assert fields["target"] == f"{ACCOUNT}/{CALENDAR}"
        assert fields["starts"]

        day = page_data(agenda)["date"]
        _method, action = form_target(blank, "#event-form")
        listed = client.post(
            action,
            data={
                **fields,
                "summary": "Design review",
                "starts": f"{day}T14:00",
                "ends": f"{day}T15:00",
                "location": "Room 2",
            },
        ).text

        # The agenda comes back with it on, and says what happened.
        assert "Created" in listed
        assert "Design review" in listed
        assert len(row_ids(listed)) == 1

        # Open it, as Enter or a tap does, and delete it from there.
        form = client.get(row_link(listed)).text
        assert form_fields(form, "#event-form")["summary"] == "Design review"
        button = element_data(form, "delete-event")
        assert button["uid"]
        # Deleting an event asks first, as it does in the terminal.
        asked = dispatch(
            client,
            form,
            "delete_event",
            uid=button["uid"],
            cal=button["target"],
        )
        assert asked.changed == 0
        assert "Design review" in str(asked.confirm)
        assert "Design review" in client.get("/calendar").text

        outcome = dispatch_agreeing(
            client,
            form,
            "delete_event",
            uid=button["uid"],
            cal=button["target"],
        )
        assert outcome.changed == 1

        emptied = client.get("/calendar").text
        assert "Design review" not in emptied

    (component,) = runtime.index.list_components_by_uid(button["uid"])
    assert component.local_status.value == "trashed"


# ----------------------------------------------------------------------
# Invitations
# ----------------------------------------------------------------------


def test_accepting_an_invitation_files_it_and_answers_the_organiser() -> None:
    """The answer and the calendar come out of the block, as the script reads them."""
    services, index, runtime = _services(
        "flow-invite",
        seed=(_invitation_mail(),),
        calendar=True,
    )
    assert runtime is not None
    posted = Mock()

    with (
        patch("pony.mail.smtp_sender.send_message", posted),
        _client(services) as client,
    ):
        folder = client.get(MAIL).text
        opened = client.get(row_link(folder)).text

        # The reader names what is proposed and offers the answers.
        assert "Design review" in opened
        assert set(attribute_values(opened, "data-answer")) >= {
            "accept",
            "decline",
            "file",
        }
        target = select_value(opened, "invitation-calendar")
        assert target == f"{ACCOUNT}/{CALENDAR}"

        outcome = dispatch(
            client,
            opened,
            "respond_invitation",
            answer="accept",
            calendar=target,
        )

    assert outcome.texts == (
        f"Filed in {CALENDAR}.",
        "Accepted; the organizer was told.",
    )
    assert list(runtime.index.list_components_by_uid("flow-invite@example.com"))
    assert posted.call_args.kwargs["msg"]["To"] == "bob@example.com"
    # The message itself is still where it was: answering does not file it away.
    assert len(index.list_folder_message_summaries(folder=INBOX)) == 1


# ----------------------------------------------------------------------
# Contacts
# ----------------------------------------------------------------------


def test_harvesting_a_folder_then_merging_two_of_the_contacts() -> None:
    """Across three surfaces: the reader harvests, the list merges."""
    services, index, _calendar = _services(
        "flow-contacts",
        seed=(plain_text(), _second_message()),
    )

    with _client(services) as client:
        folder = client.get(MAIL).text
        outcome = dispatch(client, folder, "harvest_contacts")
        assert outcome.changed >= 1

        # The contacts tab is a link away, and lists what was harvested.
        contacts = client.get(link_to(folder, "Contacts")).text
        assert page_data(contacts)["surface"] == "contacts"
        marked = row_ids(contacts)
        assert len(marked) >= 2

        # Merging is irreversible, so it asks; the page says yes.
        merged = dispatch_agreeing(client, contacts, "merge_marked", ids=marked[:2])
        assert merged.changed >= 1

        after = client.get("/contacts").text
        assert len(row_ids(after)) == len(marked) - 1

    assert index.list_all_contacts()


# ----------------------------------------------------------------------
# Every link on every page
# ----------------------------------------------------------------------


def test_every_link_the_front_end_offers_leads_somewhere() -> None:
    """A link that 404s is a dead end a route test cannot see.

    Each page's own links are followed one step, which is what catches a
    link built by hand instead of through :mod:`pony.web.urls`.
    """
    services, index, _runtime = _services(
        "flow-links",
        seed=(plain_text(), _second_message()),
        calendar=True,
    )
    message = index.list_folder_message_summaries(folder=INBOX)[0].message_ref.id
    starts = (
        MAIL,
        f"/message?account=acct&folder=INBOX&id={message}",
        "/compose?mode=new&account=acct&folder=INBOX",
        "/contacts",
        "/contacts/edit",
        "/calendar",
        "/calendar/event",
    )

    with _client(services) as client:
        surfaces = set(client.get("/keymap.json").json())
        seen: set[str] = set()
        for start in starts:
            page = client.get(start)
            assert page.status_code == 200, start
            for href in links(page.text):
                if href in seen:
                    continue
                seen.add(href)
                response = client.get(href)
                assert response.status_code == 200, href
                reached = page_data(response.text)
                if "surface" in reached:
                    assert reached["surface"] in surfaces, href

    # Not a vacuous pass: the pages really do link to each other.
    assert len(seen) > 15
