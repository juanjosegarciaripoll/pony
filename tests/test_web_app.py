"""The browser front end, over real stores and a real ASGI client.

Every route here is a thin shell over code that already has its own
tests: the actions in :mod:`pony.mail.mailbox_actions`, the row projections
in :mod:`pony.mail.views`, the keymap in :mod:`pony.mail.keymap`.  So these check
what is genuinely new — the routing, the escaping, the token gate, how a
hostile body is served, and that an action reached over HTTP is the same
call the terminal makes.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from corpus import (
    authenticated_message,
    html_only,
    multipart_mixed_attachment,
    plain_text,
)
from starlette.testclient import TestClient
from tui_helpers import (
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
    seed_message,
)
from web_helpers import form_fields, link_to

from pony.keys import KeyBinding, web_key
from pony.mail.domain import FolderRef, MessageFlag, MessageStatus
from pony.mail.views import ROW_BATCH
from pony.web import WebServices, build_web_app
from pony.web.app import CLIENT_ACTIONS, SERVER_ACTIONS, TOKEN_COOKIE
from pony.web.syncing import BACKGROUND_MESSAGE, PLANNING_MESSAGE, SyncProgress

if TYPE_CHECKING:
    from pony.mail.index_store import SqliteIndexRepository

INBOX = FolderRef(account_name="acct", folder_name="INBOX")
JS = Path(__file__).resolve().parents[1] / "src/pony/web/static/pony.js"
CSS = Path(__file__).resolve().parents[1] / "src/pony/web/static/pony.css"


def _selectors(css: str) -> list[str]:
    """Every selector the stylesheet declares, one per entry."""
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found: list[str] = []
    for block in bare.split("}"):
        head = block.split("{")[0].strip()
        if not head or head.startswith("@"):
            continue
        found.extend(part.strip() for part in head.split(","))
    return [selector for selector in found if selector]


def _services(
    label: str,
    *,
    token: str | None = None,
    seed: tuple[bytes, ...] = (plain_text(),),
    archive_folder: str | None = "Archive",
) -> tuple[WebServices, SqliteIndexRepository]:
    paths = make_tmp_paths(label)
    account = make_test_account(paths, archive_folder=archive_folder)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    for position, raw in enumerate(seed):
        seed_message(
            index=index,
            mirror=mirrors["acct"],
            folder=INBOX,
            raw=raw,
            message_id=f"<seed-{position}@example.com>",
        )
    services = WebServices(
        config=config,
        index=index,
        mirrors=mirrors,
        contacts=index,
        token=token,
    )
    return services, index


def _client(services: WebServices) -> TestClient:
    return TestClient(build_web_app(services))


def _only_message(index: SqliteIndexRepository) -> int:
    (summary,) = index.list_folder_message_summaries(folder=INBOX)
    return summary.message_ref.id


def _mail(**params: str | int) -> str:
    return "/mail?" + urlencode({"account": "acct", "folder": "INBOX", **params})


def _message(message_id: int, *, path: str = "/message", **extra: str | int) -> str:
    fields = {"account": "acct", "folder": "INBOX", "id": message_id, **extra}
    return f"{path}?{urlencode(fields)}"


# ----------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------


def test_the_root_opens_the_first_folder() -> None:
    services, _index = _services("web-root")

    with _client(services) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/mail?account=acct&folder=INBOX"


def test_the_root_says_so_when_there_is_no_mail_yet() -> None:
    services, _index = _services("web-root-empty", seed=())
    # A fresh maildir still synthesises INBOX, so empty means empty of
    # folders: take the mirrors away to get there.
    bare = dataclasses.replace(services, mirrors={})

    with _client(bare) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Press g to fetch some mail." in response.text


def test_a_folder_whose_name_has_a_slash_in_it_is_addressable() -> None:
    """A path segment could not hold one, which is why these are queries."""
    services, index = _services("web-nested-folder")
    nested = FolderRef(account_name="acct", folder_name="INBOX/2024")
    services.mirrors["acct"].create_folder(
        account_name="acct",
        folder_name="INBOX/2024",
    )
    seed_message(
        index=index,
        mirror=services.mirrors["acct"],
        folder=nested,
        raw=plain_text(),
        message_id="<nested@example.com>",
    )

    with _client(services) as client:
        response = client.get("/mail?account=acct&folder=INBOX%2F2024")

    assert response.status_code == 200
    assert 'data-folder="INBOX/2024"' in response.text
    assert "Tuesday meeting confirmed" in response.text


def test_a_folder_page_lists_its_messages_under_column_headings() -> None:
    services, _index = _services("web-folder")

    with _client(services) as client:
        response = client.get(_mail())

    assert response.status_code == 200
    body = response.text
    # The row projection's fields, and the tree the TUI renders too.
    assert "Tuesday meeting confirmed" in body
    assert 'data-folder="INBOX"' in body
    # Headings over the columns they name, as the terminal has.
    assert '<span class="date">Date</span>' in body
    assert '<span class="who">From</span>' in body
    assert '<span class="subject">Subject</span>' in body
    # Unread mail is marked as such rather than every read row being muted.
    assert 'class="row unread"' in body


def test_a_page_is_a_full_screen_application_not_a_document() -> None:
    """Panes that scroll on their own, and a footer of keys."""
    services, _index = _services("web-layout")

    with _client(services) as client:
        body = client.get(_mail()).text

    assert 'id="panes"' in body
    assert 'id="folders"' in body
    assert 'class="pane-body"' in body
    assert 'id="keys"' in body
    assert "<kbd>A</kbd>" in body


def test_opening_a_message_marks_it_read_and_shows_the_text() -> None:
    """No frame: the reader shows what `render_message` produced."""
    services, index = _services("web-message")
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.get(_message(message_id))

    assert response.status_code == 200
    body = response.text
    assert "<iframe" not in body
    assert '<pre class="text">' in body
    assert "Just a quick note to confirm" in body
    # The HTML version is one link away, in a tab of its own.
    assert "/message/body?" in body.replace("&amp;", "&")
    assert 'target="_blank"' in body
    (summary,) = index.list_folder_message_summaries(folder=INBOX)
    assert MessageFlag.SEEN in summary.local_flags


def test_nothing_but_a_reader_marks_a_message_read() -> None:
    """``GET /message`` writes, which is right for a person and wrong else.

    A prefetch, a link-preview bot and a cross-site ``<img src>`` all
    marked mail read, and the browser labels all three.
    """
    services, index = _services("web-message-prefetch")
    message_id = _only_message(index)

    with _client(services) as client:
        for headers in (
            {"sec-purpose": "prefetch;anonymous-client-ip"},
            {"sec-purpose": "prefetch;prerender"},
            {"sec-fetch-site": "cross-site"},
        ):
            answered = client.get(_message(message_id), headers=headers)
            assert answered.status_code == 200, headers
            (summary,) = index.list_folder_message_summaries(folder=INBOX)
            assert MessageFlag.SEEN not in summary.local_flags, headers

        client.get(_message(message_id), headers={"sec-fetch-site": "same-origin"})

    (summary,) = index.list_folder_message_summaries(folder=INBOX)
    assert MessageFlag.SEEN in summary.local_flags


def test_a_link_in_a_message_body_is_clickable_but_still_text() -> None:
    hostile = plain_text().replace(
        b"The room is booked from 14:00 to 15:30.",
        b"Notes at https://example.test/a?b=1 or mail ada@example.test",
    )
    services, index = _services("web-links", seed=(hostile,))

    with _client(services) as client:
        body = client.get(_message(_only_message(index))).text

    assert 'href="https://example.test/a?b=1"' in body
    # A bare address is text in both front ends: the renderer links a
    # `mailto:` and an anchor, and deliberately not every address in a
    # quoted header.
    assert 'href="mailto:ada@example.test"' not in body
    assert "ada@example.test" in body


def test_a_message_body_is_served_with_everything_switched_off() -> None:
    """Mail is hostile input; the HTML version may run or fetch nothing."""
    services, index = _services("web-body", seed=(html_only(),))
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.get(_message(message_id, path="/message/body"))

    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    # Inline images are already data URIs, so that is all img-src allows.
    assert "img-src data:" in policy
    assert response.headers["referrer-policy"] == "no-referrer"


def _phishing_message() -> bytes:
    """A message whose HTML part asks for a password.

    A script and a remote image are in here too, so one fixture covers
    the whole policy: the form and the ``<base>`` are the two that do
    *not* inherit from ``default-src``.
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    message = MIMEMultipart("alternative")
    message["From"] = "security@bank.test"
    message["To"] = "you@example.com"
    message["Subject"] = "Confirm your password"
    message["Date"] = "Mon, 6 Oct 2025 09:00:00 +0000"
    message["Message-ID"] = "<phish@bank.test>"
    message.attach(
        MIMEText(
            "<html><head><base href='https://bank.test/'></head><body>"
            "<form id='steal' action='https://bank.test/collect' method='post'>"
            "<input name='pw' type='password'>"
            "<button type='submit'>Confirm</button></form>"
            "<script>document.title='run'</script>"
            "<img src='https://bank.test/pixel.gif'>"
            "</body></html>",
            "html",
            "utf-8",
        ),
    )
    return message.as_bytes()


def test_a_form_in_a_message_has_nowhere_to_post_and_nothing_to_rebase() -> None:
    """``form-action`` and ``base-uri`` do not inherit from ``default-src``.

    Which is how a ``<form action=…><input name=pw>`` in a `text/html`
    message came to submit successfully, from Pony's own origin, one tab
    from the mailbox.  The renderer passes the markup through, so the
    header is what has to say no.
    """
    services, index = _services("web-body-phish", seed=(_phishing_message(),))
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.get(_message(message_id, path="/message/body"))

    # The renderer really does pass the form through — the policy is not
    # being asked about markup that was already stripped.
    assert "<form" in response.text
    policy = response.headers["content-security-policy"]
    for directive in (
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ):
        assert directive in policy, directive
    # And a `sandbox`, which is the only thing that stops a
    # `<meta http-equiv="refresh">` taking the tab somewhere else: no
    # directive covers one, because it is not a fetch.  The two tokens
    # it gives back are what a link needs; `tests/test_html_policy.py`
    # has the measurement, and checks them in a real browser.
    assert "sandbox allow-popups allow-popups-to-escape-sandbox" in policy


def test_the_reader_states_who_vouched_for_the_message() -> None:
    """Beside `From`, because it is a statement about `From`.

    Pony checks nothing itself: the receiving server did, and wrote its
    answer in `Authentication-Results`.  A signature that passes for a
    domain other than the one in `From` is the case worth marking, so
    the class on the value says which of the three it is.
    """
    services, index = _services("web-auth", seed=(authenticated_message(),))
    message_id = _only_message(index)

    with _client(services) as client:
        page = client.get(_message(message_id)).text

    assert "sender-check sender-passed" in page
    assert "\U0001f512" in page
    assert "example.com confirmed by dkim+dmarc" in page
    # The padlock is read as "safe", so the panel says what it is not.
    assert "says nothing about whether its owner is honest" in page


def test_the_reader_marks_a_signature_from_another_domain() -> None:
    services, index = _services(
        "web-auth-unaligned",
        seed=(
            authenticated_message(results="mx; dkim=pass header.d=bulk-sender.example"),
        ),
    )
    message_id = _only_message(index)

    with _client(services) as client:
        page = client.get(_message(message_id)).text

    assert "not by example.com" in page
    assert "sender-check sender-unaligned" in page
    assert "\U0001f513" in page


def test_an_unchecked_message_gets_an_open_padlock() -> None:
    """Absent is not the same as unchecked, so the lock is always there."""
    services, index = _services("web-auth-silent")
    message_id = _only_message(index)

    with _client(services) as client:
        page = client.get(_message(message_id)).text

    assert "sender-check sender-unknown" in page
    assert "\U0001f513" in page
    assert "sender-check sender-passed" not in page


async def test_a_slow_page_does_not_stop_the_other_tab() -> None:
    """Rendering ran on the event loop, so one page froze the server.

    8,000 rows cost 618 ms and 2.4 MB, and `refresh()` asks for the
    whole list again after every action — during which the other tab,
    an upload in flight and the sync's own requests all waited. The
    render is the thread pool's work now, so a slow page is this
    request's wait and nobody else's.
    """
    import asyncio
    import threading

    import httpx

    from pony.web import mail as mail_view

    services, _index = _services("web-threaded-render")
    rendering = threading.Event()
    finish = threading.Event()
    real_page = mail_view.mail_page

    def _slow(*args: object, **kwargs: object) -> str:
        rendering.set()
        assert finish.wait(10), "the quick request never came back"
        return real_page(*args, **kwargs)  # type: ignore[arg-type]

    app = build_web_app(services)
    transport = httpx.ASGITransport(app=app)
    with patch.object(mail_view, "mail_page", _slow):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://pony.test",
        ) as client:
            slow = asyncio.ensure_future(client.get(_mail()))
            # Waited for off the loop, so a render that blocked it would
            # never let this resume.
            assert await asyncio.to_thread(rendering.wait, 10)
            quick = await client.get("/keymap.json")
            finish.set()
            rendered = await slow

    assert quick.status_code == 200
    assert rendered.status_code == 200
    assert "Tuesday meeting confirmed" in rendered.text


def test_a_message_can_be_downloaded_as_it_arrived() -> None:
    services, index = _services("web-raw")
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.get(_message(message_id, path="/message/raw"))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("message/rfc822")
    assert b"Subject: Tuesday meeting confirmed" in response.content
    # Named after the message, as the terminal's save dialog proposes:
    # every download was `message.eml`, so three saved messages were
    # told apart by the browser's own numbering and nothing else.
    disposition = response.headers["content-disposition"]
    assert "tuesday-meeting-confirmed.eml" in disposition
    assert "message.eml" not in disposition


def test_an_attachment_is_served_as_a_download_that_cannot_be_sniffed() -> None:
    services, index = _services(
        "web-attachment",
        seed=(multipart_mixed_attachment(),),
    )
    message_id = _only_message(index)

    with _client(services) as client:
        page = client.get(_message(message_id)).text
        response = client.get(_message(message_id, path="/message/attachment", n=1))

    assert "q1-report.pdf" in page
    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 fake pdf content"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'filename="q1-report.pdf"' in response.headers["content-disposition"]


def test_the_browser_serves_every_part_and_lists_the_attachments() -> None:
    """``0`` and ``T`` are served and named; the list is the files.

    Both are reachable by name, which is what makes `S` then `0` and a
    click the same operation -- but listing them put two rows naming
    the open message above every attachment, which is what the reader
    is looking at the list for.
    """
    services, index = _services(
        "web-parts",
        seed=(multipart_mixed_attachment(),),
    )
    message_id = _only_message(index)

    with _client(services) as client:
        page = client.get(_message(message_id)).text
        whole = client.get(_message(message_id, path="/message/attachment", n=0))
        text = client.get(_message(message_id, path="/message/attachment", n="T"))
        lowercase = client.get(_message(message_id, path="/message/attachment", n="t"))
        nonsense = client.get(_message(message_id, path="/message/attachment", n="x"))

    assert "q1-report.pdf" in page
    assert ".eml" not in page
    assert "the message itself" not in page
    assert "the body as text" not in page

    assert whole.status_code == 200
    assert whole.headers["content-type"].startswith("message/rfc822")
    assert b"Subject:" in whole.content

    assert text.status_code == 200
    assert text.headers["content-type"].startswith("text/plain")
    assert ".txt" in text.headers["content-disposition"]
    assert lowercase.content == text.content

    assert nonsense.status_code == 400


def test_a_typed_selection_names_several_parts_to_download() -> None:
    """``S`` then ``0,T`` in the browser, as in the terminal.

    The dispatcher sent what was typed to ``/message/attachment?n=``
    as one part name, so every form the prompt advertises but a bare
    ``0`` navigated the application away to a 400 page.  The grammar
    is parsed here instead, and the answer says what to fetch.
    """
    services, index = _services(
        "web-select",
        seed=(multipart_mixed_attachment(),),
    )
    message_id = _only_message(index)

    with _client(services) as client:
        chosen = client.get(_message(message_id, path="/message/parts", select="0,T"))
        everything = client.get(_message(message_id, path="/message/parts", select="*"))

    assert chosen.status_code == 200
    parts = chosen.json()["parts"]
    assert [part["key"] for part in parts] == ["0", "T"]
    assert parts[0]["filename"].endswith(".eml")
    assert parts[1]["filename"].endswith(".txt")
    assert parts[0]["url"].endswith("&n=0")
    # Every part is named, listed or not: the page stopped showing 0
    # and T, not serving them.
    assert all(part["filename"] for part in parts)

    assert [part["key"] for part in everything.json()["parts"]] == ["0", "T", "1"]


def test_a_selection_that_names_nothing_is_answered_not_navigated_to() -> None:
    """The reader keeps their page, and is told what this message has.

    A 400 with the help in it, because the dispatcher shows it as a
    notice -- the bare "Which part?" page the old path reached had
    replaced the application by the time it was read.
    """
    services, index = _services(
        "web-select-bad",
        seed=(multipart_mixed_attachment(),),
    )
    message_id = _only_message(index)

    with _client(services) as client:
        nonsense = client.get(_message(message_id, path="/message/parts", select="x"))
        high = client.get(_message(message_id, path="/message/parts", select="3"))
        empty = client.get(_message(message_id, path="/message/parts", select=""))

    for response in (nonsense, high, empty):
        assert response.status_code == 400
        assert "parts" not in response.json()
    # One attachment on this message, and the message says so rather
    # than repeating the syntax at someone who mistyped a number.
    assert high.json()["error"] == (
        "Which parts? 0 is the message, T its text as a file, 1 is its attachment."
    )


def test_the_help_counts_the_attachments_it_has() -> None:
    """Three readings: several files, one, none."""
    services, index = _services("web-select-help", seed=(plain_text(),))
    message_id = _only_message(index)

    with _client(services) as client:
        none = client.get(_message(message_id, path="/message/parts", select="1"))

    assert none.status_code == 400
    assert "it has no attachments" in none.json()["error"]


def test_a_selection_for_a_message_that_is_not_there() -> None:
    services, _index = _services("web-select-missing")

    with _client(services) as client:
        response = client.get(_message(9999, path="/message/parts", select="0"))

    assert response.status_code == 404


def test_the_text_part_of_an_html_message_has_no_html_in_it() -> None:
    """What `T` means: the reading, not the markup."""
    services, index = _services("web-part-text-html", seed=(html_only(),))
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.get(_message(message_id, path="/message/attachment", n="T"))

    body = response.content.decode("utf-8")
    assert response.status_code == 200
    assert "<" not in body
    assert body.strip()


def test_a_long_folder_arrives_a_batch_at_a_time() -> None:
    """The page carries one batch and says where the next one starts.

    An unpaginated read of an 8,000-row folder was 618 ms and 2.4 MB of
    markup before this, all of it on the request the user was waiting
    on.  The reader has always drawn a batch at a time -- `ROW_BATCH`
    rows into its table, the event loop free between them -- and this is
    the browser doing the same.
    """
    services, _index = _services(
        "web-batched",
        seed=tuple(plain_text() for _ in range(ROW_BATCH + 5)),
    )

    with _client(services) as client:
        page = client.get("/mail?account=acct&folder=INBOX").text
        rest = client.get(
            f"/mail/rows?account=acct&folder=INBOX&offset={ROW_BATCH}"
        ).text
        beyond = client.get(
            f"/mail/rows?account=acct&folder=INBOX&offset={ROW_BATCH + 5}"
        ).text

    assert page.count("data-id=") == ROW_BATCH
    # And the link that a page with no script follows to the rest.
    assert f"offset={ROW_BATCH}" in page
    assert 'class="more"' in page

    assert rest.count("data-id=") == 5
    # The last batch is the last: nothing left to point at.
    assert 'class="more"' not in rest

    assert "Nothing here." in beyond


def test_the_second_page_of_a_long_folder_stands_on_its_own() -> None:
    """Following the More link with no script shows the next batch.

    The offset comes from a URL, so it is a number from outside: a
    hand-edited one that is not a number at all starts at the top
    rather than failing, which is what the reader does when asked for a
    row that is not there.
    """
    services, _index = _services(
        "web-batched-page",
        seed=tuple(plain_text() for _ in range(ROW_BATCH + 5)),
    )

    with _client(services) as client:
        second = client.get(f"/mail?account=acct&folder=INBOX&offset={ROW_BATCH}")
        nonsense = client.get("/mail?account=acct&folder=INBOX&offset=../etc")

    assert second.status_code == 200
    assert second.text.count("data-id=") == 5
    assert nonsense.status_code == 200
    assert nonsense.text.count("data-id=") == ROW_BATCH


def _cyrillic_attachment() -> bytes:
    """A message whose attachment is named in a script latin-1 has not got."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "ivan@example.test"
    message["To"] = "you@example.com"
    message["Subject"] = "The report"
    message["Date"] = "Mon, 6 Oct 2025 09:00:00 +0000"
    message["Message-ID"] = "<cyrillic@example.test>"
    message.set_content("See attached.")
    message.add_attachment(
        b"%PDF-1.4 fake",
        maintype="application",
        subtype="pdf",
        filename="документ.pdf",
    )
    return message.as_bytes()


def test_an_attachment_named_outside_latin_1_can_still_be_downloaded() -> None:
    """Starlette encodes a header as latin-1, so the name has to be encoded.

    ``документ.pdf`` raised ``UnicodeEncodeError`` and the attachment
    could not be fetched at all.  RFC 6266 answers it with two
    parameters: an ASCII one every client understands, and a
    percent-encoded one carrying the real name.
    """
    services, index = _services(
        "web-attachment-cyrillic", seed=(_cyrillic_attachment(),)
    )
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.get(_message(message_id, path="/message/attachment", n=1))

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 fake"
    disposition = response.headers["content-disposition"]
    # An ASCII fallback, with nothing in it that could end the parameter.
    assert 'filename="________.pdf"' in disposition
    # And the name the sender used, encoded.
    assert "filename*=UTF-8''%D0%B4%D0%BE%D0%BA%D1%83%D0%BC%D0%B5%D0%BD%D1%82.pdf" in (
        disposition
    )


def test_a_filename_cannot_end_the_header_parameter_it_sits_in() -> None:
    """A quote, a backslash or a newline in a filename is a header, not a name."""
    from pony.web.app import _content_disposition

    built = _content_disposition('ev"il\\\r\nX-Spoofed: yes.txt')

    # No line break to start a header of its own, and no quote or
    # backslash to end the parameter early — three characters `_quotable`
    # stripped and a fourth, the newline, that it let through.
    assert "\r" not in built
    assert "\n" not in built
    assert built.count('"') == 2
    assert "\\" not in built.partition("filename*=")[0]


def test_asking_for_an_attachment_that_is_not_there() -> None:
    services, index = _services("web-attachment-missing")
    message_id = _only_message(index)

    with _client(services) as client:
        assert (
            client.get(
                _message(message_id, path="/message/attachment", n=9),
            ).status_code
            == 404
        )
        assert (
            client.get(
                _message(message_id, path="/message/attachment", n="x"),
            ).status_code
            == 400
        )


def test_a_missing_message_is_a_404_not_a_crash() -> None:
    services, _index = _services("web-missing")

    with _client(services) as client:
        assert client.get(_message(999)).status_code == 404
        assert client.get(_message(999, path="/message/body")).status_code == 404


def test_a_message_whose_file_vanished_reports_that_it_is_gone() -> None:
    services, index = _services("web-gone")
    (summary,) = index.list_folder_message_summaries(folder=INBOX)
    message = index.get_message(message_ref=summary.message_ref)
    assert message is not None
    services.mirrors["acct"].delete_message(
        folder=INBOX,
        storage_key=message.storage_key,
    )

    with _client(services) as client:
        gone = client.get(_message(summary.message_ref.id, path="/message/body"))
        page = client.get(_message(summary.message_ref.id))

    assert gone.status_code == 410
    # The page itself still renders, and says what happened.
    assert page.status_code == 200
    assert "The message file is missing" in page.text


def test_the_fragments_a_refresh_puts_back_are_servable_on_their_own() -> None:
    """The page refreshes a part of itself rather than reloading."""
    services, _index = _services("web-fragments")

    with _client(services) as client:
        rows = client.get("/mail/rows?account=acct&folder=INBOX")
        panes = client.get("/mail/panes?account=acct&folder=INBOX")

    assert rows.status_code == 200
    assert rows.text.startswith("<ol")
    # Both panes in one response: the list, and the tree whose unread
    # counts are a projection of the same rows.
    assert panes.status_code == 200
    assert panes.text.startswith("<ol")
    assert 'id="folders"' in panes.text
    assert 'class="pane' in panes.text


def test_a_refresh_sees_one_state_of_the_folder_in_both_panes() -> None:
    """The rows and the counts come from one read, so they agree.

    Two requests meant the list could be repainted from after an
    action while the counts still said what they had said before it --
    a window the reader could see, and the reason this is one route.
    """
    services, index = _services(
        "web-one-state",
        seed=(plain_text(), plain_text()),
    )
    first, second = index.list_folder_message_summaries(folder=INBOX)

    with _client(services) as client:
        before = client.get("/mail/panes?account=acct&folder=INBOX").text
        acted = client.post(
            "/action/mark_read",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": str(first.message_ref.id),
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        after = client.get("/mail/panes?account=acct&folder=INBOX").text

    assert acted.status_code == 200
    # Two unread before, one after -- in the tree *and* in the rows,
    # out of the same response each time.
    assert '<span class="unread">2</span>' in _tree_half(before)
    assert '<span class="unread">1</span>' in _tree_half(after)
    assert _rows_half(after).count("unread") < _rows_half(before).count("unread")
    # The message that is still unread is still listed.
    assert str(second.message_ref.id) in _rows_half(after)


def _rows_half(markup: str) -> str:
    """The list, out of a combined refresh."""
    return markup.partition('<section id="folders"')[0]


def _tree_half(markup: str) -> str:
    """The folder tree, out of a combined refresh."""
    return markup.partition('<section id="folders"')[2]


def test_an_empty_folder_says_so() -> None:
    services, index = _services("web-empty-folder")
    (summary,) = index.list_folder_message_summaries(folder=INBOX)
    index.delete_message(message_ref=summary.message_ref)

    with _client(services) as client:
        assert "Nothing here." in client.get(_mail()).text


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------


def test_searching_narrows_the_list_the_way_the_reader_does() -> None:
    services, _index = _services("web-search", seed=(plain_text(), html_only()))

    with _client(services) as client:
        found = client.get(_mail(q="Tuesday"))
        missed = client.get(_mail(q="nonsense"))

    assert "Tuesday meeting confirmed" in found.text
    assert "Search: Tuesday" in found.text
    assert "Nothing found." in missed.text


# ----------------------------------------------------------------------
# Escaping
# ----------------------------------------------------------------------


def test_a_subject_full_of_markup_is_escaped() -> None:
    """Headers are attacker-controlled; the page must not believe them."""
    hostile = plain_text().replace(
        b"Subject: Tuesday meeting confirmed",
        b"Subject: <script>alert(1)</script>",
    )
    services, _index = _services("web-escaping", seed=(hostile,))

    with _client(services) as client:
        response = client.get(_mail())

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


def test_a_notice_carried_through_a_redirect_is_escaped_too() -> None:
    services, _index = _services("web-notice-escaping")

    with _client(services) as client:
        response = client.get(_mail(notice="<b>hi</b>", severity="warning"))

    assert "<b>hi</b>" not in response.text
    assert "&lt;b&gt;hi&lt;/b&gt;" in response.text
    assert 'class="notice warning"' in response.text


def test_a_notice_only_comes_from_ponys_own_redirect() -> None:
    """A `?notice=` from another page is that page's words, in our chrome.

    Correctly escaped, so it is a phishing surface rather than an XSS
    one -- and escaping is no answer to "Your session expired, sign in
    again" rendered with error styling one tab from the mailbox.
    """
    services, _index = _services("web-notice-origin")
    story = "Your session expired. Sign in again at evil.test"

    with _client(services) as client:
        hostile = client.get(
            _mail(notice=story, severity="error"),
            headers={"sec-fetch-site": "cross-site"},
        )
        ours = client.get(
            _mail(notice=story, severity="error"),
            headers={"sec-fetch-site": "same-origin"},
        )

    assert story not in hostile.text
    assert 'class="notice error"' not in hostile.text
    # Pony's own redirect still says what it did.
    assert story in ours.text


# ----------------------------------------------------------------------
# The keymap
# ----------------------------------------------------------------------


def test_the_keymap_is_served_per_surface() -> None:
    services, _index = _services("web-keymap")

    with _client(services) as client:
        keymaps = client.get("/keymap.json").json()

    assert set(keymaps) >= {"reader", "compose", "contacts", "calendar", "event"}
    reader = {entry["action"]: entry for entry in keymaps["reader"]}
    # The same table the terminal binds, so every reader key is here.
    assert reader["archive"]["key"] == "A"
    assert reader["archive"]["available"] is True
    assert reader["cursor_down"]["available"] is True
    # Composing and syncing are wired up.
    assert reader["compose_new"]["available"] is True
    assert reader["sync"]["available"] is True
    # Named keys come through in the browser's own spelling.
    assert reader["show_help"]["key"] == "F1"
    assert reader["mark_up"]["key"] == "shift+ArrowUp"
    # Opening the body in $EDITOR is not something a browser can do, and
    # the help says so rather than the page ignoring the press.
    compose = {entry["action"]: entry for entry in keymaps["compose"]}
    assert compose["edit_external"]["available"] is False
    # The calendar's own table, in the same shape. Several keys share
    # an action there, and a bare capital and its `shift+` spelling are
    # one key to a browser, so the served table carries it once.
    calendar: dict[str, list[str]] = {}
    for entry in keymaps["calendar"]:
        calendar.setdefault(entry["action"], []).append(entry["key"])
    assert calendar["view_month"] == ["M"]
    assert "t" in calendar["today"]
    by_action = {entry["action"]: entry for entry in keymaps["calendar"]}
    assert by_action["today"]["available"] is True
    assert by_action["select_span(3)"]["available"] is True


def test_every_key_the_help_calls_available_is_really_dispatched() -> None:
    """The help cannot promise what the dispatcher does not implement."""
    source = JS.read_text(encoding="utf-8")
    missing = [
        action
        for action in sorted(CLIENT_ACTIONS)
        if f"{action}:" not in source and f"clientActions.{action} =" not in source
    ]

    assert missing == []


def test_only_the_prompting_actions_live_on_both_sides() -> None:
    """A name means one thing, with one documented exception.

    Five actions need a word before they can run — a folder to move to,
    a name for a new folder, an answer to an invitation — so the browser
    asks for it and then posts to the action of the same name.
    Everything else is either local or a request, never both.
    """
    assert CLIENT_ACTIONS & set(SERVER_ACTIONS) == {
        "move",
        "copy",
        "new_folder",
        "respond_invitation",
        "delete_event",
    }


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------


def test_archiving_over_http_is_the_same_call_the_terminal_makes() -> None:
    services, index = _services("web-archive")
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.post(
            "/action/archive",
            data={"account": "acct", "folder": "INBOX", "id": str(message_id)},
        )

    assert response.status_code == 200
    assert response.json() == {
        "changed": 1,
        "notices": [{"text": "Archived to Archive.", "severity": "information"}],
    }
    assert list(index.list_folder_message_summaries(folder=INBOX)) == []
    archived = index.list_folder_messages(
        folder=FolderRef(account_name="acct", folder_name="Archive"),
    )
    # No UID: the next sync is what pushes the move to the server.
    assert [row.uid for row in archived] == [None]


def test_an_action_reports_its_refusal_rather_than_failing() -> None:
    services, index = _services("web-archive-refused", archive_folder=None)
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.post(
            "/action/archive",
            data={"account": "acct", "folder": "INBOX", "id": str(message_id)},
        )

    assert response.json() == {
        "changed": 0,
        "notices": [
            {
                "text": "No archive_folder configured for this account.",
                "severity": "error",
            },
        ],
    }
    assert len(list(index.list_folder_message_summaries(folder=INBOX))) == 1


@pytest.mark.parametrize(
    ("action", "check"),
    [
        ("trash", "trashed"),
        ("toggle_flagged", "flagged"),
        ("mark_unread", "unread"),
    ],
)
def test_the_local_mutations_are_offered_over_http(action: str, check: str) -> None:
    services, index = _services(f"web-action-{action}")
    message_id = _only_message(index)
    with _client(services) as client:
        # Open it first so "mark unread" has something to undo.
        client.get(_message(message_id))
        response = client.post(
            f"/action/{action}",
            data={"account": "acct", "folder": "INBOX", "id": str(message_id)},
        )

    assert response.status_code == 200
    assert response.json()["changed"] == 1
    (row,) = index.list_folder_messages(folder=INBOX)
    if check == "trashed":
        assert row.local_status is MessageStatus.TRASHED
    elif check == "flagged":
        assert MessageFlag.FLAGGED in row.local_flags
    else:
        assert MessageFlag.SEEN not in row.local_flags


def test_marking_a_whole_folder_read_needs_no_selection() -> None:
    services, index = _services("web-mark-all")

    with _client(services) as client:
        response = client.post(
            "/action/mark_all_read",
            data={"account": "acct", "folder": "INBOX"},
        )

    assert response.json()["notices"] == [
        {"text": "Marked 1 message as read.", "severity": "information"},
    ]
    (row,) = index.list_folder_messages(folder=INBOX)
    assert MessageFlag.SEEN in row.local_flags


def test_moving_and_copying_take_the_target_the_prompt_asked_for() -> None:
    services, index = _services("web-move")
    message_id = _only_message(index)
    services.mirrors["acct"].create_folder(account_name="acct", folder_name="Later")

    with _client(services) as client:
        copied = client.post(
            "/action/copy",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": str(message_id),
                "target": "acct/Later",
            },
        )
        moved = client.post(
            "/action/move",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": str(message_id),
                "target": "acct/Later",
            },
        )

    assert copied.json()["changed"] == 1
    assert moved.json()["changed"] == 1
    later = index.list_folder_messages(
        folder=FolderRef(account_name="acct", folder_name="Later"),
    )
    assert len(later) == 2
    assert list(index.list_folder_message_summaries(folder=INBOX)) == []


def test_a_move_with_no_target_says_how_to_name_one() -> None:
    services, index = _services("web-move-no-target")

    with _client(services) as client:
        response = client.post(
            "/action/move",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": str(_only_message(index)),
            },
        )

    assert response.json()["notices"][0]["text"] == (
        "Name the folder as account/folder."
    )


def test_a_move_to_a_folder_nobody_has_is_refused() -> None:
    """The prompt is free text; the terminal's picker is not.

    `target=acct/../../../../../../var/tmp/PWNED` reported
    `changed: 1` and "Moved to ...": nothing escaped, because Maildir
    encodes `/` as `.`, but the index row then named a junk folder and
    the next sync would have `IMAP CREATE`d it on the server.  A typo
    did the same damage as an attack.
    """
    services, index = _services("web-move-unknown")
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.post(
            "/action/move",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": str(message_id),
                "target": "acct/../../../../../../var/tmp/PWNED",
            },
        )

    body = response.json()
    assert body["changed"] == 0
    assert "No folder" in body["notices"][0]["text"]
    # And the message is still where it was.
    (row,) = index.list_folder_messages(folder=INBOX)
    assert row.message_ref.folder_name == "INBOX"


def test_a_copy_only_accepts_a_folder_the_page_offered() -> None:
    services, index = _services("web-copy-unknown")

    with _client(services) as client:
        response = client.post(
            "/action/copy",
            data={
                "account": "acct",
                "folder": "INBOX",
                "id": str(_only_message(index)),
                "target": "acct/Nowhere",
            },
        )

    assert response.json()["changed"] == 0
    assert (
        index.list_folder_messages(
            folder=FolderRef(account_name="acct", folder_name="Nowhere"),
        )
        == ()
    )


def test_a_folder_can_be_created_from_the_browser() -> None:
    services, _index = _services("web-new-folder")

    with _client(services) as client:
        response = client.post(
            "/action/new_folder",
            data={"account": "acct", "folder": "INBOX", "name": "Projects"},
        )
        refused = client.post(
            "/action/new_folder",
            data={"account": "acct", "folder": "INBOX", "name": "  "},
        )

    assert "created locally" in response.json()["notices"][0]["text"]
    assert refused.json()["notices"][0]["text"] == "Name the folder to create."
    names = [
        ref.folder_name
        for ref in services.mirrors["acct"].list_folders(account_name="acct")
    ]
    assert "Projects" in names


def test_harvesting_a_folder_adds_its_addresses_to_the_contacts() -> None:
    services, index = _services("web-harvest")

    with _client(services) as client:
        response = client.post(
            "/action/harvest_contacts",
            data={"account": "acct", "folder": "INBOX"},
        )

    assert "Harvested contacts from 1 message(s) in INBOX." in [
        notice["text"] for notice in response.json()["notices"]
    ]
    assert index.list_all_contacts()


def test_an_unknown_action_says_so_instead_of_pretending() -> None:
    services, _index = _services("web-unknown-action")

    with _client(services) as client:
        response = client.post(
            "/action/launch_rocket",
            data={"account": "acct", "folder": "INBOX"},
        )

    assert response.status_code == 404
    assert "not available in the browser" in response.json()["notices"][0]["text"]


def test_an_action_ignores_ids_that_are_not_numbers() -> None:
    services, index = _services("web-bad-ids")

    with _client(services) as client:
        response = client.post(
            "/action/trash",
            data={"account": "acct", "folder": "INBOX", "id": "not-a-row"},
        )

    assert response.json()["changed"] == 0
    (row,) = index.list_folder_messages(folder=INBOX)
    assert row.local_status is MessageStatus.ACTIVE


# ----------------------------------------------------------------------
# Syncing
# ----------------------------------------------------------------------


def _plan(*, halted: bool = False) -> object:
    """A sync plan with one folder, halted or not."""
    from pony.mail.sync import AccountSyncPlan, FolderSyncPlan, SyncPlan

    folder = FolderSyncPlan(
        folder_name="INBOX",
        uid_validity=1,
        highest_uid=0,
        ops=(),
        needs_confirmation=halted,
        pending_delete_count=9 if halted else 0,
        pending_delete_total=10 if halted else 0,
    )
    return SyncPlan(
        accounts=(AccountSyncPlan(account_name="acct", folders=(folder,)),),
    )


def _result() -> object:
    from pony.mail.sync import AccountSyncResult, FolderSyncResult, SyncResult

    return SyncResult(
        accounts=(
            AccountSyncResult(
                account_name="acct",
                folders=(FolderSyncResult(folder_name="INBOX", fetched=3),),
            ),
        ),
    )


def test_syncing_from_the_browser_runs_the_same_engine() -> None:
    """`g` fetches mail here, as it does in the terminal."""
    services, _index = _services("web-sync")

    with patch.object(services.sync, "service") as service:
        service.plan.return_value = _plan()
        service.execute.return_value = _result()
        with _client(services) as client:
            response = client.post(
                "/action/sync",
                data={"account": "acct", "folder": "INBOX"},
            )

    assert service.execute.called
    assert response.json() == {
        "changed": 3,
        "notices": [
            {
                "text": "Sync complete.  acct: +3 msgs, 0 merged, 0 pushed",
                "severity": "information",
            },
        ],
    }


def test_both_sync_keys_fetch_mail_here() -> None:
    """There is nothing for a quieter second sync key to mean."""
    assert SERVER_ACTIONS["sync"] is SERVER_ACTIONS["background_sync"]


def test_a_sync_without_credentials_says_what_is_missing() -> None:
    services, _index = _services("web-sync-no-credentials")

    with _client(services) as client:
        response = client.post(
            "/action/sync",
            data={"account": "acct", "folder": "INBOX"},
        )

    assert response.json()["notices"][0]["text"] == (
        "No credentials provider, so there is nothing to sync with."
    )


def test_a_failing_sync_is_reported_not_raised() -> None:
    services, _index = _services("web-sync-fails")

    with patch.object(services.sync, "service") as service:
        service.plan.side_effect = RuntimeError("the server hung up")
        with _client(services) as client:
            response = client.post("/action/sync", data={})

    outcome = response.json()
    assert outcome["changed"] == 0
    assert outcome["notices"][0]["text"] == "Sync failed: the server hung up"
    assert outcome["notices"][0]["severity"] == "error"


def test_only_one_sync_runs_at_a_time_in_this_server() -> None:
    """Two tabs must not open two IMAP sessions on one account."""
    import threading

    services, _index = _services("web-sync-busy")
    started = threading.Event()
    release = threading.Event()

    def _slow(**_kwargs: object) -> object:
        # `**_kwargs` because the gate hands `plan` a progress callback,
        # as the reader's worker does.
        started.set()
        release.wait(timeout=5)
        return _plan()

    with patch.object(services.sync, "service") as service:
        service.plan.side_effect = _slow
        service.execute.return_value = _result()
        first = threading.Thread(target=services.sync.mail)
        first.start()
        assert started.wait(timeout=5)
        second = services.sync.mail()
        release.set()
        first.join(timeout=5)

    assert second.outcome.notices[0].text == "Sync already running."


def test_a_halted_sync_asks_before_applying_anything() -> None:
    """The engine's mass-deletion gate holds in the browser too."""
    services, _index = _services("web-sync-halt")

    with patch.object(services.sync, "service") as service:
        service.plan.return_value = _plan(halted=True)
        with _client(services) as client:
            response = client.post("/action/sync", data={})

    assert not service.execute.called
    body = response.json()
    assert body["changed"] == 0
    assert "would delete 9 of 10 (90%)" in body["notices"][0]["text"]
    assert body["notices"][0]["severity"] == "warning"
    assert body["confirm"]["fields"] == {"confirm": "1"}
    assert "Apply the sync anyway?" in body["confirm"]["question"]


def test_answering_the_halt_applies_it_to_the_halted_folders() -> None:
    services, _index = _services("web-sync-confirmed")

    with patch.object(services.sync, "service") as service:
        service.plan.return_value = _plan(halted=True)
        service.execute.return_value = _result()
        with _client(services) as client:
            response = client.post("/action/sync", data={"confirm": "1"})

    assert response.json()["changed"] == 3
    _plan_arg, kwargs = service.execute.call_args
    assert kwargs["confirmed_folders"] == frozenset({("acct", "INBOX")})


def test_the_sync_applied_is_the_sync_that_was_described() -> None:
    """Answering used to re-plan, and apply the *new* plan.

    So the user agreed to one described mass deletion and a freshly
    computed one was applied, and a confirmed sync cost two full IMAP
    passes instead of one.  The terminal plans once and executes that
    same object; so does this.
    """
    services, _index = _services("web-sync-held-plan")
    described = _plan(halted=True)
    later = _plan(halted=True)

    with patch.object(services.sync, "service") as service:
        service.plan.side_effect = [described, later]
        service.execute.return_value = _result()
        with _client(services) as client:
            asked = client.post("/action/sync", data={})
            assert "confirm" in asked.json()
            answered = client.post("/action/sync", data={"confirm": "1"})

    assert answered.json()["changed"] == 3
    # Planned once, for the question. The answer applied that plan.
    assert service.plan.call_count == 1
    (applied,), _kwargs = service.execute.call_args
    assert applied is described


def test_a_plan_nobody_answered_in_time_is_not_applied_cold() -> None:
    """A server cannot hold a plan for however long a person takes."""
    import itertools

    from pony.web.syncing import HELD_PLAN_SECONDS

    services, _index = _services("web-sync-stale-plan")
    described = _plan(halted=True)
    later = _plan(halted=True)

    with (
        patch.object(services.sync, "service") as service,
        # The clock moves past the limit between the question and the
        # answer, which is what a person taking their time looks like.
        patch(
            "pony.web.syncing.time.monotonic",
            side_effect=itertools.count(0.0, HELD_PLAN_SECONDS + 1),
        ),
    ):
        service.plan.side_effect = [described, later]
        service.execute.return_value = _result()
        with _client(services) as client:
            client.post("/action/sync", data={})
            answered = client.post("/action/sync", data={"confirm": "1"})

    body = answered.json()
    assert "expired" in body["notices"][0]["text"]
    assert service.plan.call_count == 2
    (applied,), _kwargs = service.execute.call_args
    assert applied is later


def test_an_unhalted_sync_is_never_asked_about() -> None:
    services, _index = _services("web-sync-quiet")

    with patch.object(services.sync, "service") as service:
        service.plan.return_value = _plan()
        service.execute.return_value = _result()
        with _client(services) as client:
            response = client.post("/action/sync", data={})

    assert "confirm" not in response.json()


# ----------------------------------------------------------------------
# What a sync in flight says about itself
# ----------------------------------------------------------------------


def test_nothing_is_running_until_something_is() -> None:
    services, _index = _services("web-sync-idle")

    with _client(services) as client:
        response = client.get("/sync/status")

    assert response.json() == {
        "running": False,
        "message": "",
        "current": 0,
        "total": 0,
    }
    # A reading of this instant: a cached one would report a sync that
    # has finished.
    assert response.headers["cache-control"] == "no-store"


def test_the_page_is_told_what_the_engine_is_doing() -> None:
    """The terminal's dialog draws these; the browser used to drop them.

    `POST /action/sync` answers when the sync is over, so everything the
    engine says while it runs reached the browser only if something kept
    it.  The gate keeps the last one, which is what the chrome shows.
    """
    import threading

    from pony.mail.sync import ProgressInfo

    services, _index = _services("web-sync-progress")
    reported = threading.Event()
    release = threading.Event()
    seen: list[dict[str, object]] = []

    def _plan_slowly(*, progress: object = None, **_kwargs: object) -> object:
        assert callable(progress)
        progress(ProgressInfo("acct/INBOX: 3/10", current=3, total=10))
        reported.set()
        release.wait(timeout=5)
        return _plan()

    with patch.object(services.sync, "service") as service:
        service.plan.side_effect = _plan_slowly
        service.execute.return_value = _result()
        syncing = threading.Thread(target=services.sync.mail)
        syncing.start()
        try:
            assert reported.wait(timeout=5)
            seen.append(services.sync.progress().as_json())
        finally:
            release.set()
            syncing.join(timeout=5)

    assert seen == [
        {
            "running": True,
            "message": "acct/INBOX: 3/10",
            "current": 3,
            "total": 10,
        },
    ]
    # And the record is put back when the sync ends, so a page that
    # asks later is not told a finished sync is still running.
    assert services.sync.progress() == SyncProgress()


def test_a_sync_says_it_is_planning_before_the_engine_says_anything() -> None:
    """Planning an account with a large folder is a silent minute."""
    import threading

    services, _index = _services("web-sync-planning")
    entered = threading.Event()
    release = threading.Event()
    seen: list[SyncProgress] = []

    def _plan_slowly(**_kwargs: object) -> object:
        entered.set()
        release.wait(timeout=5)
        return _plan()

    with patch.object(services.sync, "service") as service:
        service.plan.side_effect = _plan_slowly
        service.execute.return_value = _result()
        syncing = threading.Thread(target=services.sync.mail)
        syncing.start()
        try:
            assert entered.wait(timeout=5)
            seen.append(services.sync.progress())
        finally:
            release.set()
            syncing.join(timeout=5)

    assert seen == [SyncProgress(running=True, message=PLANNING_MESSAGE)]


def test_a_failed_sync_leaves_nothing_running_behind_it() -> None:
    services, _index = _services("web-sync-progress-failure")

    with patch.object(services.sync, "service") as service:
        service.plan.side_effect = RuntimeError("the server hung up")
        services.sync.mail()

    assert services.sync.progress() == SyncProgress()


def test_a_run_this_page_did_not_start_is_still_a_run() -> None:
    """`g` is refused while the periodic sync holds the slot.

    The refusal is of more use with the run that caused it on screen:
    that thread was started with no callback of ours, so all there is to
    say is that it is happening.
    """
    from pony.mail.sync import MailSyncScheduler, SyncResult
    from pony.scheduler import PeriodicSync

    def _never_runs(**_kwargs: object) -> SyncResult:
        raise AssertionError("the slot is claimed by hand here")

    services, _index = _services("web-sync-background")
    scheduler: MailSyncScheduler = PeriodicSync(
        name="mail-sync",
        runner=_never_runs,
        interval=3600.0,
    )
    services.sync.scheduler = scheduler

    assert services.sync.progress() == SyncProgress()
    assert scheduler.try_claim()
    try:
        assert services.sync.progress() == SyncProgress(
            running=True,
            message=BACKGROUND_MESSAGE,
        )
    finally:
        scheduler.release()
    assert services.sync.progress() == SyncProgress()


# ----------------------------------------------------------------------
# Notices
# ----------------------------------------------------------------------


def test_every_notice_survives_a_redirect() -> None:
    """A per-message failure must not be swallowed by a later success."""
    from starlette.requests import Request

    from pony.outcome import Outcome, failure, info, warn
    from pony.web.app import _back_with, _notices

    outcome = Outcome(
        changed=2,
        notices=(
            failure("Could not archive <a@x>."),
            warn("No Sent folder; the copy stayed in Drafts."),
            info("Archived 2 message(s)."),
        ),
    )

    location = _back_with("/mail?account=acct", outcome).headers["location"]
    _path, _, query = location.partition("?")
    carried = _notices(
        Request({"type": "http", "query_string": query.encode(), "headers": []}),
    )

    assert [(notice.text, notice.severity) for notice in carried] == [
        ("Could not archive <a@x>.", "error"),
        ("No Sent folder; the copy stayed in Drafts.", "warning"),
        ("Archived 2 message(s).", "information"),
    ]


def test_a_flood_of_notices_is_counted_rather_than_spelled_out() -> None:
    from starlette.requests import Request

    from pony.outcome import Outcome, warn
    from pony.web.app import NOTICE_COUNT_LIMIT, _back_with, _notices

    outcome = Outcome(
        changed=0,
        notices=tuple(warn(f"row {n} failed") for n in range(NOTICE_COUNT_LIMIT + 4)),
    )

    location = _back_with("/mail", outcome).headers["location"]
    _path, _, query = location.partition("?")
    carried = _notices(
        Request({"type": "http", "query_string": query.encode(), "headers": []}),
    )

    assert len(carried) == NOTICE_COUNT_LIMIT + 1
    assert carried[-1].text == "...and 4 more."


# ----------------------------------------------------------------------
# The token
# ----------------------------------------------------------------------


def test_without_the_token_nothing_is_served() -> None:
    services, _index = _services("web-token-refused", token="s3cret")

    with _client(services) as client:
        assert client.get(_mail()).status_code == 403
        assert client.get("/keymap.json").status_code == 403
        assert client.get("/contacts").status_code == 403
        assert (
            client.post(
                "/action/trash",
                data={"account": "acct", "folder": "INBOX"},
            ).status_code
            == 403
        )


def test_the_token_arrives_in_the_url_once_and_is_remembered() -> None:
    """A phone should need the long link only the first time."""
    services, _index = _services("web-token-cookie", token="s3cret")

    with _client(services) as client:
        first = client.get(_mail(token="s3cret"))
        assert first.status_code == 200
        assert client.cookies[TOKEN_COOKIE] == "s3cret"
        # The cookie alone is enough from here on.
        assert client.get(_mail()).status_code == 200


def test_a_wrong_token_is_refused() -> None:
    services, _index = _services("web-token-wrong", token="s3cret")

    with _client(services) as client:
        assert client.get(_mail(token="guess")).status_code == 403
        assert TOKEN_COOKIE not in client.cookies


def test_a_token_outside_ascii_is_refused_rather_than_raising() -> None:
    """``compare_digest`` on two ``str`` raises for anything non-ASCII.

    So the one function that must never raise answered 500 with a
    traceback where it should answer 403.
    """
    services, _index = _services("web-token-unicode", token="s3cret")

    with _client(services) as client:
        assert client.get(_mail(token="é")).status_code == 403


# ----------------------------------------------------------------------
# Where a request came from
# ----------------------------------------------------------------------


def _states(index: SqliteIndexRepository) -> list[MessageStatus]:
    return [
        message.local_status for message in index.list_folder_messages(folder=INBOX)
    ]


def test_a_cross_site_post_changes_nothing() -> None:
    """The whole of CSRF, as a browser labels it.

    ``--no-token`` is the configuration the design points at — a tailnet
    already authenticates the device — so the refusal cannot depend on
    the token, and this runs with none.
    """
    services, index = _services("web-origin-cross")
    before = _states(index)

    with _client(services) as client:
        for headers in (
            {"sec-fetch-site": "cross-site"},
            {"sec-fetch-site": "same-site"},
            {"origin": "https://evil.example"},
            # A sandboxed frame or a `data:` page sends no origin at all.
            {"origin": "null"},
        ):
            refused = client.post(
                "/action/trash",
                data={"account": "acct", "folder": "INBOX", "id": "1"},
                headers=headers,
            )
            assert refused.status_code == 403, headers
            assert "another site" in refused.text

    assert _states(index) == before


def test_every_mutating_route_is_behind_the_origin_check() -> None:
    """Not just ``/action/*``: the forms and the upload route too."""
    services, _index = _services("web-origin-routes")
    hostile = {"sec-fetch-site": "cross-site"}

    with _client(services) as client:
        for path in (
            "/action/trash",
            "/compose",
            "/compose/attachment?name=x.txt",
            "/contacts/edit",
            "/calendar/event",
        ):
            answered = client.post(path, data={"to": "a@b.test"}, headers=hostile)
            assert answered.status_code == 403, path


def test_the_pages_own_request_is_served() -> None:
    """What a browser sends for Pony's own page, and for a typed URL."""
    services, index = _services("web-origin-same")
    message_id = _only_message(index)

    with _client(services) as client:
        for headers in (
            {"sec-fetch-site": "same-origin"},
            {"sec-fetch-site": "none"},
            {"origin": "http://testserver"},
        ):
            answered = client.post(
                "/action/toggle_flagged",
                data={"account": "acct", "folder": "INBOX", "id": str(message_id)},
                headers=headers,
            )
            assert answered.status_code == 200, headers
            assert answered.json()["changed"] == 1


def test_a_client_that_names_no_origin_still_works() -> None:
    """curl, a script, a phone shortcut: not a browser aimed at anyone."""
    services, index = _services("web-origin-headless")
    message_id = _only_message(index)

    with _client(services) as client:
        answered = client.post(
            "/action/toggle_flagged",
            data={"account": "acct", "folder": "INBOX", "id": str(message_id)},
        )

    assert answered.status_code == 200
    assert answered.json()["changed"] == 1


def test_a_cross_site_read_is_still_served() -> None:
    """Reads are not refused: the browser keeps the answer to itself."""
    services, _index = _services("web-origin-read")

    with _client(services) as client:
        answered = client.get(_mail(), headers={"sec-fetch-site": "cross-site"})

    assert answered.status_code == 200


def test_the_sync_keys_mean_the_calendar_on_the_calendar_page() -> None:
    """One spelling, two tables: the surface tells them apart."""
    source = JS.read_text(encoding="utf-8")

    assert 'state.surface === "calendar"' in source
    assert 'name = "sync_calendar";' in source
    assert "sync_calendar" in SERVER_ACTIONS


# ----------------------------------------------------------------------
# Addressing
# ----------------------------------------------------------------------


def test_the_footer_and_the_keymap_drop_a_repeated_key_the_same_way() -> None:
    """One de-duplication, two callers; it was written twice, yesterday.

    A surface can inherit the application keys and declare them again,
    and a bare capital and its ``shift+`` spelling are one key to a
    browser — so the footer listed "Q Quit" twice and the help offered a
    key no press could reach.
    """
    from pony.web.layout import PageSurface, bindings_for, one_per_web_key

    repeated = (
        KeyBinding("shift+q", "quit", "Quit", "application"),
        KeyBinding("Q", "quit", "Quit again", "application"),
        KeyBinding("n", "cursor_down", "Next", "list"),
    )

    assert [binding.description for binding in one_per_web_key(repeated)] == [
        "Quit",
        "Next",
    ]
    # And every surface the front end serves is already clean of them.
    for surface in PageSurface:
        keys = [
            web_key(binding.key) for binding in one_per_web_key(bindings_for(surface))
        ]
        assert len(keys) == len(set(keys)), surface


def test_one_rule_decides_whether_a_form_stays_up() -> None:
    """Compose, the event form and the contact form had three.

    Two asked ``Outcome.blocked``, which is ``failed or not acted`` and
    so reads a save that legitimately changed nothing as a failure; the
    third asked whether a record came back.  What a form needs to know
    is whether anything is *wrong*, and a refusal is deliberately a
    warning rather than an error, because throwing away what somebody
    typed over a warning loses their work.
    """
    from pony.outcome import Notice, Outcome, failure, info, warn
    from pony.web.actions import holds_form

    # Something is wrong: the typing stays on screen.
    assert holds_form(Outcome.refused(warn("An event needs a summary.")))
    assert holds_form(Outcome(changed=0, notices=(failure("nobody answered"),)))
    assert holds_form(Outcome(changed=1, notices=(failure("half of it failed"),)))
    # It went through, or there was nothing to do: the form is done with.
    assert not holds_form(Outcome(changed=1, notices=(info("Saved Ada."),)))
    assert not holds_form(Outcome(changed=0, notices=(info("Nothing to change."),)))
    assert not holds_form(Outcome())
    # Which is where `blocked` and this part company.
    unchanged = Outcome(changed=0, notices=(Notice("Nothing to change."),))
    assert unchanged.blocked
    assert not holds_form(unchanged)


def test_a_link_leaves_out_what_has_no_value() -> None:
    from pony.web import urls

    assert urls.query(account="acct", folder=None, q="") == "?account=acct"
    assert urls.query() == ""
    assert urls.mail_url(None) == "/mail"


def test_only_the_first_slash_separates_an_account_from_its_folder() -> None:
    """`work/INBOX/2024` is a nested folder, not a nested account."""
    from pony.web.actions import parse_target

    target = parse_target("work/INBOX/2024")

    assert target is not None
    assert (target.account_name, target.folder_name) == ("work", "INBOX/2024")
    assert parse_target("nothing-but-an-account") is None
    assert parse_target("/leading") is None


# ----------------------------------------------------------------------
# A screen with no keyboard
# ----------------------------------------------------------------------


def test_the_reader_offers_buttons_for_the_keys_it_binds() -> None:
    """A phone has no `A`, so Archive has to be tappable."""
    services, index = _services("web-touch")
    message_id = _only_message(index)

    with _client(services) as client:
        body = client.get(_message(message_id)).text

    for action in ("archive", "trash", "toggle_flagged", "mark_unread"):
        assert f'data-action="{action}"' in body
    # And the search the `/` key runs has a field of its own.
    assert 'type="search"' in body
    assert 'name="q"' in body


def test_every_button_that_names_an_action_names_a_real_one() -> None:
    """A `data-action` the server does not know would do nothing."""
    import re

    services, index = _services("web-touch-actions")
    message_id = _only_message(index)

    with _client(services) as client:
        pages = [
            client.get(_message(message_id)).text,
            client.get("/contacts").text,
        ]

    named = {
        name for page in pages for name in re.findall(r'data-action="([^"]+)"', page)
    }
    assert named
    assert named <= set(SERVER_ACTIONS)


def test_the_folder_pane_shows_folders_the_mirror_has_not_materialised() -> None:
    """A synced-but-absent folder is in the reader's tree; it belongs here too."""
    from pony.mail.domain import FolderSyncState

    services, index = _services("web-folder-tree-sync-state")
    index.record_folder_sync_state(
        state=FolderSyncState(
            account_name="acct",
            folder_name="Archive/2019",
            uid_validity=1,
            highest_uid=0,
        ),
    )

    with _client(services) as client:
        body = client.get(_mail()).text

    assert "Archive" in body
    assert "2019" in body


def test_an_account_and_a_parent_folder_both_fold() -> None:
    """The reader folds both, which is how a dozen accounts stay usable.

    A ``<details>`` each, so a fold needs no script: the account's
    heading is the summary, and a folder with folders under it keeps its
    own row as the summary -- the name still opens the folder, the
    twisty beside it shows or hides what is below.
    """
    from pony.mail.domain import FolderSyncState

    services, index = _services("web-folder-folds")
    index.record_folder_sync_state(
        state=FolderSyncState(
            account_name="acct",
            folder_name="Archive/2019",
            uid_validity=1,
            highest_uid=0,
        ),
    )

    with _client(services) as client:
        body = client.get(_mail()).text

    # The account, open, and named so a fold can be remembered.
    assert '<details class="account" data-fold="a:acct" open>' in body
    assert "<summary><h2>" in body
    # The parent folder, with the folder's own row as its summary.
    assert '<details class="branch" data-fold="f:acct/Archive" open>' in body
    assert '<summary><span class="folder synthetic' in body
    # And a leaf has no fold of its own.
    assert body.count("<details") == body.count("</details>")
    assert body.count('data-fold="f:') == 1


def test_an_address_in_a_body_opens_the_composer_and_not_the_desktop() -> None:
    """A `mailto:` anchor hands the address to the wrong program.

    Whatever the operating system has registered is not the mail
    program the message is being read in, and the terminal routes the
    same link to its own composer.  So the link is to ``/compose``,
    built on the server: the pages work with the script switched off.
    """
    html = (
        b"From: them@example.test\r\n"
        b"To: you@example.test\r\n"
        b"Subject: Invitation\r\n"
        b'Content-Type: text/html; charset="utf-8"\r\n'
        b"\r\n"
        b'<p>Write to <a href="mailto:ada@example.test?subject=Hi">Ada</a>.</p>\r\n'
    )
    services, index = _services("web-mailto", seed=(html,))
    message_id = _only_message(index)

    with _client(services) as client:
        body = client.get(_message(message_id)).text

    assert "mailto:" not in body
    assert "Ada" in body
    target = link_to(body, "ada@example.test")
    assert target.startswith("/compose")
    # And the composer it opens is prefilled with that address, on the
    # account the mail arrived at.
    with _client(services) as client:
        form = client.get(target).text
    assert form_fields(form, "#compose-form")["to"] == "ada@example.test"
    assert form_fields(form, "#compose-form")["account"] == "acct"


def test_the_body_keeps_the_anchor_text_an_html_message_chose() -> None:
    """The renderer's links, not a second regex: `click here` survives."""
    html = (
        b"From: them@example.test\r\n"
        b"To: you@example.test\r\n"
        b"Subject: Invitation\r\n"
        b'Content-Type: text/html; charset="utf-8"\r\n'
        b"\r\n"
        b'<p>Please <a href="https://example.test/rsvp">click here</a> '
        b"and <b>hurry</b>.</p>\r\n"
    )
    services, index = _services("web-anchor-text", seed=(html,))

    with _client(services) as client:
        body = client.get(_message(_only_message(index))).text

    assert "click here" in body
    assert 'href="https://example.test/rsvp"' in body
    # The emphasis the renderer marked is emphasis here too.
    assert "<strong>hurry</strong>" in body


def test_a_shift_letter_key_is_served_as_the_browser_reports_it() -> None:
    """Every `shift+<letter>` alias used to be served and never match."""

    services, _index = _services("web-shift-keys")

    with _client(services) as client:
        keymaps = client.get("/keymap.json").json()

    calendar = {entry["key"] for entry in keymaps["calendar"]}
    # The browser reports shift+g as "G" with the shift flag set, and
    # `pony.js` builds "G" for it.
    assert web_key("shift+g") == "G"
    assert "shift+g" not in calendar
    assert "G" in calendar
    # A named key keeps its modifier, because the DOM reports it there.
    reader = {entry["key"] for entry in keymaps["reader"]}
    assert "shift+ArrowUp" in reader


# ----------------------------------------------------------------------
# Quitting
# ----------------------------------------------------------------------


def test_quit_stops_the_server() -> None:
    """`Q` means the same here as in the reader: stop Pony."""
    services, _index = _services("web-quit")
    stopped: list[bool] = []
    services.shutdown.stop = lambda: stopped.append(True)

    with _client(services) as client:
        response = client.post("/action/quit", data={})

    assert stopped == [True]
    body = response.json()
    assert body["changed"] == 1
    assert body["notices"][0]["text"] == "Pony Express is stopping."


def test_quit_says_so_when_there_is_nothing_to_stop() -> None:
    """A server embedded in something else does not stop itself."""
    services, _index = _services("web-quit-unwired")

    with _client(services) as client:
        response = client.post("/action/quit", data={})

    outcome = response.json()
    assert outcome["changed"] == 0
    assert "close it where it was started" in outcome["notices"][0]["text"]


def test_the_page_closes_itself_after_the_server_stops() -> None:
    """Both halves: the process stops and the tab goes."""
    source = JS.read_text(encoding="utf-8")

    assert "quit: quit," in source
    assert 'await server("quit")' in source
    assert "window.close()" in source
    # And a page whose server has gone stops answering keys.
    assert "state.quitting = true;" in source
    assert "state.quitting" in source.split("function onKeyDown")[1]


def test_an_action_drops_the_message_it_took_out_of_the_folder() -> None:
    """`A` twice archived one message and then failed on it again.

    `data-message` is rendered by the server and a refresh swaps only
    the rows, so after archiving the open message the page went on
    naming it. `targets()` prefers that name over the cursor, so the
    second press acted on a message that had left the folder -- and the
    reading pane still showed it. The refresh now leaves for the folder
    without it.

    Read rather than executed: the behaviour needs a browser, and the
    real-browser harness intercepts `/action/*`, so nothing would
    actually leave the folder there.
    """
    source = JS.read_text(encoding="utf-8")
    refresh = source.split("async function refresh()")[1].split("\n}")[0]

    assert "here().message" in refresh
    assert "folderParams()" in refresh
    # Without the `id`, so the page stops naming a message that is gone.
    assert "id" not in _params_of(source, "folderParams")


def _params_of(source: str, name: str) -> str:
    return source.split(f"function {name}()")[1].split("\n}")[0]


def test_the_footer_lists_each_key_once() -> None:
    """A surface can inherit the application keys and declare them again."""
    from pony.web.layout import PageSurface, _footer, bindings_for

    for surface in PageSurface:
        bindings = bindings_for(surface)
        rendered = str(_footer(bindings))
        shown = [web_key(b.key) for b in bindings if b.show]
        assert rendered.count('class="key"') == len(set(shown)), surface


def test_a_state_class_is_never_styled_on_its_own() -> None:
    """`unread` is a count beside a folder and a state on a message row.

    A bare `.unread` rule reached both, so every unread row inherited
    the monospace meant for the count and read as a different typeface
    instead of as bold.  It took two wrong diagnoses to find, because
    nothing in either place looks wrong on its own.  A name the markup
    uses as state stays qualified -- by the pane it belongs to or by the
    element that carries it -- and then a rule written for one place
    cannot travel to the other.
    """
    loose = [
        selector
        for selector in _selectors(CSS.read_text(encoding="utf-8"))
        if ".unread" in selector
        and not selector.startswith("#")
        and ".row" not in selector
    ]

    assert loose == []


def test_the_move_prompt_offers_a_folder_with_no_directory_yet() -> None:
    """The prompt's options came from the mirror, the pane's from the index.

    A folder the server has but this host has not materialised, and one
    that has messages with no directory on disk, are both folders: the
    pane shows them because it asks `folder_unread_counts`.  Two other
    loops asked the mirror alone, so the move and go-to prompts offered
    a different set from the pane beside them -- the divergence the
    terminal's `action_goto_folder` had.
    """
    from pony.mail.domain import FolderSyncState
    from pony.web.mail import folder_options

    services, index = _services("web-folder-options")
    index.record_folder_sync_state(
        state=FolderSyncState(
            account_name="acct",
            folder_name="Synced/Elsewhere",
            uid_validity=1,
            highest_uid=0,
        ),
    )

    assert "acct/Synced/Elsewhere" in folder_options(services)
