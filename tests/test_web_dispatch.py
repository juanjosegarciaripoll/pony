"""The contract between ``/keymap.json``, ``static/pony.js`` and the pages.

Three things have to agree for a key to work in a browser, and each of
them is written in a different language: the keymap the server serves,
the dispatcher that matches a ``keydown`` against it, and the markup
that offers the same operations to a screen with no keyboard.  Nothing
holds them together at runtime, so what holds them together is here.

The dispatcher is read rather than run, because this suite has no
JavaScript engine and a dependency on one is not wanted.  Reading it is
enough for the mistakes that matter most, because they are all
mismatches rather than logic: an action the help advertises and the page
has not got, a key spelled so that no press can ever produce it, a path
fetched that no route serves, a function called that nobody defined
(which is exactly how ``Q`` stopped quitting), a button naming an action
the server does not run.

What happens *inside* a client-side action — the prompt dialog, the mark
toggling, the fragment swap — cannot be read off the source, and is not
pretended at here.  ``tests/test_web_browser.py`` presses the keys at a
real page in a headless Chromium for that, opt-in through
``PONY_BROWSER_UI=1``, so a machine without a browser still has
everything below.

The line between the two matters, because a test that only asserts a
string appears in the source cannot fail for the reason it names:
wrapping ``quit()``'s body in ``if (false) {}`` left a whole test about
quitting passing, which is the shape of the dead ``server()`` bug that
shipped once already.  Five tests here were that shape and are now
split — the mismatch half read, the behaviour half executed, each
naming the other.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

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
    BROWSER_METHODS,
    actions,
    attribute_values,
    browser_calls,
    called_names,
    client_actions,
    declared_names,
    fetched_paths,
    form_fields,
    link_to,
    page_data,
    script,
)

from chronos.keymap import web_key
from pony.domain import Contact, FolderRef, MessageRef
from pony.web import WebServices, build_web_app
from pony.web.actions import MARKED_ONLY_ACTIONS
from pony.web.app import CLIENT_ACTIONS, SERVER_ACTIONS
from pony.web.layout import PageSurface, bindings_for

if TYPE_CHECKING:
    from pony.calendar import CalendarRuntime

INBOX = FolderRef(account_name="acct", folder_name="INBOX")
ACCOUNT = "personal"
CALENDAR = "work"

#: What a browser puts in ``event.key`` for a key that is not a
#: character.  Written out here rather than imported, because it is the
#: DOM's vocabulary and not Pony's: a translation that forgot an entry
#: would otherwise agree with itself.
DOM_KEY_NAMES = frozenset(
    {
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "Backspace",
        "Delete",
        "Enter",
        "Escape",
        "Tab",
        *(f"F{number}" for number in range(1, 13)),
    },
)

#: The order ``pressed()`` builds a chord in.
MODIFIER_ORDER = ("ctrl", "alt", "shift")

#: Actions that are two names for one operation, so one button serves
#: both: the quiet sync key, and the sync that means the calendar.
ALIASES = {"background_sync": "sync", "sync_calendar": "sync"}


# ----------------------------------------------------------------------
# A front end with something on every page
# ----------------------------------------------------------------------


def _ics(uid: str) -> bytes:
    """An event at noon today, which every view's window contains.

    Noon in the local zone and not in UTC, because the views window by
    local days: an hour chosen in UTC falls on the day before or after
    in some zone, and the agenda would then be empty on the runner and
    full here (see ``ai/AGENTS.md`` on running the suite under TZ=UTC).
    """
    start = (
        datetime.now(UTC)
        .astimezone()
        .replace(
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
    )
    return (
        "\r\n".join(
            (
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//tests//EN",
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{start.astimezone(UTC):%Y%m%dT%H%M%SZ}",
                f"DTSTART:{start.astimezone(UTC):%Y%m%dT%H%M%SZ}",
                f"DTEND:{(start + timedelta(hours=1)).astimezone(UTC):%Y%m%dT%H%M%SZ}",
                "SUMMARY:Standing meeting",
                "END:VEVENT",
                "END:VCALENDAR",
            ),
        )
        + "\r\n"
    ).encode()


def _invitation() -> bytes:
    return (
        b"From: bob@example.com\r\n"
        b"To: acct@example.com\r\n"
        b"Subject: Design review\r\n"
        b'Content-Type: text/calendar; method=REQUEST; charset="utf-8"\r\n'
        b"\r\n"
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nMETHOD:REQUEST\r\n"
        b"BEGIN:VEVENT\r\nUID:invite@example.com\r\nDTSTAMP:20260101T090000Z\r\n"
        b"DTSTART:20260601T090000Z\r\nDTEND:20260601T100000Z\r\n"
        b"SUMMARY:Design review\r\nORGANIZER;CN=Bob:mailto:bob@example.com\r\n"
        b"ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:acct@example.com\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def _shared_calendar_file() -> bytes:
    """A `.ics` somebody attached, with nobody to answer.

    Not an invitation: no METHOD and no organizer, so the reader offers
    it as a file to import rather than as a meeting to accept.
    """
    return (
        b"From: bob@example.com\r\n"
        b"To: acct@example.com\r\n"
        b"Subject: Our schedule\r\n"
        b'Content-Type: multipart/mixed; boundary="b1"\r\n'
        b"\r\n"
        b"--b1\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"Here it is.\r\n"
        b"--b1\r\n"
        b'Content-Type: text/calendar; charset="utf-8"\r\n'
        b'Content-Disposition: attachment; filename="schedule.ics"\r\n'
        b"\r\n"
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:shared@example.com\r\nDTSTAMP:20260101T090000Z\r\n"
        b"DTSTART:20260602T090000Z\r\nDTEND:20260602T100000Z\r\n"
        b"SUMMARY:Our schedule\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        b"--b1--\r\n"
    )


def _services(
    label: str,
) -> tuple[WebServices, CalendarRuntime, dict[str, int]]:
    """Mail, contacts and a calendar, each with something in it."""
    paths = make_tmp_paths(label)
    account = dataclasses.replace(
        make_test_account(paths, archive_folder="Archive"),
        sent_folder="Sent",
    )
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    plain = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=INBOX,
        raw=plain_text(),
        message_id="<plain@example.com>",
    )
    invited = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=INBOX,
        raw=_invitation(),
        message_id="<invite@example.com>",
    )
    shared = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=INBOX,
        raw=_shared_calendar_file(),
        message_id="<shared@example.com>",
    )
    index.upsert_contact(
        contact=Contact(
            id=None,
            first_name="Ada",
            last_name="Lovelace",
            emails=("ada@example.test",),
        ),
    )
    runtime = make_calendar_runtime(
        make_tmp_paths(f"{label}-cal"),
        account_name=ACCOUNT,
        calendar_name=CALENDAR,
        events=(("standing@example.com", _ics("standing@example.com")),),
    )
    (runtime.mirror.root / ACCOUNT / CALENDAR).mkdir(parents=True, exist_ok=True)
    services = WebServices(
        config=config,
        index=index,
        mirrors=mirrors,
        contacts=index,
        calendar=runtime,
    )
    return (
        services,
        runtime,
        {
            "plain": plain.id,
            "invited": invited.id,
            "shared": shared.id,
        },
    )


def _pages(client: TestClient, seeded: dict[str, int]) -> dict[str, str]:
    """One page of every kind the front end serves, by a short name.

    The event form is reached through the agenda's own row link, so the
    page under test is the one a user lands on rather than a URL made up
    here.
    """
    agenda = client.get("/calendar").text
    # The row's own link, not the New event link beside it.
    event_link = re.search(r'href="(/calendar/event\?[^"]*uid=[^"]+)"', agenda)
    assert event_link is not None, "the agenda offers no event to open"
    return {
        "list": client.get("/mail?account=acct&folder=INBOX").text,
        "message": client.get(
            f"/message?account=acct&folder=INBOX&id={seeded['plain']}",
        ).text,
        "invitation": client.get(
            f"/message?account=acct&folder=INBOX&id={seeded['invited']}",
        ).text,
        # A calendar file with nobody to answer, which is where the
        # Import button lives: an invitation offers "File only" instead,
        # because importing skips the reply to the organizer.
        "shared_ics": client.get(
            f"/message?account=acct&folder=INBOX&id={seeded['shared']}",
        ).text,
        "compose": client.get("/compose?mode=new&account=acct").text,
        "contacts": client.get("/contacts").text,
        "contact_form": client.get("/contacts/edit").text,
        "agenda": agenda,
        "month": client.get("/calendar?view=month").text,
        "event": client.get(event_link.group(1).replace("&amp;", "&")).text,
    }


def _by_surface(pages: dict[str, str]) -> dict[str, set[str]]:
    """Which actions are tappable on each surface, over every page of it.

    Two buttons carry no ``data-action`` and are counted anyway,
    because the script wires each of them to the same action with
    something extra that a plain action button could not carry: the
    event's Delete button knows its uid and calendar, and an
    invitation's answers know which answer they are.
    """
    found: dict[str, set[str]] = {}
    for page in pages.values():
        surface = page_data(page).get("surface", "")
        tappable = found.setdefault(surface, set())
        tappable |= actions(page)
        if 'id="delete-event"' in page:
            tappable.add("delete_event")
        if "invitation-answer" in page:
            tappable.add("respond_invitation")
    return found


# ----------------------------------------------------------------------
# The keymap the browser is served
# ----------------------------------------------------------------------


def test_the_served_keys_are_the_terminal_tables_for_that_surface() -> None:
    """One table, two front ends: the browser is served a translation.

    Not a second keymap — every entry has to be the terminal's binding
    for that surface, in the terminal's order, with only the spelling
    changed.  Two spellings of one key (a capital and its ``shift+``
    alias) are one key here, so the first wins.
    """
    services, _runtime, _seeded = _services("dispatch-tables")

    with TestClient(build_web_app(services)) as client:
        served = client.get("/keymap.json").json()

    assert set(served) == {surface.value for surface in PageSurface}
    for surface in PageSurface:
        expected: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for binding in bindings_for(surface):
            key = web_key(binding.key)
            if key in seen:
                continue
            seen.add(key)
            expected.append((key, binding.action, binding.description))
        got = [
            (entry["key"], entry["action"], entry["description"])
            for entry in served[surface.value]
        ]
        assert got == expected, surface.value


def test_no_served_key_is_one_a_press_could_never_produce() -> None:
    """The ``shift+<letter>`` class of bug, over every surface at once.

    ``pressed()`` builds ``[ctrl+][alt+][shift+]<event.key>``, and adds
    ``shift`` only in front of a named key, because the browser reports
    shift+m as ``M`` with the flag set.  A key spelled any other way is
    served, listed in the help, and dead.
    """
    services, _runtime, _seeded = _services("dispatch-spelling")

    with TestClient(build_web_app(services)) as client:
        served = client.get("/keymap.json").json()

    for surface, entries in served.items():
        assert entries, surface
        for entry in entries:
            key = str(entry["key"])
            *modifiers, base = key.split("+")
            assert set(modifiers) <= set(MODIFIER_ORDER), key
            order = [MODIFIER_ORDER.index(name) for name in modifiers]
            assert order == sorted(set(order)), key
            assert len(base) == 1 or base in DOM_KEY_NAMES, key
            if "shift" in modifiers:
                # A single character already carries the shift in itself.
                assert base in DOM_KEY_NAMES, key
        keys = [entry["key"] for entry in entries]
        # `Array.find` takes the first match, so a repeat would be dead.
        assert len(keys) == len(set(keys)), surface


def test_the_keymap_spells_a_key_the_way_a_browser_reports_it() -> None:
    """The server's half of the normalisation, run rather than read.

    The dispatcher's half is run too, in
    ``tests/test_web_browser.py::test_every_key_the_server_serves_is_one_the_page_can_match``,
    which hands ``pressed()`` the event a browser would deliver for
    every key the server serves and requires the same spelling back.
    Asserting the shape of its source here as well said nothing that
    was not already said, and said it about the text rather than the
    behaviour.
    """
    assert web_key("shift+m") == "M"
    assert web_key("shift+up") == "shift+ArrowUp"
    assert web_key("ctrl+g") == "ctrl+g"
    assert web_key("f1") == "F1"


def test_every_available_key_is_an_action_the_page_can_really_run() -> None:
    """The help cannot promise what nothing implements.

    From the served keymap rather than from the server's own idea of it,
    so an entry that is available because of a typo in the action name
    fails here.
    """
    source = script()
    here = client_actions(source)
    services, _runtime, _seeded = _services("dispatch-available")

    with TestClient(build_web_app(services)) as client:
        served = client.get("/keymap.json").json()

    unimplemented: list[tuple[str, str]] = []
    for surface, entries in served.items():
        for entry in entries:
            if not entry["available"]:
                continue
            # `select_span(3)` carries its argument in the name.
            name = str(entry["action"]).partition("(")[0]
            if name not in here and name not in SERVER_ACTIONS:
                unimplemented.append((surface, str(entry["action"])))

    assert unimplemented == []


def test_a_key_the_browser_has_not_got_is_marked_rather_than_silent() -> None:
    """One key is terminal-only, and the press says so."""
    source = script()
    services, _runtime, _seeded = _services("dispatch-unavailable")

    with TestClient(build_web_app(services)) as client:
        served = client.get("/keymap.json").json()

    absent = {
        str(entry["action"])
        for entries in served.values()
        for entry in entries
        if not entry["available"]
    }
    assert absent == {"edit_external"}
    # Nothing claims to implement it, on either side.
    assert absent.isdisjoint(client_actions(source))
    assert absent.isdisjoint(SERVER_ACTIONS)
    # And the dispatcher answers rather than swallowing the press.
    assert "is only in the terminal" in _function(source, "onKeyDown")
    # The help overlay marks it too.
    assert "(terminal only)" in source


def test_the_dispatcher_implements_nothing_no_key_names() -> None:
    """A client action no key reaches is dead code, not a feature."""
    source = script()
    named = {
        binding.action.partition("(")[0]
        for surface in PageSurface
        for binding in bindings_for(surface)
    }

    assert client_actions(source) <= named
    # And the server's own list of them agrees with the script's.
    assert client_actions(source) >= CLIENT_ACTIONS


def test_the_dispatcher_calls_only_functions_it_defines() -> None:
    """``Q`` was dead for exactly this reason.

    ``runServerAction`` was renamed and two call sites — the quit path
    and the mass-deletion confirmation — kept calling ``server``, which
    no longer existed.  Every press down those paths threw a
    ``ReferenceError`` into the console and did nothing at all, and no
    test could see it, because both halves were tested separately and
    each was right.
    """
    source = script()

    assert called_names(source) <= declared_names(source)
    # And the ones preceded by a dot, which that check skips: a typo'd
    # `window.closeTab()` is a method no browser has, and it passed.
    assert browser_calls(source) <= BROWSER_METHODS


def test_everything_that_can_be_typed_into_counts_as_a_field() -> None:
    """Which elements keep a plain key to themselves.

    That a press into one is *left* to it is executed in
    ``tests/test_web_browser.py::test_a_press_into_the_search_field_is_left_to_the_field``,
    which types ``D`` into a search box and requires that nothing was
    trashed.  What a browser test types into is one element, though, so
    the list of the others is worth pinning: a `<select>` or a
    contenteditable left off it would silently hand the page a
    destructive key mid-word.
    """
    typing = _function(script(), "typing")

    for kind in ("HTMLInputElement", "HTMLTextAreaElement", "HTMLSelectElement"):
        assert kind in typing
    assert "isContentEditable" in typing


def test_escape_on_a_form_asks_before_throwing_the_typing_away() -> None:
    """``Escape`` is the one key a field does not keep, so it has to ask.

    ``cancel`` was ``window.history.back()`` and nothing else, so
    Escape in a half-written message discarded it without a word.  The
    terminal has a save-draft prompt for exactly this moment; the page
    asks instead of saving, because a form opened by mistake should file
    nothing and an event form has no draft to file.
    """
    source = script()

    # Escape still reaches the dispatcher from inside a field, which is
    # the whole reason this needs a question.
    assert 'event.key !== "Escape"' in _function(source, "onKeyDown")
    assert "cancel: leaveForm," in source
    leaving = _function(source, "leaveForm")
    # Nothing is asked about a form nobody has touched.
    assert "state.formAtLoad" in leaving
    assert "formState(form)" in leaving
    assert "window.confirm" in leaving
    # Said no, and the page stays where it is.
    assert "return;" in leaving.partition("window.history.back()")[0]
    # And the snapshot the comparison needs is taken when the page loads.
    assert "state.formAtLoad = form ? formState(form) : null;" in _function(
        source,
        "start",
    )


# ----------------------------------------------------------------------
# What the dispatcher asks the server for
# ----------------------------------------------------------------------


def test_every_url_the_module_builds_is_answered_with_what_it_asked_for() -> None:
    """``urls`` says a route and its links cannot drift. They had, twice.

    ``SEARCH = "/search"`` named a route that 404s and ``rows_url`` had
    no callers at all, and nothing noticed either, because the one check
    there was compared only the path in front of the ``?``.  So every
    name in the module is walked here: a path constant has to be a route
    the application serves, and a builder's URL has to come back
    answered *with what it asked for* — which is what catches a renamed
    query parameter, where the server happily answers 200 and shows the
    wrong thing.
    """
    from pony.web import urls

    services, _runtime, seeded = _services("dispatch-urls")
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    ref = MessageRef(account_name="acct", folder_name="INBOX", id=seeded["plain"])
    routes = {
        str(getattr(route, "path", "")) for route in build_web_app(services).routes
    }

    # Every path constant is a route.
    for name in urls.__all__:
        value = getattr(urls, name)
        if isinstance(value, str) and value.startswith("/"):
            assert value in routes, name

    with TestClient(build_web_app(services)) as client:
        # A folder link arrives at that folder, with the query kept.
        listed = client.get(urls.mail_url(folder, q="meeting"))
        assert listed.status_code == 200
        assert page_data(listed.text)["account"] == "acct"
        assert page_data(listed.text)["folder"] == "INBOX"
        assert page_data(listed.text)["query"] == "meeting"
        # A message link opens that message.
        opened = client.get(urls.message_url(ref))
        assert opened.status_code == 200
        assert page_data(opened.text)["message"] == str(ref.id)
        # The HTML version, the raw bytes and an attachment all resolve.
        assert client.get(urls.body_url(ref)).status_code == 200
        assert client.get(urls.raw_url(ref)).status_code == 200
        assert client.get(urls.attachment_url(ref, 1)).status_code in {200, 404}
        # The composer is prefilled from the message the link names.
        writing = client.get(
            urls.compose_url(
                mode="reply",
                account="acct",
                folder="INBOX",
                message_id=ref.id,
            ),
        )
        assert writing.status_code == 200
        assert "Re:" in form_fields(writing.text, "#compose-form")["subject"]
        # The contacts list keeps its search, and the form its record.
        searched = client.get(urls.contacts_url(q="ada"))
        assert page_data(searched.text)["query"] == "ada"
        assert client.get(urls.contact_form_url()).status_code == 200
        # The calendar keeps the view it was asked for.
        viewed = client.get(urls.calendar_url(view="month", date="2026-05-17"))
        assert page_data(viewed.text)["view"] == "month"
        assert page_data(viewed.text)["date"] == "2026-05-17"
        # And the event form opens blank on the day it names.
        blank = client.get(urls.event_form_url(date="2026-05-17"))
        assert blank.status_code == 200
        assert form_fields(blank.text, "#event-form")["starts"].startswith("2026-05-17")
        # The fragment the script refetches, with the open row marked.
        fragment = client.get(
            f"{urls.ROWS}{urls.query(account='acct', folder='INBOX', id=ref.id)}",
        )
        assert fragment.status_code == 200
        assert 'aria-selected="true"' in fragment.text


def test_every_path_the_dispatcher_reaches_for_is_a_route() -> None:
    """A fetch to a path nobody serves is a feature that silently fails."""
    services, _runtime, _seeded = _services("dispatch-paths")
    routes = {
        str(getattr(route, "path", "")) for route in build_web_app(services).routes
    }

    for path in sorted(fetched_paths(script())):
        assert path in routes or any(
            route.startswith(f"{path}/{{") for route in routes
        ), path


def test_the_fields_the_dispatcher_posts_are_fields_an_action_reads() -> None:
    """A prompt that posts a name nobody reads is a prompt for nothing."""
    source = script()
    posted = set(re.findall(r'body\.append\("(\w+)"', source))
    # The three every action is built out of.
    assert posted == {"account", "folder", "id"}
    # Everything a prompt or a button adds travels as `extra`.
    extra = _extra_fields(source)
    read = set(re.findall(r'(?:first|values)\("(\w+)"', _web_sources()))
    assert extra >= {"target", "name", "answer", "uid"}
    assert extra <= read, extra - read
    # The event's delete carries the pair the server looks it up by.
    assert "uid: button.dataset.uid" in source
    assert {"uid", "cal"} <= read


def test_the_surface_every_page_declares_has_a_keymap_of_its_own() -> None:
    """Every page answers to keys, which means its surface is served.

    ``state.keymap = state.keymaps[state.surface]``, so a page whose
    ``data-surface`` the server does not serve answers to nothing at
    all — and a surface served for no page is a table nobody reads.
    """
    services, _runtime, seeded = _services("dispatch-surfaces")

    with TestClient(build_web_app(services)) as client:
        served = set(client.get("/keymap.json").json())
        pages = _pages(client, seeded)

    declared = {page_data(page)["surface"] for page in pages.values()}
    assert declared <= served
    # Every surface is really used by a page; none is served for nothing.
    assert declared == served


# ----------------------------------------------------------------------
# A screen with no keyboard
# ----------------------------------------------------------------------


def test_the_bulk_actions_act_on_the_marks_and_not_on_the_cursor() -> None:
    """Both sides of one rule, which is why the set is not written twice.

    An action whose label says "marked" must refuse when nothing is
    marked rather than taking the cursor -- which starts on the first
    row, so "Delete marked" on a freshly loaded list deleted the first
    contact.  The script decides what to send; the server decides what
    to do with nothing.  A name in one set and not the other puts the
    fallback back.
    """
    source = script()
    listed = re.search(r"const markedOnly = new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert listed is not None, "the script no longer names the marked-only actions"

    assert set(re.findall(r'"([\w_]+)"', listed.group(1))) == set(MARKED_ONLY_ACTIONS)
    assert set(SERVER_ACTIONS) >= MARKED_ONLY_ACTIONS
    # And the body is built from the marks for those, not from targets().
    builder = _function(source, "server")
    assert "markedOnly.has(name)" in builder


def test_every_button_that_names_an_action_goes_through_the_dispatcher() -> None:
    """Scanned off the pages, not kept in a list here."""
    source = script()
    services, _runtime, seeded = _services("dispatch-buttons")

    with TestClient(build_web_app(services)) as client:
        pages = _pages(client, seeded)

    named = {name for page in pages.values() for name in actions(page)}
    assert named
    assert named <= set(SERVER_ACTIONS)
    # Every one of them is wired to an action the dispatcher can send,
    # which is the half reading the source can see.  That a click really
    # posts it is executed in
    # ``tests/test_web_browser.py::test_a_button_runs_the_action_a_key_would``.
    assert "wireActionButtons" in _function(source, "start")
    # And what a button adds to the post is read off the button itself.
    fields = _function(source, "buttonFields")
    for name in ("attachment", "uid", "target", "calendarFrom"):
        assert f"dataset.{name}" in fields


def test_the_operations_a_phone_needs_are_all_reachable_by_tapping() -> None:
    """A phone has no keyboard, so every page offers its own operations.

    What the architecture document promises, checked against the pages
    themselves: the four row operations and Write beside the reader, the
    two bulk operations on the contacts list, Sync in the title bar, New
    event and the view switcher in the calendar, and a search field in
    each list head.
    """
    services, _runtime, seeded = _services("dispatch-touch")

    with TestClient(build_web_app(services)) as client:
        pages = _pages(client, seeded)

    assert actions(pages["message"]) >= {
        "archive",
        "trash",
        "toggle_flagged",
        "mark_unread",
    }
    assert link_to(pages["message"], "Reply")
    assert link_to(pages["list"], "Write")
    assert actions(pages["contacts"]) >= {"delete_marked", "merge_marked"}
    assert link_to(pages["contacts"], "New")
    # Sync is in the chrome, so it is on every page there is.
    for name, page in pages.items():
        assert "sync" in actions(page), name
    # The calendar's own pane: a view each, the date steps, a new event.
    for label in ("Agenda", "Day", "Week", "Month"):
        assert link_to(pages["agenda"], label)
    for element_id in ("prev-date", "today", "next-date", "new-event"):
        assert f'id="{element_id}"' in pages["agenda"]
    # An invitation is answered by tapping an answer, not only by `i`.
    assert set(attribute_values(pages["invitation"], "data-answer")) >= {
        "accept",
        "decline",
        "file",
    }
    # A search field wherever there is a list to search.
    for name in ("list", "contacts", "agenda"):
        assert "q" in form_fields(pages[name], ".search-form"), name
    # And the forms can be submitted and left without a keyboard: a
    # submit button each, beside the way out.
    assert (
        '<button type="submit" form="event-form" class="action primary">Save'
        in (pages["event"])
    )
    assert 'id="delete-event"' in pages["event"]
    assert link_to(pages["event"], "Cancel")
    assert (
        '<button type="submit" form="compose-form" class="action primary">Send'
        in (pages["compose"])
    )
    assert link_to(pages["compose"], "Discard")


def test_the_operations_with_no_button_are_the_ones_that_need_typing() -> None:
    """The documented gap, so that widening it is a deliberate act.

    Every other server action on a surface has a button on one of that
    surface's own pages.  These do not, in four kinds:

    * ``move``, ``copy`` and ``new_folder`` need a word typed before
      they can run at all — the folder to move to, the name to create.
    * ``mark_all_read`` and ``harvest_contacts`` act on the whole
      folder rather than on anything the page is pointing at.
    * ``quit`` has no button on any page: a phone can open the front
      end but cannot stop it without a keyboard.
    * ``delete_event`` is one tap further on — the event's own form
      carries the Delete button, because the server needs the uid and
      the calendar together and a bare action button cannot carry them.
    """
    services, _runtime, seeded = _services("dispatch-gaps")

    with TestClient(build_web_app(services)) as client:
        served = client.get("/keymap.json").json()
        tappable = _by_surface(_pages(client, seeded))

    missing: dict[str, set[str]] = {}
    for surface, entries in served.items():
        for entry in entries:
            name = str(entry["action"])
            if name not in SERVER_ACTIONS:
                continue
            if ALIASES.get(name, name) in tappable.get(surface, set()):
                continue
            missing.setdefault(surface, set()).add(name)

    assert missing == {
        "reader": {
            "mark_all_read",
            "move",
            "copy",
            "new_folder",
            "harvest_contacts",
            "quit",
        },
        "contacts": {"quit"},
        "calendar": {"quit", "delete_event"},
    }


def test_every_page_carries_its_own_keymap_so_no_press_is_dropped() -> None:
    """It used to arrive in a request of its own, fired last.

    ``start()`` attached ``keydown`` and *then* fetched the table, so
    every key pressed before the answer landed matched nothing and was
    dropped without a word.  The page carries it now, from the same
    function ``/keymap.json`` is built from.
    """
    import json

    source = script()
    services, _runtime, seeded = _services("dispatch-inline-keymap")

    with TestClient(build_web_app(services)) as client:
        served = client.get("/keymap.json").json()
        pages = _pages(client, seeded)

    for name, page in pages.items():
        found = re.search(
            r'<script type="application/json" id="keymap">(.*?)</script>',
            page,
            re.DOTALL,
        )
        assert found is not None, name
        carried = json.loads(found.group(1))
        assert carried, name
        # The same table the endpoint serves for that surface.
        assert carried == served[page_data(page)["surface"]], name

    # And the script reads it before it starts listening, rather than
    # fetching it afterwards.
    assert "/keymap.json" not in source
    starting = _function(source, "start")
    assert starting.index("state.keymap = pageKeymap()") < starting.index(
        'document.addEventListener("keydown"',
    )


def test_the_actions_with_neither_a_key_nor_a_button_are_known() -> None:
    """One operation the browser can only be asked for by hand.

    ``mark_read`` is the other half of ``u``, which the reader does by
    opening a message: a real action with tests of its own, reachable
    from no key and no button.  ``import_ics`` used to be here too and
    is now the Import button on a calendar attachment.
    """
    services, _runtime, seeded = _services("dispatch-unreachable")

    with TestClient(build_web_app(services)) as client:
        served = client.get("/keymap.json").json()
        pages = _pages(client, seeded)

    reachable = {
        str(entry["action"]) for entries in served.values() for entry in entries
    }
    reachable |= {name for page in pages.values() for name in actions(page)}
    reachable |= {ALIASES[name] for name in ALIASES if name in reachable}
    reachable |= {"sync_calendar", "delete_event"}

    assert set(SERVER_ACTIONS) - reachable == {"mark_read"}


# ----------------------------------------------------------------------
# Quitting
# ----------------------------------------------------------------------


def test_the_quit_key_is_a_key_on_both_sides() -> None:
    """``Q`` needs an entry in two tables, and had one in neither.

    What it then *does* — ask the server, wait for an answer, close the
    tab, say so where the tab cannot be closed, and answer no further
    keys — is executed in
    ``tests/test_web_browser.py::test_quitting_stops_the_page_as_well_as_the_server``.
    Wrapping ``quit()``'s body in ``if (false) {}`` left every assertion
    that used to be here still holding, which is the shape of the dead
    ``server()`` bug that shipped once already.
    """
    assert "quit" in client_actions(script())
    assert "quit" in SERVER_ACTIONS


# ----------------------------------------------------------------------
# Reading the script
# ----------------------------------------------------------------------


def _function(source: str, name: str) -> str:
    """One function's text, from its signature to its closing brace."""
    head = re.search(rf"^(?:async )?function {re.escape(name)}\(", source, re.MULTILINE)
    assert head is not None, f"no function {name}"
    end = source.index("\n}", head.start())
    return source[head.start() : end]


def _extra_fields(source: str) -> set[str]:
    """The field names a prompt or a button adds to an action's body.

    ``server("move", { target })`` and ``server("respond_invitation", {
    answer: …, calendar: … })`` are the two spellings, so both the
    shorthand and the named form are read.
    """
    found: set[str] = set()
    for literal in re.findall(r'server\("[\w_]+",\s*\{(.*?)\}', source, re.DOTALL):
        for part in literal.split(","):
            token = part.strip().partition(":")[0].strip()
            if token and not token.startswith("..."):
                found.add(token)
    return found


def _web_sources() -> str:
    """Every module of the browser front end, as one text to grep."""
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "src/pony/web"
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py"))
    )


def test_the_event_form_offers_a_sync_at_all() -> None:
    """*Which* half it syncs is executed in the browser test.

    ``tests/test_web_browser.py::test_the_event_form_syncs_the_calendar_and_not_the_mail``
    taps the button on a real event form and requires
    ``/action/sync_calendar``.  What is left here is that the button is
    on the page and the action exists to receive it.
    """
    services, _runtime, seeded = _services("dispatch-event-sync")

    with TestClient(build_web_app(services)) as client:
        pages = _pages(client, seeded)

    assert page_data(pages["event"])["surface"] == "event"
    assert "sync" in actions(pages["event"])
    assert "sync_calendar" in SERVER_ACTIONS
