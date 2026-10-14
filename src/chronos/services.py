from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from chronos.authorization import Authorization
from chronos.caldav import CalDAVError, CalDAVHttpSession
from chronos.credentials import CredentialResolutionError
from chronos.domain import (
    AccountConfig,
    AppConfig,
    CalendarRef,
    ComponentKind,
    StoredComponent,
    SyncResult,
    VEvent,
    VTodo,
)
from chronos.ical_parser import IcalParseError, parse_vcalendar
from chronos.locking import SyncLockError, acquire_sync_lock
from chronos.oauth import OAuthError
from chronos.paths import CalendarPaths
from chronos.protocols import (
    CalDAVSession,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from chronos.sync import SyncCancelled, sync_account

SessionFactory = Callable[[AccountConfig, Authorization], CalDAVSession]

# ---------------------------------------------------------------------------
# What an operation works over
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarServices:
    """The stores a calendar operation works over, and where they are.

    One bundle, so a front end hands over the same thing for a sync, a
    doctor run or an event action instead of six arguments in an order
    each caller gets to choose.  It holds no stream and no screen: what
    an operation has to say it returns, and reporting it belongs to
    whoever asked.  That is the line this type is here to keep -- the
    sync runner below used to take the *command line's* context, so the
    terminal UI and the browser both imported a CLI module to run a
    sync.
    """

    config: AppConfig
    mirror: MirrorRepository
    index: IndexRepository
    creds: CredentialsProvider
    paths: CalendarPaths
    now: datetime
    #: How to open a CalDAV session for an account; the default talks
    #: to the real server.
    session_factory: SessionFactory | None = None


def build_sync_runner(
    services: CalendarServices,
) -> Callable[..., Sequence[SyncResult]]:
    """Closure that runs `sync_account` over every configured account.

    Returns the per-account `SyncResult`s rather than reporting
    anything: every front end has its own way of saying what happened.
    Per-account exceptions are caught
    and reported in the result's `errors` tuple so a single bad
    credential doesn't take the whole sync down.

    The runner accepts an optional `cancel_event` keyword. The TUI
    sets this event from another thread to interrupt a long-running
    sync; `sync_account` checks it at calendar boundaries and raises
    `SyncCancelled`, which the runner translates into a per-account
    error rather than letting it tear through the worker.
    """
    factory = services.session_factory or default_session_factory

    def run(*, cancel_event: threading.Event | None = None) -> Sequence[SyncResult]:
        try:
            with acquire_sync_lock(services.paths.lock_file):
                return _run_locked(cancel_event)
        except SyncLockError as exc:
            # Surface the lock contention as a SyncResult so the TUI's
            # banner shows the message instead of crashing the worker.
            return (_failure_result("(sync)", str(exc)),)

    def _run_locked(
        cancel_event: threading.Event | None,
    ) -> Sequence[SyncResult]:
        results: list[SyncResult] = []
        for account in services.config.accounts:
            if cancel_event is not None and cancel_event.is_set():
                results.append(_failure_result(account.name, "sync cancelled"))
                continue
            try:
                auth = services.creds.build_auth(account)
            except CredentialResolutionError as exc:
                results.append(_failure_result(account.name, str(exc)))
                continue
            try:
                session = factory(account, auth)
            except NotImplementedError as exc:
                results.append(_failure_result(account.name, str(exc)))
                continue
            except CalDAVError as exc:
                results.append(_failure_result(account.name, f"CalDAV: {exc}"))
                continue
            try:
                result = sync_account(
                    account=account,
                    session=session,
                    mirror=services.mirror,
                    index=services.index,
                    now=services.now,
                    cancel_event=cancel_event,
                )
            except SyncCancelled:
                results.append(_failure_result(account.name, "sync cancelled"))
                continue
            except CalDAVError as exc:
                results.append(_failure_result(account.name, f"CalDAV: {exc}"))
                continue
            except OAuthError as exc:
                results.append(_failure_result(account.name, f"OAuth: {exc}"))
                continue
            if auth.on_commit is not None:
                auth.on_commit()
            results.append(result)
        return tuple(results)

    return run


def _failure_result(account_name: str, message: str) -> SyncResult:
    return SyncResult(
        account_name=account_name,
        calendars_synced=0,
        components_added=0,
        components_updated=0,
        components_removed=0,
        errors=(message,),
    )


def default_session_factory(
    account: AccountConfig, authorization: Authorization
) -> CalDAVSession:
    return CalDAVHttpSession(url=account.url, authorization=authorization)


# Live progress sink for the remote probe. Each call is a single short
# step description, emitted *before* a potentially-blocking network call
# so a stall is attributable to the last line printed.
ProgressFn = Callable[[str], None]
_REMOTE_MULTIGET_SAMPLE_LIMIT = 20


class DiagnosticStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, kw_only=True)
class DiagnosticResult:
    check: str
    scope: str
    status: DiagnosticStatus
    message: str


@dataclass(frozen=True, kw_only=True)
class DoctorReport:
    results: tuple[DiagnosticResult, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> tuple[DiagnosticResult, ...]:
        return tuple(r for r in self.results if r.status == DiagnosticStatus.FAIL)

    @property
    def warnings(self) -> tuple[DiagnosticResult, ...]:
        return tuple(r for r in self.results if r.status == DiagnosticStatus.WARN)

    @property
    def exit_code(self) -> int:
        if self.failed:
            return 1
        return 0


def run_doctor(
    *,
    config: AppConfig,
    mirror: MirrorRepository,
    index: IndexRepository,
    creds: CredentialsProvider,
    session_factory: SessionFactory | None = None,
    progress: ProgressFn | None = None,
) -> DoctorReport:
    emit = progress if progress is not None else _noop_progress
    results: list[DiagnosticResult] = []
    for account in config.accounts:
        results.append(_check_credentials(account, creds))
        results.extend(_check_mirror_integrity(account, mirror, index))
        if session_factory is not None:
            results.extend(
                _check_remote_caldav(account, creds, index, session_factory, emit)
            )
    return DoctorReport(results=tuple(results))


def check_local_integrity(
    *,
    config: AppConfig,
    mirror: MirrorRepository,
    index: IndexRepository,
) -> DoctorReport:
    """Compare every calendar's mirror against the index, and nothing else.

    The part of :func:`run_doctor` that needs no credentials and no
    network: it reads each ``.ics`` the mirror holds and each row the
    index holds, and reports a file with no row, a row with no file and
    an ``.ics`` that will not parse.

    It is public because ``pony doctor`` answers for the calendar half
    too, and used to answer "is it healthy?" by counting mirror
    directories with ``iterdir`` -- so the comparison that can actually
    find a missing file was reachable only through
    ``pony calendar doctor``.
    """
    results: list[DiagnosticResult] = []
    for account in config.accounts:
        results.extend(_check_mirror_integrity(account, mirror, index))
    return DoctorReport(results=tuple(results))


def _noop_progress(_message: str) -> None:
    return None


def _check_credentials(
    account: AccountConfig, creds: CredentialsProvider
) -> DiagnosticResult:
    try:
        creds.build_auth(account)
    except CredentialResolutionError as exc:
        return DiagnosticResult(
            check="credentials",
            scope=account.name,
            status=DiagnosticStatus.FAIL,
            message=str(exc),
        )
    return DiagnosticResult(
        check="credentials",
        scope=account.name,
        status=DiagnosticStatus.OK,
        message="credential resolved",
    )


def _check_mirror_integrity(
    account: AccountConfig,
    mirror: MirrorRepository,
    index: IndexRepository,
) -> list[DiagnosticResult]:
    results: list[DiagnosticResult] = []
    calendars = mirror.list_calendars(account.name)
    for calendar_name in calendars:
        scope = f"{account.name}/{calendar_name}"
        calendar_ref = CalendarRef(account.name, calendar_name)

        mirror_refs = mirror.list_resources(account.name, calendar_name)
        mirror_uids = {r.uid for r in mirror_refs}

        index_components = index.list_calendar_components(calendar_ref)
        index_uids = {c.ref.uid for c in index_components}

        orphans = mirror_uids - index_uids
        missing = {
            c.ref.uid
            for c in index_components
            if c.href is not None and c.ref.uid not in mirror_uids
        }
        unparseable: list[str] = []
        for ref in mirror_refs:
            try:
                parse_vcalendar(mirror.read(ref))
            except IcalParseError:
                unparseable.append(ref.uid)

        if not (orphans or missing or unparseable):
            results.append(
                DiagnosticResult(
                    check="mirror-integrity",
                    scope=scope,
                    status=DiagnosticStatus.OK,
                    message=(
                        f"{len(mirror_refs)} resources, "
                        f"{len(index_components)} index rows"
                    ),
                )
            )
            continue
        if unparseable:
            results.append(
                DiagnosticResult(
                    check="mirror-integrity",
                    scope=scope,
                    status=DiagnosticStatus.FAIL,
                    message=f"unparseable .ics: {sorted(unparseable)}",
                )
            )
        if missing:
            results.append(
                DiagnosticResult(
                    check="mirror-integrity",
                    scope=scope,
                    status=DiagnosticStatus.FAIL,
                    message=f"index rows point at missing mirror files: "
                    f"{sorted(missing)}",
                )
            )
        if orphans:
            results.append(
                DiagnosticResult(
                    check="mirror-integrity",
                    scope=scope,
                    status=DiagnosticStatus.WARN,
                    message=f"mirror files with no index row: {sorted(orphans)}",
                )
            )
    return results


def _check_remote_caldav(
    account: AccountConfig,
    creds: CredentialsProvider,
    index: IndexRepository,
    session_factory: SessionFactory,
    progress: ProgressFn = _noop_progress,
) -> list[DiagnosticResult]:
    """Probe remote CalDAV discovery without logging secrets or event bodies."""
    progress(f"{account.name}: resolving credentials")
    try:
        auth = creds.build_auth(account)
    except CredentialResolutionError as exc:
        return [
            DiagnosticResult(
                check="remote-caldav",
                scope=account.name,
                status=DiagnosticStatus.FAIL,
                message=f"credential unresolved: {_redact(str(exc))}",
            )
        ]

    try:
        progress(f"{account.name}: opening session")
        session = session_factory(account, auth)
        progress(f"{account.name}: discovering principal (PROPFIND)")
        principal = session.discover_principal()
        progress(f"{account.name}: listing calendars (PROPFIND Depth:1)")
        calendars = tuple(session.list_calendars(principal))
    except Exception as exc:
        return [
            DiagnosticResult(
                check="remote-caldav",
                scope=account.name,
                status=DiagnosticStatus.FAIL,
                message=(
                    f"discovery failed: {type(exc).__name__}: {_redact(str(exc))}"
                ),
            )
        ]

    scoped = tuple(
        calendar
        for calendar in calendars
        if any(pattern.fullmatch(calendar.name) for pattern in account.include)
        and not any(pattern.fullmatch(calendar.name) for pattern in account.exclude)
    )
    results: list[DiagnosticResult] = [
        DiagnosticResult(
            check="remote-caldav",
            scope=account.name,
            status=DiagnosticStatus.OK if scoped else DiagnosticStatus.WARN,
            message=(
                f"discovered {len(calendars)} calendar(s); "
                f"{len(scoped)} matched include/exclude filters"
            ),
        )
    ]

    progress(
        f"{account.name}: discovered {len(calendars)} calendar(s), {len(scoped)} scoped"
    )
    for calendar in scoped:
        scope = f"{account.name}/{calendar.name}"
        try:
            progress(f"{scope}: calendar-query (REPORT)")
            remote_resources = tuple(session.calendar_query(calendar.url))
        except Exception as exc:
            results.append(
                DiagnosticResult(
                    check="remote-calendar-query",
                    scope=scope,
                    status=DiagnosticStatus.FAIL,
                    message=(
                        f"calendar-query failed: {type(exc).__name__}: "
                        f"{_redact(str(exc))}"
                    ),
                )
            )
            continue

        local_rows = tuple(
            index.list_calendar_components(CalendarRef(account.name, calendar.name))
        )
        remote_count = len(remote_resources)
        results.append(
            DiagnosticResult(
                check="remote-calendar-query",
                scope=scope,
                status=DiagnosticStatus.OK if remote_count else DiagnosticStatus.WARN,
                message=(
                    f"calendar-query returned {remote_count} resource(s); "
                    f"local index has {_component_count_summary(local_rows)}"
                ),
            )
        )
        if remote_resources:
            sample_size = min(len(remote_resources), _REMOTE_MULTIGET_SAMPLE_LIMIT)
            progress(f"{scope}: multiget sample of {sample_size} resource(s) (REPORT)")
            results.append(
                _check_remote_multiget_sample(
                    session=session,
                    calendar_url=calendar.url,
                    scope=scope,
                    remote_resources=remote_resources,
                )
            )

    progress(f"{account.name}: remote probe complete")

    if auth.on_commit is not None:
        auth.on_commit()
    return results


@dataclass(frozen=True, kw_only=True)
class _SampleParseStats:
    events: int = 0
    todos: int = 0
    parse_errors: int = 0
    empty_or_unsupported: int = 0

    @property
    def parsed_components(self) -> int:
        return self.events + self.todos


def _check_remote_multiget_sample(
    *,
    session: CalDAVSession,
    calendar_url: str,
    scope: str,
    remote_resources: tuple[tuple[str, str], ...],
) -> DiagnosticResult:
    sample_hrefs = tuple(
        href for href, _etag in remote_resources[:_REMOTE_MULTIGET_SAMPLE_LIMIT]
    )
    try:
        fetched = tuple(session.calendar_multiget(calendar_url, sample_hrefs))
    except Exception as exc:
        return DiagnosticResult(
            check="remote-multiget-sample",
            scope=scope,
            status=DiagnosticStatus.FAIL,
            message=(
                f"calendar-multiget failed: {type(exc).__name__}: {_redact(str(exc))}"
            ),
        )

    stats = _sample_parse_stats(fetched)
    status = DiagnosticStatus.OK
    if (
        len(fetched) < len(sample_hrefs)
        or stats.parse_errors
        or stats.parsed_components == 0
    ):
        status = DiagnosticStatus.WARN

    return DiagnosticResult(
        check="remote-multiget-sample",
        scope=scope,
        status=status,
        message=(
            f"sampled {len(sample_hrefs)} of {len(remote_resources)} resource(s); "
            f"multiget returned {len(fetched)} body(ies); "
            f"parsed {stats.events} event component(s), "
            f"{stats.todos} task component(s); "
            f"{stats.parse_errors} parse error(s), "
            f"{stats.empty_or_unsupported} empty/unsupported object(s)"
        ),
    )


def _sample_parse_stats(
    fetched: tuple[tuple[str, str, bytes], ...],
) -> _SampleParseStats:
    events = 0
    todos = 0
    parse_errors = 0
    empty_or_unsupported = 0
    for _href, _etag, raw in fetched:
        try:
            parsed = parse_vcalendar(raw)
        except IcalParseError:
            parse_errors += 1
            continue
        if not parsed:
            empty_or_unsupported += 1
            continue
        for component in parsed:
            if component.kind == ComponentKind.VEVENT:
                events += 1
            elif component.kind == ComponentKind.VTODO:
                todos += 1
    return _SampleParseStats(
        events=events,
        todos=todos,
        parse_errors=parse_errors,
        empty_or_unsupported=empty_or_unsupported,
    )


def _component_count_summary(components: tuple[StoredComponent, ...]) -> str:
    events = sum(1 for component in components if isinstance(component, VEvent))
    todos = sum(1 for component in components if isinstance(component, VTodo))
    return f"{len(components)} row(s): {events} event(s), {todos} task(s)"


_URL_RE = re.compile(r"https?://[^\s)>\"]+")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|access_token|refresh_token|"
    r"client_secret|password|passwd|token)=([^\s;&]+)"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization):\s*(?:(?:bearer|basic)\s+)?[^\s,;]+"
)
_COOKIE_HEADER_RE = re.compile(r"(?i)\b(cookie|set-cookie):\s*[^\r\n]+")


def _redact(text: str) -> str:
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}: <redacted>", text)
    text = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}: <redacted>", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _URL_RE.sub(lambda m: _fingerprint("url", m.group(0)), text)
    return _EMAIL_RE.sub(lambda m: _fingerprint("email", m.group(0)), text)


def _fingerprint(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"<redacted-{kind}:{digest}>"


def format_report(report: DoctorReport) -> str:
    lines: list[str] = []
    for result in report.results:
        lines.append(
            f"[{result.status.value.upper():<4}] {result.check} "
            f"({result.scope}): {result.message}"
        )
    if not report.results:
        lines.append("No accounts configured; nothing to check.")
    return "\n".join(lines) + "\n"


__all__ = [
    "CalendarServices",
    "DiagnosticResult",
    "DiagnosticStatus",
    "DoctorReport",
    "ProgressFn",
    "SessionFactory",
    "build_sync_runner",
    "check_local_integrity",
    "default_session_factory",
    "format_report",
    "run_doctor",
]
