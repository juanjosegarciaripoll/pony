"""The browser front end, over real stores and a real ASGI client.

Every route here is a thin shell over code that already has its own
tests: the actions in :mod:`pony.mailbox_actions`, the row projections in
:mod:`pony.views`, the keymap in :mod:`pony.keymap`.  So these check what
is genuinely new — the routing, the escaping, the token gate, the
sandboxing of a message body, and that an action reached over HTTP is
the same call the terminal makes.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from corpus import html_only, plain_text
from starlette.testclient import TestClient
from tui_helpers import (
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
    seed_message,
)

from pony.domain import FolderRef, MessageFlag, MessageStatus
from pony.web import WebServices, build_web_app
from pony.web.app import TOKEN_COOKIE

if TYPE_CHECKING:
    from pony.index_store import SqliteIndexRepository

INBOX = FolderRef(account_name="acct", folder_name="INBOX")


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
    for raw in seed:
        seed_message(
            index=index,
            mirror=mirrors["acct"],
            folder=INBOX,
            raw=raw,
            message_id=f"<seed-{len(raw)}-{seed.index(raw)}@example.com>",
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


# ----------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------


def test_the_root_opens_the_first_folder() -> None:
    services, _index = _services("web-root")

    with _client(services) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/f/acct/INBOX"


def test_the_root_says_so_when_there_is_no_mail_yet() -> None:
    services, _index = _services("web-root-empty", seed=())
    # A fresh maildir still synthesises INBOX, so empty means empty of
    # folders: take the mirrors away to get there.
    bare = dataclasses.replace(services, mirrors={})

    with _client(bare) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Run 'pony sync'" in response.text


def test_a_folder_page_lists_its_messages_and_the_folder_tree() -> None:
    services, _index = _services("web-folder")

    with _client(services) as client:
        response = client.get("/f/acct/INBOX")

    assert response.status_code == 200
    body = response.text
    # The row projection's fields, and the tree the TUI renders too.
    assert "Tuesday meeting confirmed" in body
    assert 'data-folder="INBOX"' in body
    assert "INBOX" in body
    # Unread mail is marked as such rather than every read row being muted.
    assert 'class="row unread"' in body


def test_opening_a_message_marks_it_read_and_frames_the_body() -> None:
    services, index = _services("web-message")
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.get(f"/m/acct/INBOX/{message_id}")

    assert response.status_code == 200
    # The body is in a frame of its own, with everything switched off.
    assert f'src="/m/acct/INBOX/{message_id}/body"' in response.text
    assert 'sandbox=""' in response.text
    (summary,) = index.list_folder_message_summaries(folder=INBOX)
    assert MessageFlag.SEEN in summary.local_flags


def test_a_message_body_is_served_with_everything_switched_off() -> None:
    """Mail is hostile input; the frame may not run or fetch anything."""
    services, index = _services("web-body", seed=(html_only(),))
    message_id = _only_message(index)

    with _client(services) as client:
        response = client.get(f"/m/acct/INBOX/{message_id}/body")

    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    # Inline images are already data URIs, so that is all img-src allows.
    assert "img-src data:" in policy
    assert response.headers["referrer-policy"] == "no-referrer"


def test_a_missing_message_is_a_404_not_a_crash() -> None:
    services, _index = _services("web-missing")

    with _client(services) as client:
        assert _client(services).get("/m/acct/INBOX/999").status_code == 404
        assert client.get("/m/acct/INBOX/999/body").status_code == 404


def test_a_message_whose_file_vanished_reports_that_it_is_gone() -> None:
    services, index = _services("web-gone")
    message_id = _only_message(index)
    message = index.get_message(
        message_ref=dataclasses.replace(
            index.list_folder_message_summaries(folder=INBOX)[0].message_ref,
            id=message_id,
        ),
    )
    assert message is not None
    services.mirrors["acct"].delete_message(
        folder=INBOX,
        storage_key=message.storage_key,
    )

    with _client(services) as client:
        response = client.get(f"/m/acct/INBOX/{message_id}/body")

    assert response.status_code == 410


def test_the_folder_fragment_and_row_fragment_are_servable_on_their_own() -> None:
    """The page refreshes a part of itself rather than reloading."""
    services, _index = _services("web-fragments")

    with _client(services) as client:
        rows = client.get("/f/acct/INBOX/rows")
        folders = client.get("/folders")

    assert rows.status_code == 200
    assert rows.text.startswith("<ol")
    assert folders.status_code == 200
    assert "<section>" in folders.text


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
        response = client.get("/f/acct/INBOX")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


# ----------------------------------------------------------------------
# The keymap
# ----------------------------------------------------------------------


def test_the_keymap_is_served_with_what_the_browser_can_actually_do() -> None:
    services, _index = _services("web-keymap")

    with _client(services) as client:
        bindings = client.get("/keymap.json").json()

    by_action = {entry["action"]: entry for entry in bindings}
    # The same table the terminal binds, so every reader key is here.
    assert by_action["archive"]["key"] == "A"
    assert by_action["archive"]["available"] is True
    assert by_action["cursor_down"]["available"] is True
    # Composing is not wired up yet, and the help says so rather than
    # the page ignoring the press.
    assert by_action["compose_new"]["available"] is False
    # Named keys come through in the browser's own spelling.
    assert by_action["show_help"]["key"] == "F1"
    assert by_action["mark_up"]["key"] == "shift+ArrowUp"


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
        client.get(f"/m/acct/INBOX/{message_id}")
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


def test_an_unknown_action_says_so_instead_of_pretending() -> None:
    services, _index = _services("web-unknown-action")

    with _client(services) as client:
        response = client.post(
            "/action/compose_new",
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
# The token
# ----------------------------------------------------------------------


def test_without_the_token_nothing_is_served() -> None:
    services, _index = _services("web-token-refused", token="s3cret")

    with _client(services) as client:
        assert client.get("/f/acct/INBOX").status_code == 403
        assert client.get("/keymap.json").status_code == 403
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
        first = client.get("/f/acct/INBOX?token=s3cret")
        assert first.status_code == 200
        assert client.cookies[TOKEN_COOKIE] == "s3cret"
        # The cookie alone is enough from here on.
        assert client.get("/f/acct/INBOX").status_code == 200


def test_a_wrong_token_is_refused() -> None:
    services, _index = _services("web-token-wrong", token="s3cret")

    with _client(services) as client:
        assert client.get("/f/acct/INBOX?token=guess").status_code == 403
        assert TOKEN_COOKIE not in client.cookies


# ----------------------------------------------------------------------
# The agenda
# ----------------------------------------------------------------------


def test_the_agenda_says_when_there_is_no_calendar() -> None:
    services, _index = _services("web-agenda-none")

    with _client(services) as client:
        response = client.get("/agenda")

    assert response.status_code == 200
    assert "No calendar configured." in response.text


def test_the_agenda_renders_the_calendar_it_is_given() -> None:
    """The same projections the TUI agenda uses, in HTML."""
    from datetime import UTC, datetime, timedelta

    from tui_helpers import make_calendar_runtime

    start = datetime.now(UTC).astimezone().replace(
        hour=10,
        minute=0,
        second=0,
        microsecond=0,
    ) + timedelta(days=1)
    end = start + timedelta(minutes=45)
    ics = (
        "\r\n".join(
            (
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//tests//EN",
                "BEGIN:VEVENT",
                "UID:web-agenda@example.com",
                f"DTSTAMP:{start.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART:{start.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}",
                f"DTEND:{end.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}",
                "SUMMARY:Planning review",
                "LOCATION:Room 4",
                "END:VEVENT",
                "END:VCALENDAR",
            ),
        )
        + "\r\n"
    ).encode()
    services, _index = _services("web-agenda")
    with_calendar = dataclasses.replace(
        services,
        calendar=make_calendar_runtime(
            make_tmp_paths("web-agenda-cal"),
            events=(("web-agenda@example.com", ics),),
        ),
    )

    with _client(with_calendar) as client:
        response = client.get("/agenda")

    assert response.status_code == 200
    assert "Planning review" in response.text
    assert "Room 4" in response.text
    # Tomorrow is named, not dated, exactly as the agenda screen names it.
    assert "Tomorrow" in response.text


def test_an_empty_folder_says_so() -> None:
    services, index = _services("web-empty-folder")
    (summary,) = index.list_folder_message_summaries(folder=INBOX)
    index.delete_message(message_ref=summary.message_ref)

    with _client(services) as client:
        response = client.get("/f/acct/INBOX")

    assert "Nothing here." in response.text


def test_a_body_on_an_account_with_no_mirror_is_gone_not_broken() -> None:
    services, index = _services("web-no-mirror")
    message_id = _only_message(index)
    bare = dataclasses.replace(services, mirrors={})

    with _client(bare) as client:
        response = client.get(f"/m/acct/INBOX/{message_id}/body")

    assert response.status_code == 410
