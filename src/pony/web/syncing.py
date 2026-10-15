"""Syncing from the browser.

The browser syncs.  Pressing ``g`` here runs the same engine ``pony
sync`` runs and reports the same sentence the terminal reports: fetching
mail from a phone is much of the reason to have a second front end at
all.

What this module adds is the one guarantee the server can make on its
own: a single sync at a time *inside this process*.  Two browser tabs,
or a tab and the periodic thread, go through the same gate, and the
second one is told the first is still running rather than opening a
second IMAP session on the same account.  Across processes it is the
user's call — the calendar takes a lock of its own
(:mod:`pony.calendar.locking`), the mail half has never had one, and a
terminal left open is not a reason for the phone to refuse.

The gate also keeps *what the sync in flight is doing*, because the
engine reports it and nothing here was listening.  The terminal hands
:meth:`ImapSyncService.plan` and ``.execute`` a progress callback and
draws every :class:`~pony.mail.sync.ProgressInfo` into its dialog; a
browser cannot be handed a callback, so the gate holds the last one and
the page polls ``/sync/status`` for it.  Without that the only sign a
browser had was a notice that expired in eight seconds while the sync
ran for minutes.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pony.calendar.sync import describe_sync_result as describe_calendar_sync
from pony.outcome import Outcome, failure, info, warn

from ..mail.sync import describe_halt, describe_sync_result
from .actions import Confirmation, Reply

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pony.calendar.domain import SyncResult as CalendarSyncResult
    from pony.outcome import Notice

    from ..calendar_runtime import CalendarRuntime
    from ..mail.sync import (
        ImapSyncService,
        MailSyncScheduler,
        ProgressInfo,
        SyncPlan,
    )

_log = logging.getLogger(__name__)

# How long a halted plan is kept while the question about it is out.
# Long enough for somebody to read what it would delete and decide;
# short enough that it is not applied against a server that has moved
# on since.  Past it the answer plans again and says so.
HELD_PLAN_SECONDS = 300.0


#: What a page is told about a sync it did not start -- the periodic
#: thread holds the same slot, and there is no callback to read from it.
BACKGROUND_MESSAGE = "Background sync running…"

#: And while the calendar syncs, which reports no progress of its own.
CALENDAR_MESSAGE = "Syncing calendars…"

#: The first thing a mail sync says, before the engine says anything.
PLANNING_MESSAGE = "Planning the sync…"


@dataclass(frozen=True, slots=True)
class SyncProgress:
    """What a sync in flight has to say about itself.

    The fields are :class:`~pony.mail.sync.ProgressInfo`'s, which is
    what the terminal's dialog draws: *message* is the engine's own
    sentence (it already carries the counts), and *current* / *total*
    are there for a bar.  ``total`` is 0 wherever the work has no known
    length -- connecting, scanning -- and then there is nothing to draw
    but the sentence.
    """

    running: bool = False
    message: str = ""
    current: int = 0
    total: int = 0

    def as_json(self) -> dict[str, object]:
        """The shape ``/sync/status`` serves and ``pony.js`` reads."""
        return {
            "running": self.running,
            "message": self.message,
            "current": self.current,
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class _HeldPlan:
    """A plan waiting on an answer, and the folders it halted on."""

    plan: SyncPlan
    halted: frozenset[tuple[str, str]]
    at: float


@dataclass(slots=True)
class SyncGate:
    """The server's one sync slot, for the mail half and the calendar.

    *scheduler* is the periodic mail sync when one is running.  Holding
    its slot for the duration of a manual sync is what keeps the two
    from overlapping — the same claim the terminal's reader takes while
    its confirmation dialog is open.
    """

    service: ImapSyncService | None = None
    calendar: CalendarRuntime | None = None
    scheduler: MailSyncScheduler | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # The last thing the sync in flight said about itself.  Written from
    # the thread the sync runs on, read from the event loop by
    # ``/sync/status``: one immutable value replaced by a single
    # attribute store, so there is nothing to tear and no lock to take.
    # The two halves share the record -- a mail sync and a calendar sync
    # at once means the second to report wins, which costs a page a
    # sentence and nothing else.
    _progress: SyncProgress = field(default=SyncProgress(), repr=False)
    # The plan a halt is waiting on an answer about.  Kept because the
    # answer has to apply to the plan the question described.
    _held: _HeldPlan | None = field(default=None, repr=False)

    def mail(self, *, confirmed: bool = False) -> Reply:
        """Fetch and push mail, or say why nothing was fetched.

        Planned and executed in two steps, because the plan may trip the
        mass-deletion halt: then nothing is applied and the page is
        handed the question to ask.

        The plan is *held* across that question, and answering applies
        the plan the question described.  Re-planning on the answer
        meant the user agreed to one described mass deletion and a
        freshly computed one was applied -- and it cost a second full
        IMAP pass to compute.  The slot is released while the question
        is out, because a server cannot hold it for however long a
        person takes to read; the held plan expires after
        :data:`HELD_PLAN_SECONDS` rather than being applied cold.

        Blocks for as long as the sync does, so the caller runs it off
        the event loop.
        """
        if self.service is None:
            return Reply(
                Outcome.refused(
                    warn("No credentials provider, so there is nothing to sync with."),
                ),
            )
        if not self._claim():
            return Reply(Outcome.refused(info("Sync already running.")))
        stale: Notice | None = None
        self._progress = SyncProgress(running=True, message=PLANNING_MESSAGE)
        try:
            held = self._take_held() if confirmed else None
            if confirmed and held is None:
                stale = warn(
                    "The plan you were shown has expired, so this is a fresh one.",
                )
            if held is None:
                plan = self.service.plan(progress=self._note)
                halted = plan.folders_needing_confirmation()
            else:
                plan, halted = held.plan, held.halted
            if halted and not confirmed:
                reason = describe_halt(plan)
                self._held = _HeldPlan(plan=plan, halted=halted, at=time.monotonic())
                return Reply(
                    Outcome(changed=0, notices=(warn(reason),)),
                    confirm=Confirmation(
                        question=f"{reason}\n\nApply the sync anyway?",
                        fields={"confirm": "1"},
                    ),
                )
            result = self.service.execute(
                plan,
                confirmed_folders=halted,
                progress=self._note,
            )
        except Exception as exc:  # noqa: BLE001 — any backend may fail
            _log.error("web sync failed: %s", exc)
            return Reply(Outcome.refused(failure(f"Sync failed: {exc}")))
        finally:
            self._progress = SyncProgress()
            self._release()
        notices = [info(describe_sync_result(result))]
        if stale is not None:
            notices.insert(0, stale)
        return Reply(Outcome(changed=_changed(result), notices=tuple(notices)))

    def _note(self, info: ProgressInfo) -> None:
        """Keep what the engine just said, for ``/sync/status`` to serve."""
        self._progress = SyncProgress(
            running=True,
            message=info.message,
            current=info.current,
            total=info.total,
        )

    def progress(self) -> SyncProgress:
        """What to tell a page that asks whether a sync is running.

        Whoever started it: the periodic thread holds the same slot, so
        a page whose ``g`` was refused sees the run it was refused for
        rather than nothing at all.  That run reports no progress here —
        it was started with no callback — so all there is to say is that
        it is happening.
        """
        current = self._progress
        if current.running:
            return current
        scheduler = self.scheduler
        if scheduler is not None and scheduler.running:
            return SyncProgress(running=True, message=BACKGROUND_MESSAGE)
        return SyncProgress()

    def _take_held(self) -> _HeldPlan | None:
        """The plan the question described, if it is still worth applying."""
        held = self._held
        self._held = None
        if held is None or time.monotonic() - held.at > HELD_PLAN_SECONDS:
            return None
        return held

    def calendars(self) -> Outcome:
        """Sync the calendar, which takes its own cross-process lock."""
        runtime = self.calendar
        if runtime is None:
            return Outcome.refused(warn("No calendar configured."))
        from pony.calendar.services import CalendarServices, build_sync_runner

        runner = build_sync_runner(
            CalendarServices(
                config=runtime.config,
                mirror=runtime.mirror,
                index=runtime.index,
                creds=runtime.credentials,
                paths=runtime.paths,
                now=datetime.now(UTC),
            ),
        )
        self._progress = SyncProgress(running=True, message=CALENDAR_MESSAGE)
        try:
            results = runner()
        except Exception as exc:  # noqa: BLE001 — any backend may fail
            _log.error("web calendar sync failed: %s", exc)
            return Outcome.refused(failure(f"Calendar sync failed: {exc}"))
        finally:
            self._progress = SyncProgress()
        return _calendar_outcome(results)

    def _claim(self) -> bool:
        scheduler = self.scheduler
        if scheduler is not None and not scheduler.try_claim():
            return False
        if not self._lock.acquire(blocking=False):
            if scheduler is not None:
                scheduler.release()
            return False
        return True

    def _release(self) -> None:
        self._lock.release()
        if self.scheduler is not None:
            self.scheduler.release()


def _changed(result: object) -> int:
    """How much a mail sync moved, for the page's refresh decision."""
    accounts = getattr(result, "accounts", ())
    return sum(
        folder.fetched
        + folder.appended_to_server
        + folder.moved_to_server
        + folder.expunged_on_server
        + folder.flag_conflicts_merged
        for account in accounts
        for folder in account.folders
    )


def _calendar_outcome(results: Sequence[CalendarSyncResult]) -> Outcome:
    """What a calendar sync did, in one line, plus anything it has to say.

    ``notes`` are things that went neither wrong nor unremarked — a
    collision between a local edit and a server change, resolved one way
    or the other.  This dropped them, so a conflict the terminal shows
    for thirty seconds as a warning was silently swallowed here.
    """
    added = sum(result.components_added for result in results)
    updated = sum(result.components_updated for result in results)
    removed = sum(result.components_removed for result in results)
    errors = [error for result in results for error in result.errors]
    notes = [note for result in results for note in result.notes]
    notices = [info(describe_calendar_sync(results))]
    notices.extend(warn(note) for note in notes)
    notices.extend(failure(error) for error in errors)
    return Outcome(changed=added + updated + removed, notices=tuple(notices))


__all__ = [
    "BACKGROUND_MESSAGE",
    "CALENDAR_MESSAGE",
    "HELD_PLAN_SECONDS",
    "PLANNING_MESSAGE",
    "SyncGate",
    "SyncProgress",
]
