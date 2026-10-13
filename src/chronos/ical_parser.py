from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from email.utils import parseaddr

from icalendar import Calendar

from chronos.domain import AlarmAction, ComponentKind, ParsedAlarm

# What an address may hold once it has been through `parseaddr`.  No
# separator and no control character: the value is written back into an
# iCalendar property, where either would end the value early.
_ATTENDEE_EMAIL_RE = re.compile(r"^[^@\s,;:\x00-\x1f\x7f]+@[^@\s,;:\x00-\x1f\x7f]+$")


class IcalParseError(ValueError):
    pass


@dataclass(frozen=True, kw_only=True)
class ParsedComponent:
    kind: ComponentKind
    uid: str | None
    recurrence_id: str | None
    summary: str | None
    description: str | None
    location: str | None
    dtstart: datetime | None
    dtend: datetime | None
    due: datetime | None
    status: str | None
    sequence: int | None


def parse_vcalendar(raw: bytes) -> list[ParsedComponent]:
    try:
        cal = Calendar.from_ical(raw)
    except ValueError as exc:
        raise IcalParseError(f"invalid iCalendar data: {exc}") from exc
    out: list[ParsedComponent] = []
    for sub in cal.walk("VEVENT"):  # pyright: ignore[reportUnknownMemberType]
        out.append(_project(sub, ComponentKind.VEVENT))
    for sub in cal.walk("VTODO"):  # pyright: ignore[reportUnknownMemberType]
        out.append(_project(sub, ComponentKind.VTODO))
    return out


def parse_method(raw: bytes) -> str | None:
    """Return the upper-cased VCALENDAR ``METHOD`` (iTIP) or ``None``.

    ``METHOD:REQUEST``/``CANCEL``/``REPLY``/``PUBLISH`` distinguishes an
    iTIP scheduling message from a plain calendar export.  ``None`` means
    no method was declared (treat as a plain ``PUBLISH``-style import).
    """
    try:
        cal = Calendar.from_ical(raw)
    except ValueError as exc:
        raise IcalParseError(f"invalid iCalendar data: {exc}") from exc
    method = _call_get(cal, "METHOD")
    if method is None:
        return None
    return str(method).strip().upper() or None


def _project(component: object, kind: ComponentKind) -> ParsedComponent:
    return ParsedComponent(
        kind=kind,
        uid=_get_str(component, "UID"),
        recurrence_id=_get_recurrence_id(component),
        summary=_get_str(component, "SUMMARY"),
        description=_get_str(component, "DESCRIPTION"),
        location=_get_str(component, "LOCATION"),
        dtstart=_get_datetime(component, "DTSTART"),
        dtend=_end_of(component, "DTEND"),
        due=_end_of(component, "DUE"),
        status=_get_str(component, "STATUS"),
        sequence=_get_int(component, "SEQUENCE"),
    )


def _get_int(component: object, key: str) -> int | None:
    value = _call_get(component, key)
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _get_str(component: object, key: str) -> str | None:
    value = _call_get(component, key)
    if value is None:
        return None
    return str(value)


def _get_datetime(component: object, key: str) -> datetime | None:
    value = _decoded(component, key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def _end_of(component: object, key: str) -> datetime | None:
    """When the component ends: `key` if present, else DTSTART + DURATION.

    RFC 5545 makes DTEND and DURATION alternatives — "either" for a
    VEVENT, and DUE or DTSTART+DURATION for a VTODO — and plenty of
    senders use the second form. Reading only DTEND left those events
    with no end at all: the agenda showed a start and a blank duration,
    and the timeline drew nothing, because a span that ends when it
    begins covers no slot.

    A DURATION without a DTSTART to anchor it, or one that is not a
    duration at all, is ignored rather than guessed at.
    """
    explicit = _get_datetime(component, key)
    if explicit is not None:
        return explicit
    duration = _decoded(component, "DURATION")
    if not isinstance(duration, timedelta):
        return None
    anchor = _get_datetime(component, "DTSTART")
    if anchor is None:
        return None
    return anchor + duration


def _get_recurrence_id(component: object) -> str | None:
    dt = _get_datetime(component, "RECURRENCE-ID")
    if dt is None:
        return None
    return dt.isoformat()


def parse_recurrence_id(component: object) -> str | None:
    """Return a component's RECURRENCE-ID as a UTC ISO string, or ``None``.

    Public wrapper over the internal projection so callers handling raw
    icalendar subcomponents (e.g. ingest's instance-cancel path) compare
    RECURRENCE-IDs in the same normalized form the index stores.
    """
    return _get_recurrence_id(component)


def to_utc(dt: datetime) -> datetime:
    """*dt* in UTC, reading a naive value as local wall time.

    The zone is attached before the conversion rather than left to
    ``astimezone``, which resolves a naive value through the platform's
    localtime: that raises ``OSError: [Errno 22]`` on Windows for dates
    far enough from now, which is why :func:`localize_naive` exists at
    all.  Attaching a fixed offset first gives the same answer for
    every date a calendar can hold.
    """
    if dt.tzinfo is None:
        dt = localize_naive(dt)
    return dt.astimezone(UTC)


def localize_naive(value: datetime) -> datetime:
    """Attach the local zone to a naive datetime without converting it.

    A DTSTART with neither TZID nor ``Z`` is *floating*: it means local
    wall time, which is all this records.  Shared with
    :mod:`chronos.recurrence`, which anchors rrule expansion on the same
    reading.
    """
    return value.replace(tzinfo=local_tzinfo())


def local_tzinfo() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    return tz or UTC


def _call_get(component: object, key: str) -> object:
    getter = getattr(component, "get", None)
    if getter is None:
        return None
    return getter(key)


def _decoded(component: object, key: str) -> object:
    contains = getattr(component, "__contains__", None)
    if contains is None or not contains(key):
        return None
    decoder = getattr(component, "decoded", None)
    if decoder is None:
        return None
    try:
        return decoder(key)
    except (KeyError, ValueError):
        return None


_SUPPORTED_ALARM_ACTIONS = frozenset({"DISPLAY", "AUDIO"})


def extract_alarm_triggers(raw_ics: bytes, uid: str) -> list[ParsedAlarm]:
    """Return all DISPLAY/AUDIO VALARM triggers for the master component with ``uid``.

    Only examines the master (no RECURRENCE-ID) — alarms are defined on the
    master and inherited by every recurrence instance.  Returns ``[]`` on any
    parse error or when no supported alarms exist.
    """
    try:
        cal = Calendar.from_ical(raw_ics)
    except ValueError:
        return []
    out: list[ParsedAlarm] = []
    for sub in cal.walk():  # pyright: ignore[reportUnknownMemberType]
        name = _component_name(sub)
        if name not in ("VEVENT", "VTODO"):
            continue
        if _component_get_str(sub, "UID") != uid:
            continue
        if _component_has(sub, "RECURRENCE-ID"):
            continue
        for valarm in sub.walk("VALARM"):  # pyright: ignore[reportUnknownMemberType]
            parsed = _parse_valarm(valarm)
            if parsed is not None:
                out.append(parsed)
        break  # found our master; stop scanning
    return out


def extract_last_modified(raw_ics: bytes, uid: str) -> datetime | None:
    """`LAST-MODIFIED` of the master component with ``uid``, in UTC.

    The tie-break when two versions of an event carry the same
    SEQUENCE: whoever wrote last wins. Absent from plenty of real
    calendars, so a caller has to cope with None.
    """
    component = _find_master_component(raw_ics, uid)
    if component is None:
        return None
    return _get_datetime(component, "LAST-MODIFIED")


def extract_sequence(raw_ics: bytes, uid: str) -> int:
    """`SEQUENCE` of the master component with ``uid``; 0 when absent.

    RFC 5545 §3.8.7.4 starts a component at 0 and has the organiser
    raise it on every substantive change, which is what makes it the
    first thing to compare when two sides both changed.
    """
    component = _find_master_component(raw_ics, uid)
    if component is None:
        return 0
    return _get_int(component, "SEQUENCE") or 0


def extract_attendees(raw_ics: bytes, uid: str) -> tuple[str, ...]:
    """Attendee email addresses from the master component with ``uid``.

    Each value goes through :func:`normalize_attendee_email`, so what
    comes out is what `build_event_ics` can write back.  A value that is
    not an address -- a server sending ``MAILTO:team room@example.com``
    -- is dropped rather than carried: these addresses are handed
    straight back to the writer by ``calendar edit``, which refused the
    whole edit over one of them.
    """
    component = _find_master_component(raw_ics, uid)
    if component is None:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for value in _component_values(component, "ATTENDEE"):
        email = _mailto_value(value)
        if email is None:
            continue
        key = email.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(email)
    return tuple(out)


def extract_organizer(raw_ics: bytes, uid: str) -> str | None:
    """Return the organizer email address from the master component with ``uid``."""
    component = _find_master_component(raw_ics, uid)
    if component is None:
        return None
    value = _call_get(component, "ORGANIZER")
    if value is None:
        return None
    return _mailto_value(value)


def _parse_valarm(valarm: object) -> ParsedAlarm | None:
    action_raw = str(_call_get(valarm, "ACTION") or "DISPLAY").upper()
    if action_raw not in _SUPPORTED_ALARM_ACTIONS:
        return None
    action = AlarmAction(action_raw)

    trigger_val = _decoded(valarm, "TRIGGER")
    if trigger_val is None:
        return None

    if isinstance(trigger_val, timedelta):
        raw_prop = _call_get(valarm, "TRIGGER")
        params = getattr(raw_prop, "params", {})
        related = str(params.get("RELATED", "START")).upper()
        return ParsedAlarm(
            action=action,
            trigger_offset=trigger_val,
            trigger_related=related,
            description=_get_str(valarm, "DESCRIPTION"),
        )
    if isinstance(trigger_val, datetime):
        utc_trigger = (
            trigger_val.astimezone(UTC)
            if trigger_val.tzinfo
            else trigger_val.replace(tzinfo=UTC)
        )
        return ParsedAlarm(
            action=action,
            trigger_offset=utc_trigger,
            trigger_related="START",
            description=_get_str(valarm, "DESCRIPTION"),
        )
    return None


def _component_name(component: object) -> str:
    return str(getattr(component, "name", ""))


def _component_get_str(component: object, key: str) -> str | None:
    value = _call_get(component, key)
    if value is None:
        return None
    return str(value)


def _component_has(component: object, key: str) -> bool:
    contains = getattr(component, "__contains__", None)
    if contains is None:
        return False
    return bool(contains(key))


def _find_master_component(raw_ics: bytes, uid: str) -> object | None:
    try:
        cal = Calendar.from_ical(raw_ics)
    except ValueError:
        return None
    for sub in cal.walk():  # pyright: ignore[reportUnknownMemberType]
        name = _component_name(sub)
        if name not in ("VEVENT", "VTODO"):
            continue
        if _component_get_str(sub, "UID") != uid:
            continue
        if _component_has(sub, "RECURRENCE-ID"):
            continue
        return sub
    return None


def _component_values(component: object, key: str) -> tuple[object, ...]:
    value = _call_get(component, key)
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def normalize_attendee_email(value: str) -> str:
    """An attendee or organizer value as a bare email address.

    Accepts a plain address, a ``mailto:`` value and simple
    ``Name <email>`` input, which is what both an ``ATTENDEE`` property
    and a form field can hold.  Separators and control characters are
    refused because the result is serialized straight back into an
    iCalendar property, and because an address with a space or a
    semicolon in it is not one.

    One rule for reading and for writing: the reader used to require
    only an ``@``, so an event whose server-supplied ``ATTENDEE`` had a
    space in it could be displayed and then not edited at all --
    ``build_event_ics`` refused the value the reader had just handed it.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("attendee email cannot be blank")
    _display_name, parsed = parseaddr(raw)
    email = (parsed or raw).strip()
    if email.lower().startswith("mailto:"):
        email = email[7:].strip()
    if not _ATTENDEE_EMAIL_RE.fullmatch(email):
        raise ValueError(f"invalid attendee email: {value!r}")
    return email


def _mailto_value(value: object) -> str | None:
    """*value* as an email address, or ``None`` when it is not one."""
    try:
        return normalize_attendee_email(str(value))
    except ValueError:
        return None
