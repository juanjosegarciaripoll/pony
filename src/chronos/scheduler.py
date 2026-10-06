"""Periodic synchronisation on a thread of its own.

A Textual timer belongs to the widget that created it, and a screen that
is popped is unmounted — so a timer armed in a screen's ``on_mount`` stops
existing the moment the user looks at something else.  Both halves of the
program used to schedule their sync that way, which made the cadence
depend on what was on screen: the agenda's countdown restarted from zero
on every visit, and the mail application ran a second timer for the
calendar that skipped its turn whenever the agenda happened to be in
front.

:class:`PeriodicSync` owns the cadence instead.  One instance per
subsystem, created and started by the application, running on a daemon
thread that does not care which screen is mounted.  Screens read
:attr:`next_run_at` to paint their countdown and call
:meth:`request_now` when the user asks for a sync; results arrive through
the callbacks.

This module is deliberately free of Textual.  Both callbacks fire **on
the scheduler thread**, so a caller that touches widgets has to marshal
them onto the UI thread itself (``App.call_from_thread``) — which is also
what lets the mail half reuse this class, since ``pony`` may import
``chronos`` but not the other way round.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic

logger = logging.getLogger(__name__)

# How long to wait before trying again when the slot is taken by a sync
# the user started.  Short enough that the deferred turn still happens
# within the same sitting, long enough that a slow sync is not polled.
DEFAULT_DEFER_RETRY_SECONDS = 30.0

# How long `stop` waits for a run in flight to notice the cancel event.
DEFAULT_STOP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class SyncOutcome[T]:
    """What one run produced.

    Exactly one of `result` and `error` is set: a runner that raised has
    no result, and the exception is carried here rather than killing the
    thread.  `manual` distinguishes a run the user asked for — which
    always reports — from a periodic one, which by convention stays quiet
    unless something changed or failed.
    """

    result: T | None
    error: BaseException | None
    manual: bool


class PeriodicSync[T]:
    """Run `runner` every `interval` seconds on a thread of its own.

    The thread is a daemon: a mail sync has no cancellation point, so a
    hung IMAP session must never be able to hold up application exit.
    `runner` is called as ``runner(cancel_event=...)``, the convention the
    calendar's sync already follows, and a runner that cannot be
    interrupted simply ignores the keyword.

    The first automatic run happens one full interval after
    :meth:`start`, never at startup.
    """

    def __init__(
        self,
        *,
        name: str,
        interval: float,
        runner: Callable[..., T],
        on_started: Callable[[], None] | None = None,
        on_finished: Callable[[SyncOutcome[T]], None] | None = None,
        defer_retry: float = DEFAULT_DEFER_RETRY_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._name = name
        self._interval = interval
        self._runner = runner
        self._on_started = on_started
        self._on_finished = on_finished
        self._defer_retry = defer_retry
        self._clock = clock
        # Set by `stop`, and handed to the runner as its cancel event so a
        # run in flight abandons itself at the next boundary it checks.
        self._stop = threading.Event()
        # Set to cut a wait short: a manual request, or `stop`.
        self._wake = threading.Event()
        # Held for the duration of a run, by this thread or by a caller
        # that took `hold`.  Non-blocking everywhere: nothing ever queues.
        self._busy = threading.Lock()
        self._state = threading.Lock()
        self._next_run_at: float | None = None
        self._manual_requested = False
        self._thread: threading.Thread | None = None

    # -- state the UI reads ------------------------------------------------

    @property
    def next_run_at(self) -> float | None:
        """`clock()` value of the next scheduled run, or None when stopped.

        The same monotonic frame the countdown widgets render from, so a
        screen can paint the deadline without knowing how it was reached.
        """
        with self._state:
            return self._next_run_at

    @property
    def running(self) -> bool:
        """True while a sync occupies the slot, whoever started it."""
        return self._busy.locked()

    @property
    def started(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Begin the cadence.  Idempotent; a second call is ignored."""
        if self.started:
            return
        self._stop.clear()
        self._wake.clear()
        self._postpone(self._interval)
        thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> None:
        """Stop the cadence and wait for a run in flight to give up.

        Safe to call on a scheduler that was never started, and safe to
        call twice.  A run that ignores its cancel event is left to the
        daemon thread rather than waited out.
        """
        thread = self._thread
        self._stop.set()
        self._wake.set()
        with self._state:
            self._next_run_at = None
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                logger.warning(
                    "%s did not stop within %.1fs; leaving it to exit",
                    self._name,
                    timeout,
                )
        self._thread = None

    # -- asking for a run -------------------------------------------------

    def request_now(self) -> bool:
        """Sync as soon as possible.  False when one is already running.

        On a started scheduler this cuts the wait short and counts the
        next interval from this run, so a manual sync pushes the
        automatic one a full interval out.

        On a scheduler that was never started — the configuration says not
        to sync in the background — it runs **once**, on a thread of its
        own, and starts no cadence: the key the user pressed is not
        permission to sync hourly from now on.  A caller that does want
        the cadence calls :meth:`start` first.
        """
        if self._busy.locked():
            return False
        if not self.started:
            threading.Thread(
                target=self._run_detached, name=f"{self._name}-once", daemon=True
            ).start()
            return True
        with self._state:
            self._manual_requested = True
            self._next_run_at = self._clock()
        self._wake.set()
        return True

    def try_claim(self) -> bool:
        """Take the slot for a sync run by someone else.  False if taken.

        The foreground flows — a confirmation dialog, a progress screen —
        own their own worker, and two syncs against one account at the
        same time is exactly what must not happen.  While the slot is held
        the thread's own turn is **postponed, not dropped**: it tries again
        after `defer_retry`.  Every successful claim must be returned with
        :meth:`release`, which is why the spans that fit in one scope use
        :meth:`hold` instead.
        """
        return self._busy.acquire(blocking=False)

    def release(self) -> None:
        """Give back a claimed slot, counting it as a sync just finished.

        The next automatic run is therefore a full interval away, which is
        what the user means by having just synced by hand.
        """
        self._busy.release()
        self._postpone(self._interval)

    @contextmanager
    def hold(self) -> Generator[bool]:
        """:meth:`try_claim` and :meth:`release` around one block."""
        taken = self.try_claim()
        try:
            yield taken
        finally:
            if taken:
                self.release()

    # -- internals --------------------------------------------------------

    def _postpone(self, delay: float) -> None:
        """Set the next deadline, unless the scheduler is shutting down.

        A run in flight re-arms the deadline when it finishes, which can
        land after `stop` has already cleared it; a stopped scheduler must
        not advertise a run that will never come.
        """
        with self._state:
            self._next_run_at = None if self._stop.is_set() else self._clock() + delay

    def _loop(self) -> None:
        while True:
            with self._state:
                deadline = self._next_run_at
            delay = self._interval if deadline is None else deadline - self._clock()
            if delay > 0:
                self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._state:
                if self._next_run_at is not None and self._clock() < self._next_run_at:
                    # Woken, but the deadline moved further out while we
                    # waited.  Wait again rather than running early.
                    continue
            if not self._busy.acquire(blocking=False):
                # A sync the user started has the slot. Keep this turn by
                # trying again shortly instead of losing the cycle.
                self._postpone(self._defer_retry)
                continue
            try:
                with self._state:
                    manual = self._manual_requested
                    self._manual_requested = False
                self._run_once(manual=manual)
            finally:
                self._busy.release()
                self._postpone(self._interval)

    def _run_detached(self) -> None:
        """One run with no cadence behind it, for an unstarted scheduler.

        Returns without doing anything once `stop` has been called: the
        cancel event the runner would be handed is already set, so there
        is nothing to be gained by starting a sync the application is
        shutting down on.
        """
        if self._stop.is_set() or not self._busy.acquire(blocking=False):
            return
        try:
            self._run_once(manual=True)
        finally:
            self._busy.release()

    def _run_once(self, *, manual: bool) -> None:
        """One run, start to finish, swallowing everything it can.

        Neither a failing runner nor a failing listener may end the
        thread: a listener that marshals onto a UI thread raises once the
        application is gone, which is ordinary at shutdown and never a
        reason to stop syncing.
        """
        if self._on_started is not None:
            try:
                self._on_started()
            except Exception as exc:  # noqa: BLE001 — a listener is not worth the thread
                logger.warning("%s start listener failed: %s", self._name, exc)
        result: T | None = None
        error: BaseException | None = None
        try:
            result = self._runner(cancel_event=self._stop)
        except BaseException as exc:  # noqa: BLE001 — one bad run must not end the thread
            error = exc
            logger.warning("%s failed: %s", self._name, exc)
        if self._on_finished is not None:
            try:
                self._on_finished(
                    SyncOutcome(result=result, error=error, manual=manual)
                )
            except Exception as exc:  # noqa: BLE001 — a listener is not worth the thread
                logger.warning("%s listener failed: %s", self._name, exc)


__all__ = [
    "DEFAULT_DEFER_RETRY_SECONDS",
    "DEFAULT_STOP_TIMEOUT_SECONDS",
    "PeriodicSync",
    "SyncOutcome",
]
