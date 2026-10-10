"""The calendar's four views, and its event form, in a browser.

The projections are :mod:`chronos.views`', tested in the calendar's own
suite; what is checked here is that each view renders the rows it should
in the shape a grid needs, that the view and the date live in the URL,
and that the form saves through :func:`chronos.event_actions.save_event`.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from starlette.testclient import TestClient
from tui_helpers import (
    make_calendar_runtime,
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
)

from pony.web import WebServices, build_web_app

if TYPE_CHECKING:
    from pony.calendar import CalendarRuntime

ACCOUNT = "personal"
CALENDAR = "work"


def _ics(
    uid: str,
    start: datetime,
    *,
    summary: str = "Planning review",
    minutes: int = 45,
    location: str = "Room 4",
) -> bytes:
    end = start + timedelta(minutes=minutes)
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
                f"DTEND:{end.astimezone(UTC):%Y%m%dT%H%M%SZ}",
                f"SUMMARY:{summary}",
                f"LOCATION:{location}",
                "END:VEVENT",
                "END:VCALENDAR",
            ),
        )
        + "\r\n"
    ).encode()


def _tomorrow_at(hour: int) -> datetime:
    return (datetime.now(UTC).astimezone() + timedelta(days=1)).replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
    )


def _services(
    label: str,
    *,
    events: tuple[tuple[str, bytes], ...] = (),
) -> tuple[WebServices, CalendarRuntime]:
    paths = make_tmp_paths(label)
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    runtime = make_calendar_runtime(
        make_tmp_paths(f"{label}-cal"),
        account_name=ACCOUNT,
        calendar_name=CALENDAR,
        events=events,
    )
    (runtime.mirror.root / ACCOUNT / CALENDAR).mkdir(parents=True, exist_ok=True)
    return (
        WebServices(
            config=config,
            index=index,
            mirrors=make_mirrors(config),
            contacts=index,
            calendar=runtime,
        ),
        runtime,
    )


def _client(services: WebServices) -> TestClient:
    return TestClient(build_web_app(services))


# ----------------------------------------------------------------------
# The views
# ----------------------------------------------------------------------


def test_the_agenda_lists_what_is_coming_with_column_headings() -> None:
    start = _tomorrow_at(10)
    services, _runtime = _services(
        "cal-agenda",
        events=(("agenda@example.com", _ics("agenda@example.com", start)),),
    )

    # The window is pinned rather than left to the default, which is a
    # Monday-based week and so depends on which day the test runs.
    with _client(services) as client:
        body = client.get(f"/calendar?window=day&date={start.date()}").text

    assert "Planning review" in body
    assert "Room 4" in body
    # Tomorrow is named, not dated, exactly as the agenda screen names it.
    assert "Tomorrow" in body
    # Headings, as every other list in the program has.
    assert '<span class="summary">Event</span>' in body
    assert '<span class="time">Time</span>' in body


def test_the_month_grid_is_a_grid_of_days() -> None:
    start = _tomorrow_at(10)
    services, _runtime = _services(
        "cal-month",
        events=(("month@example.com", _ics("month@example.com", start)),),
    )

    with _client(services) as client:
        body = client.get("/calendar?view=month").text

    assert 'class="month"' in body
    assert "--weeks:" in body
    # Seven weekday headings and a cell per day, with the event in its
    # own cell and today marked.
    assert body.count('class="day-head"') == 7
    assert f'data-date="{start.date()}"' in body
    assert "month-cell today" in body
    assert "Planning review" in body


def test_the_day_view_places_an_event_on_the_hour_axis() -> None:
    start = _tomorrow_at(10)
    services, _runtime = _services(
        "cal-day",
        events=(("day@example.com", _ics("day@example.com", start)),),
    )

    with _client(services) as client:
        body = client.get(f"/calendar?view=day&date={start.date()}").text

    assert 'class="timeline"' in body
    # The hour window and the block's place in it are both data.
    assert "--hours:" in body
    assert "top:" in body
    assert "height:" in body
    assert "10:00" in body


def test_the_week_view_is_seven_columns_wide() -> None:
    start = _tomorrow_at(10)
    services, _runtime = _services(
        "cal-week",
        events=(("week@example.com", _ics("week@example.com", start)),),
    )

    with _client(services) as client:
        body = client.get(f"/calendar?view=grid&span=7&date={start.date()}").text

    assert "--columns:7" in body
    assert "Planning review" in body


def test_an_all_day_event_goes_in_the_banner_not_the_axis() -> None:
    first = (datetime.now(UTC).astimezone() + timedelta(days=1)).date()
    all_day = (
        "\r\n".join(
            (
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//tests//EN",
                "BEGIN:VEVENT",
                "UID:allday@example.com",
                f"DTSTAMP:{first:%Y%m%d}T000000Z",
                f"DTSTART;VALUE=DATE:{first:%Y%m%d}",
                f"DTEND;VALUE=DATE:{first + timedelta(days=1):%Y%m%d}",
                "SUMMARY:Public holiday",
                "END:VEVENT",
                "END:VCALENDAR",
            ),
        )
        + "\r\n"
    ).encode()
    services, _runtime = _services(
        "cal-all-day",
        events=(("allday@example.com", all_day),),
    )

    with _client(services) as client:
        body = client.get(f"/calendar?view=day&date={first}").text

    assert "all-day-banner" in body
    assert "Public holiday" in body


def test_the_view_and_the_date_live_in_the_url() -> None:
    services, _runtime = _services("cal-links")

    with _client(services) as client:
        body = client.get("/calendar?view=month&date=2026-05-17").text

    assert "May 2026" in body
    # Prev, Today and Next are links, so the back button means what it
    # looks like it means.
    assert 'id="prev-date"' in body
    assert 'id="next-date"' in body
    assert 'id="today"' in body
    assert "view=agenda" in body


def test_a_nonsense_date_or_view_falls_back_rather_than_failing() -> None:
    services, _runtime = _services("cal-bad-params")

    with _client(services) as client:
        response = client.get("/calendar?view=sideways&date=yesterday&span=99")

    assert response.status_code == 200
    assert "Agenda" in response.text


def test_the_calendar_list_narrows_to_one_and_back() -> None:
    start = _tomorrow_at(10)
    services, _runtime = _services(
        "cal-selection",
        events=(("sel@example.com", _ics("sel@example.com", start)),),
    )

    day = f"window=day&date={start.date()}"
    with _client(services) as client:
        body = client.get(f"/calendar?{day}").text
        only = client.get(f"/calendar?{day}&cal={ACCOUNT}/{CALENDAR}").text
        elsewhere = client.get(f"/calendar?{day}&cal=personal/other").text

    assert CALENDAR in body
    assert "Planning review" in only
    # A calendar nobody is showing hides its events.
    assert "Planning review" not in elsewhere


def test_searching_the_calendar_narrows_what_is_on_screen() -> None:
    start = _tomorrow_at(10)
    services, _runtime = _services(
        "cal-search",
        events=(
            ("one@example.com", _ics("one@example.com", start, summary="Standup")),
            (
                "two@example.com",
                _ics("two@example.com", start, summary="Retrospective"),
            ),
        ),
    )

    with _client(services) as client:
        body = client.get(
            f"/calendar?window=day&date={start.date()}&q=retro",
        ).text

    assert "Search: retro" in body
    assert "Retrospective" in body
    assert "Standup" not in body


def test_a_front_end_without_a_calendar_says_so() -> None:
    services, _runtime = _services("cal-none")
    bare = dataclasses.replace(services, calendar=None)

    with _client(bare) as client:
        body = client.get("/calendar").text

    assert "No calendar configured" in body


# ----------------------------------------------------------------------
# The event form
# ----------------------------------------------------------------------


def test_the_form_opens_blank_on_the_viewed_day() -> None:
    services, _runtime = _services("cal-form-new")

    with _client(services) as client:
        body = client.get("/calendar/event?date=2026-05-17").text

    assert "New event" in body
    assert 'value="2026-05-17T09:00"' in body
    assert f'value="{ACCOUNT}/{CALENDAR}"' in body


def test_the_form_opens_on_an_existing_event() -> None:
    start = _tomorrow_at(10)
    services, _runtime = _services(
        "cal-form-edit",
        events=(("edit@example.com", _ics("edit@example.com", start)),),
    )

    with _client(services) as client:
        body = client.get("/calendar/event?uid=edit@example.com").text

    assert 'value="Planning review"' in body
    assert 'value="Room 4"' in body
    assert 'id="delete-event"' in body


def test_an_event_that_is_not_there_is_a_404() -> None:
    services, _runtime = _services("cal-form-missing")

    with _client(services) as client:
        assert client.get("/calendar/event?uid=nope@example.com").status_code == 404


def test_saving_the_form_creates_the_event() -> None:
    services, runtime = _services("cal-form-save")

    with _client(services) as client:
        response = client.post(
            "/calendar/event",
            data={
                "uid": "",
                "target": f"{ACCOUNT}/{CALENDAR}",
                "summary": "Design review",
                "starts": "2026-05-17T14:00",
                "ends": "2026-05-17T15:00",
                "location": "Room 2",
                "attendees": "ada@example.test, bob@example.test",
                "description": "Bring sketches.",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Created" in response.headers["location"]
    from chronos.views import (
        CalendarSelection,
        all_calendar_refs,
        day_window,
        gather_occurrences,
    )

    rows = gather_occurrences(
        index=runtime.index,
        calendars=all_calendar_refs(runtime.config, runtime.mirror),
        selection=CalendarSelection(refs=frozenset()),
        window=day_window(date(2026, 5, 17)),
    )
    assert [row.component.summary for row in rows] == ["Design review"]


def test_saving_an_event_with_no_summary_keeps_the_form() -> None:
    services, _runtime = _services("cal-form-no-summary")

    with _client(services) as client:
        response = client.post(
            "/calendar/event",
            data={
                "uid": "",
                "target": f"{ACCOUNT}/{CALENDAR}",
                "summary": "  ",
                "starts": "2026-05-17T14:00",
            },
        )

    assert response.status_code == 200
    assert "An event needs a summary." in response.text


def test_an_event_that_ends_before_it_starts_is_refused() -> None:
    services, _runtime = _services("cal-form-backwards")

    with _client(services) as client:
        response = client.post(
            "/calendar/event",
            data={
                "uid": "",
                "target": f"{ACCOUNT}/{CALENDAR}",
                "summary": "Time travel",
                "starts": "2026-05-17T14:00",
                "ends": "2026-05-17T13:00",
            },
        )

    assert "The event ends before it starts." in response.text


def test_an_event_can_be_deleted_from_the_browser() -> None:
    start = _tomorrow_at(10)
    services, runtime = _services(
        "cal-delete",
        events=(("gone@example.com", _ics("gone@example.com", start)),),
    )

    with _client(services) as client:
        response = client.post(
            "/action/delete_event",
            data={"uid": "gone@example.com"},
        )

    assert response.json()["changed"] == 1
    (component,) = runtime.index.list_components_by_uid("gone@example.com")
    assert component.local_status.value == "trashed"


def test_deleting_an_event_that_is_gone_says_so() -> None:
    services, _runtime = _services("cal-delete-missing")

    with _client(services) as client:
        response = client.post("/action/delete_event", data={"uid": "nope"})

    assert response.json()["notices"][0]["text"] == "That event is no longer here."


def test_pasted_icalendar_text_can_be_imported() -> None:
    services, runtime = _services("cal-import")
    start = _tomorrow_at(9)

    with _client(services) as client:
        response = client.post(
            "/action/import_ics",
            data={
                "target": f"{ACCOUNT}/{CALENDAR}",
                "ics": _ics("imported@example.com", start, summary="Imported").decode(),
            },
        )

    assert response.json()["changed"] == 1
    assert list(runtime.index.list_components_by_uid("imported@example.com"))


def test_importing_nothing_says_what_to_paste() -> None:
    services, _runtime = _services("cal-import-empty")

    with _client(services) as client:
        response = client.post(
            "/action/import_ics",
            data={"target": f"{ACCOUNT}/{CALENDAR}", "ics": "  "},
        )

    assert response.json()["notices"][0]["text"] == (
        "Paste the iCalendar text to import."
    )


# ----------------------------------------------------------------------
# Syncing the calendar
# ----------------------------------------------------------------------


def test_the_calendar_can_be_synced_from_the_browser() -> None:
    from unittest.mock import patch

    from chronos.domain import SyncResult

    services, _runtime = _services("cal-sync")
    result = SyncResult(
        account_name=ACCOUNT,
        calendars_synced=1,
        components_added=2,
        components_updated=1,
        components_removed=0,
        errors=(),
    )

    with (
        patch("chronos.cli.build_sync_runner", return_value=lambda: (result,)),
        _client(services) as client,
    ):
        response = client.post("/action/sync_calendar", data={})

    assert response.json() == {
        "changed": 3,
        "notices": [
            {
                "text": "Calendar sync complete.  +2 added, 1 updated, 0 removed",
                "severity": "information",
            },
        ],
    }


def test_a_calendar_sync_error_is_reported() -> None:
    from unittest.mock import patch

    from chronos.domain import SyncResult

    services, _runtime = _services("cal-sync-error")
    result = SyncResult(
        account_name=ACCOUNT,
        calendars_synced=0,
        components_added=0,
        components_updated=0,
        components_removed=0,
        errors=("the server refused the credentials",),
    )

    with (
        patch("chronos.cli.build_sync_runner", return_value=lambda: (result,)),
        _client(services) as client,
    ):
        response = client.post("/action/sync_calendar", data={})

    texts = [notice["text"] for notice in response.json()["notices"]]
    assert "the server refused the credentials" in texts


def test_syncing_a_calendar_that_is_not_configured_says_so() -> None:
    services, _runtime = _services("cal-sync-none")
    bare = dataclasses.replace(services, calendar=None)

    with _client(bare) as client:
        response = client.post("/action/sync_calendar", data={})

    assert response.json()["notices"][0]["text"] == "No calendar configured."


def test_editing_an_event_keeps_the_reminder_it_had() -> None:
    """Silently dropping an alarm on save would be data loss."""
    start = _tomorrow_at(10)
    with_alarm = _ics("alarm@example.com", start).replace(
        b"END:VEVENT",
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\n"
        b"DESCRIPTION:Soon\r\nEND:VALARM\r\nEND:VEVENT",
    )
    services, runtime = _services(
        "cal-alarm",
        events=(("alarm@example.com", with_alarm),),
    )

    with _client(services) as client:
        form = client.get("/calendar/event?uid=alarm@example.com").text
        client.post(
            "/calendar/event",
            data={
                "uid": "alarm@example.com",
                "target": f"{ACCOUNT}/{CALENDAR}",
                "summary": "Planning review",
                "starts": form.split('name="starts" id="starts" value="')[1][:16],
                "ends": form.split('name="ends" id="ends" value="')[1][:16],
                "reminders": "15",
            },
            follow_redirects=False,
        )

    assert 'name="reminders" id="reminders" value="15"' in form
    (saved,) = runtime.index.list_components_by_uid("alarm@example.com")
    assert b"TRIGGER:-PT15M" in saved.raw_ics


def test_several_calendars_can_be_shown_at_once() -> None:
    """Clicking one narrows, clicking another adds — as the panel does."""
    start = _tomorrow_at(10)
    services, runtime = _services(
        "cal-multi",
        events=(("multi@example.com", _ics("multi@example.com", start)),),
    )
    (runtime.mirror.root / ACCOUNT / "home").mkdir(parents=True, exist_ok=True)

    day = f"window=day&date={start.date()}"
    with _client(services) as client:
        narrowed = client.get(f"/calendar?{day}&cal={ACCOUNT}/{CALENDAR}").text
        both = client.get(
            f"/calendar?{day}&cal={ACCOUNT}/{CALENDAR}&cal={ACCOUNT}/home",
        ).text

    # The one that is shown links to dropping it; the other to adding it.
    assert f"cal={ACCOUNT}%2Fhome" in narrowed.replace("&amp;", "&")
    assert "Planning review" in both
    assert both.count('class="folder current"') == 2


def test_each_agenda_window_asks_the_index_for_its_own_range() -> None:
    """`d`, `w` and `m` are the terminal's three agenda windows."""
    start = _tomorrow_at(10)
    services, _runtime = _services(
        "cal-windows",
        events=(("window@example.com", _ics("window@example.com", start)),),
    )
    # A month away: inside the month window only when the month happens
    # to contain it, so the day window is the one that must exclude it.
    with _client(services) as client:
        day = client.get(f"/calendar?window=day&date={start.date()}").text
        week = client.get(f"/calendar?window=week&date={start.date()}").text
        month = client.get(f"/calendar?window=month&date={start.date()}").text
        elsewhere = client.get("/calendar?window=day&date=2001-01-01").text

    for body in (day, week, month):
        assert "Planning review" in body
    assert "Planning review" not in elsewhere
    assert "Nothing in this window." in elsewhere
