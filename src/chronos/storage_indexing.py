from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from chronos.domain import (
    CalendarRef,
    ComponentKind,
    ComponentRef,
    LocalStatus,
    ResourceRef,
    StoredComponent,
    VEvent,
    VTodo,
)
from chronos.ical_parser import IcalParseError, ParsedComponent, parse_vcalendar
from chronos.protocols import IndexRepository, MirrorRepository


@dataclass(frozen=True, kw_only=True)
class IndexingResult:
    calendar: CalendarRef
    resources_scanned: int
    components_upserted: int
    components_removed: int
    parse_errors: tuple[str, ...]


def index_calendar(
    *,
    mirror: MirrorRepository,
    index: IndexRepository,
    calendar: CalendarRef,
) -> IndexingResult:
    """Project every `.ics` file in the calendar's mirror into the index.

    Idempotent: re-running after a no-op change produces zero writes at
    the SQL level beyond the deterministic upsert.

    What the file cannot say is carried over from the row it replaces.
    A `.ics` on disk knows nothing about the server -- no ``href``, no
    ``etag`` -- and nothing about what the user has done to it since:
    `build_stored_component` therefore produces an ACTIVE row with no
    handle and no trash timestamp, and `upsert_component` writes every
    column.  Projecting a synced calendar would have cleared the whole
    of its server metadata, resurrected its trashed events and left
    every row looking locally created, which is what ``href IS NULL``
    means to the sync engine.
    """
    resources = mirror.list_resources(calendar.account_name, calendar.calendar_name)
    existing = {comp.ref: comp for comp in index.list_calendar_components(calendar)}
    seen_refs: set[ComponentRef] = set()
    upserts = 0
    errors: list[str] = []

    for resource in resources:
        try:
            raw = mirror.read(resource)
            parsed = parse_vcalendar(raw)
        except IcalParseError as exc:
            errors.append(f"{resource.uid}: {exc}")
            continue
        except OSError as exc:
            errors.append(f"{resource.uid}: {exc}")
            continue

        components = _project_resource(resource, raw, parsed)
        with index.connection():
            for component in components:
                index.upsert_component(
                    _keeping_server_state(component, existing.get(component.ref)),
                )
                seen_refs.add(component.ref)
                upserts += 1

    stale = set(existing) - seen_refs
    removed = 0
    if stale:
        with index.connection():
            for ref in stale:
                index.delete_component(ref)
                removed += 1

    return IndexingResult(
        calendar=calendar,
        resources_scanned=len(resources),
        components_upserted=upserts,
        components_removed=removed,
        parse_errors=tuple(errors),
    )


def _keeping_server_state(
    projected: StoredComponent,
    known: StoredComponent | None,
) -> StoredComponent:
    """*projected*, with what only the index knows taken from *known*.

    The mirror file is the content; the row is the content plus where it
    came from and what has happened to it here.  A projection replaces
    the first and must not touch the second.
    """
    if known is None:
        return projected
    return dataclasses.replace(
        projected,
        href=known.href,
        etag=known.etag,
        local_flags=known.local_flags,
        server_flags=known.server_flags,
        local_status=known.local_status,
        trashed_at=known.trashed_at,
    )


def _project_resource(
    resource: ResourceRef, raw: bytes, parsed: list[ParsedComponent]
) -> list[StoredComponent]:
    if not parsed:
        return []
    fallback_uid = synthetic_uid(resource.account_name, resource.calendar_name, raw)
    now = datetime.now(UTC)
    out: list[StoredComponent] = []
    for component in parsed:
        uid = component.uid or fallback_uid
        ref = ComponentRef(
            account_name=resource.account_name,
            calendar_name=resource.calendar_name,
            uid=uid,
            recurrence_id=component.recurrence_id,
        )
        out.append(build_stored_component(ref, raw, component, now))
    return out


def build_stored_component(
    ref: ComponentRef,
    raw: bytes,
    component: ParsedComponent,
    synced_at: datetime,
) -> StoredComponent:
    if component.kind == ComponentKind.VEVENT:
        return VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=raw,
            summary=component.summary,
            description=component.description,
            location=component.location,
            dtstart=component.dtstart,
            dtend=component.dtend,
            status=component.status,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=synced_at,
        )
    return VTodo(
        ref=ref,
        href=None,
        etag=None,
        raw_ics=raw,
        summary=component.summary,
        description=component.description,
        location=component.location,
        dtstart=component.dtstart,
        due=component.due,
        status=component.status,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=synced_at,
    )


def synthetic_uid(account: str, calendar: str, raw: bytes) -> str:
    digest = hashlib.sha256(f"{account}|{calendar}|".encode() + raw).hexdigest()[:32]
    return f"chronos-syn-{digest}"


__all__ = [
    "IndexingResult",
    "build_stored_component",
    "index_calendar",
    "synthetic_uid",
]
