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
    actions,
    attribute_values,
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
from pony.domain import Contact, FolderRef
from pony.web import WebServices, build_web_app
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


def _services(label: str) -> tuple[WebServices, CalendarRuntime, int, int]:
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
    return services, runtime, plain.id, invited.id


def _pages(client: TestClient, plain: int, invited: int) -> dict[str, str]:
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
            f"/message?account=acct&folder=INBOX&id={plain}",
        ).text,
        "invitation": client.get(
            f"/message?account=acct&folder=INBOX&id={invited}",
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
    services, _runtime, _plain, _invited = _services("dispatch-tables")

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
    services, _runtime, _plain, _invited = _services("dispatch-spelling")

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


def test_the_dispatcher_spells_a_press_the_way_the_keymap_does() -> None:
    """Both sides of the normalisation, so neither drifts alone."""
    source = script()
    pressed = _function(source, "pressed")

    # ctrl, then alt, then shift — the order `MODIFIER_ORDER` names.
    pushes = re.findall(r'parts\.push\("(\w+)"\)', pressed)
    assert pushes == list(MODIFIER_ORDER)
    # Shift only in front of a named key.
    assert "event.key.length > 1" in pressed
    tail = 'parts.push(event.key);\n  return parts.join("+");'
    assert pressed.rstrip().endswith(tail)
    # And the keymap's own translation says the same thing.
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
    services, _runtime, _plain, _invited = _services("dispatch-available")

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
    services, _runtime, _plain, _invited = _services("dispatch-unavailable")

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


def test_a_press_into_a_field_belongs_to_the_field() -> None:
    """Typing ``D`` into a recipient must not trash the open message."""
    guard = _function(script(), "onKeyDown")

    assert "inField" in guard
    # A plain key is the field's; a chord and Escape are still ours.
    assert "!event.ctrlKey && !event.altKey" in guard
    assert 'event.key !== "Escape"' in guard
    typing = _function(script(), "typing")
    for kind in ("HTMLInputElement", "HTMLTextAreaElement", "HTMLSelectElement"):
        assert kind in typing
    assert "isContentEditable" in typing


# ----------------------------------------------------------------------
# What the dispatcher asks the server for
# ----------------------------------------------------------------------


def test_every_path_the_dispatcher_reaches_for_is_a_route() -> None:
    """A fetch to a path nobody serves is a feature that silently fails."""
    services, _runtime, _plain, _invited = _services("dispatch-paths")
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
    services, _runtime, plain, invited = _services("dispatch-surfaces")

    with TestClient(build_web_app(services)) as client:
        served = set(client.get("/keymap.json").json())
        pages = _pages(client, plain, invited)

    declared = {page_data(page)["surface"] for page in pages.values()}
    assert declared <= served
    # Every surface is really used by a page; none is served for nothing.
    assert declared == served


# ----------------------------------------------------------------------
# A screen with no keyboard
# ----------------------------------------------------------------------


def test_every_button_that_names_an_action_goes_through_the_dispatcher() -> None:
    """Scanned off the pages, not kept in a list here."""
    source = script()
    services, _runtime, plain, invited = _services("dispatch-buttons")

    with TestClient(build_web_app(services)) as client:
        pages = _pages(client, plain, invited)

    named = {name for page in pages.values() for name in actions(page)}
    assert named
    assert named <= set(SERVER_ACTIONS)
    # Every one of them is clicked into the same `server()` call a key is.
    wiring = _function(source, "wireActionButtons")
    assert "[data-action]" in wiring
    assert "server(button.dataset.action)" in wiring


def test_the_operations_a_phone_needs_are_all_reachable_by_tapping() -> None:
    """A phone has no keyboard, so every page offers its own operations.

    What the architecture document promises, checked against the pages
    themselves: the four row operations and Write beside the reader, the
    two bulk operations on the contacts list, Sync in the title bar, New
    event and the view switcher in the calendar, and a search field in
    each list head.
    """
    services, _runtime, plain, invited = _services("dispatch-touch")

    with TestClient(build_web_app(services)) as client:
        pages = _pages(client, plain, invited)

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
    services, _runtime, plain, invited = _services("dispatch-gaps")

    with TestClient(build_web_app(services)) as client:
        served = client.get("/keymap.json").json()
        tappable = _by_surface(_pages(client, plain, invited))

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


def test_the_actions_with_neither_a_key_nor_a_button_are_known() -> None:
    """Two operations the browser can only be asked for by hand.

    ``mark_read`` is the other half of ``u``, which the reader does by
    opening a message, and ``import_ics`` is the calendar's paste-an-ics
    import, which has no form on any page yet.  Both are real actions
    with tests of their own; neither is reachable from the interface.
    """
    services, _runtime, plain, invited = _services("dispatch-unreachable")

    with TestClient(build_web_app(services)) as client:
        served = client.get("/keymap.json").json()
        pages = _pages(client, plain, invited)

    reachable = {
        str(entry["action"]) for entries in served.values() for entry in entries
    }
    reachable |= {name for page in pages.values() for name in actions(page)}
    reachable |= {ALIASES[name] for name in ALIASES if name in reachable}
    reachable |= {"sync_calendar", "delete_event"}

    assert set(SERVER_ACTIONS) - reachable == {"mark_read", "import_ics"}


# ----------------------------------------------------------------------
# Quitting
# ----------------------------------------------------------------------


def test_the_quit_key_stops_the_server_and_then_closes_the_page() -> None:
    """Both halves, and the order they have to happen in.

    The server half is ``tests/test_web_app.py``'s; what is pinned here
    is the page's: that it asks before closing, that it closes only on a
    server that really stopped, and that it stops answering keys
    afterwards instead of posting into a socket that has gone.
    """
    source = script()
    quit_path = _function(source, "quit")

    assert "quit" in client_actions(source)
    assert "quit" in SERVER_ACTIONS
    assert 'await server("quit")' in quit_path
    # Nothing happens if the server would not stop.
    assert "outcome.changed === 0) return" in quit_path
    # Then the tab, and a word if the browser refuses to close it.
    assert quit_path.index("state.quitting = true") < quit_path.index("window.close()")
    assert "You can close this tab" in quit_path
    # A page whose server has gone answers no more keys.
    assert "state.quitting" in _function(source, "onKeyDown")


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
