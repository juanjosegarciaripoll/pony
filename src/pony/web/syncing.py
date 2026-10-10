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
(:mod:`chronos.locking`), the mail half has never had one, and a
terminal left open is not a reason for the phone to refuse.
"""

from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from chronos.outcome import Outcome, failure, info, warn

from ..sync import describe_sync_result

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chronos.domain import SyncResult as CalendarSyncResult

    from ..calendar import CalendarRuntime
    from ..sync import ImapSyncService, MailSyncScheduler

_log = logging.getLogger(__name__)


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

    def mail(self) -> Outcome:
        """Fetch and push mail, or say why nothing was fetched.

        Blocks for as long as the sync does, so the caller runs it off
        the event loop.
        """
        if self.service is None:
            return Outcome.refused(
                warn("No credentials provider, so there is nothing to sync with."),
            )
        if not self._claim():
            return Outcome.refused(info("Sync already running."))
        try:
            result = self.service.sync()
        except Exception as exc:  # noqa: BLE001 — any backend may fail
            _log.error("web sync failed: %s", exc)
            return Outcome.refused(failure(f"Sync failed: {exc}"))
        finally:
            self._release()
        return Outcome(
            changed=_changed(result),
            notices=(
                info(
                    describe_sync_result(result),
                ),
            ),
        )

    def calendars(self) -> Outcome:
        """Sync the calendar, which takes its own cross-process lock."""
        runtime = self.calendar
        if runtime is None:
            return Outcome.refused(warn("No calendar configured."))
        from chronos.cli import CliContext, build_sync_runner

        runner = build_sync_runner(
            CliContext(
                config=runtime.config,
                mirror=runtime.mirror,
                index=runtime.index,
                creds=runtime.credentials,
                # The runner returns its results rather than printing
                # them, but a context needs both streams.
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                now=datetime.now(UTC),
            ),
        )
        try:
            results = runner()
        except Exception as exc:  # noqa: BLE001 — any backend may fail
            _log.error("web calendar sync failed: %s", exc)
            return Outcome.refused(failure(f"Calendar sync failed: {exc}"))
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
    """What a calendar sync did, in one line, plus anything that failed."""
    added = sum(result.components_added for result in results)
    updated = sum(result.components_updated for result in results)
    removed = sum(result.components_removed for result in results)
    errors = [error for result in results for error in result.errors]
    notices = [
        info(
            f"Calendar sync complete.  +{added} added, "
            f"{updated} updated, {removed} removed",
        ),
    ]
    notices.extend(failure(error) for error in errors)
    return Outcome(changed=added + updated + removed, notices=tuple(notices))


__all__ = ["SyncGate"]
