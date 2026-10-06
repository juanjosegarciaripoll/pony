"""The periodic sync thread, exercised without an application.

Every test here drives the thread through `threading.Event` handshakes
and tiny intervals rather than sleeping for a deadline: a test that waits
out a real interval is slow when it passes and flaky when it does not.
"""

from __future__ import annotations

import threading
import unittest

from chronos.scheduler import PeriodicSync, SyncOutcome

# Long enough that a periodic turn never fires on its own during a test
# that is about the manual path, short enough to wait out on purpose.
_NEVER = 3600.0
_WAIT = 5.0


class _Recorder:
    """A runner that counts its calls and announces each one."""

    def __init__(self) -> None:
        self.calls: list[threading.Event | None] = []
        self.called = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def __call__(self, *, cancel_event: threading.Event | None = None) -> str:
        self.calls.append(cancel_event)
        self.called.set()
        self.release.wait(_WAIT)
        return "done"

    def wait_for_call(self) -> None:
        assert self.called.wait(_WAIT), "the runner was never called"
        self.called.clear()


class PeriodicSyncTest(unittest.TestCase):
    def _scheduler(
        self,
        runner: object,
        *,
        interval: float = _NEVER,
        defer_retry: float = 0.01,
        on_finished: object = None,
    ) -> PeriodicSync[str]:
        scheduler: PeriodicSync[str] = PeriodicSync(
            name="test-sync",
            interval=interval,
            runner=runner,  # type: ignore[arg-type]
            on_finished=on_finished,  # type: ignore[arg-type]
            defer_retry=defer_retry,
        )
        self.addCleanup(scheduler.stop)
        return scheduler

    def test_a_positive_interval_is_required(self) -> None:
        with self.assertRaises(ValueError):
            PeriodicSync(name="x", interval=0, runner=lambda **_: None)

    def test_nothing_runs_before_start(self) -> None:
        runner = _Recorder()
        scheduler = self._scheduler(runner)
        self.assertFalse(scheduler.started)
        self.assertIsNone(scheduler.next_run_at)
        self.assertFalse(runner.called.wait(0.05))

    def test_start_schedules_one_interval_out_and_does_not_sync_now(self) -> None:
        runner = _Recorder()
        scheduler = self._scheduler(runner, interval=_NEVER)
        scheduler.start()
        deadline = scheduler.next_run_at
        assert deadline is not None
        self.assertGreater(deadline, 0)
        # No sync at startup: the first automatic run is a full interval away.
        self.assertFalse(runner.called.wait(0.05))
        self.assertEqual(runner.calls, [])

    def test_the_interval_fires_repeatedly(self) -> None:
        runner = _Recorder()
        scheduler = self._scheduler(runner, interval=0.01)
        scheduler.start()
        runner.wait_for_call()
        runner.wait_for_call()
        self.assertGreaterEqual(len(runner.calls), 2)

    def test_request_now_runs_immediately_and_resets_the_countdown(self) -> None:
        runner = _Recorder()
        scheduler = self._scheduler(runner, interval=_NEVER)
        scheduler.start()
        first_deadline = scheduler.next_run_at
        self.assertTrue(scheduler.request_now())
        runner.wait_for_call()
        self._settle(scheduler)
        second_deadline = scheduler.next_run_at
        assert first_deadline is not None and second_deadline is not None
        # The deadline moved: a full interval from the manual run, not
        # from when the scheduler happened to start.
        self.assertGreater(second_deadline, first_deadline)

    def test_the_runner_is_given_the_cancel_event(self) -> None:
        runner = _Recorder()
        scheduler = self._scheduler(runner)
        scheduler.start()
        scheduler.request_now()
        runner.wait_for_call()
        self.assertIsInstance(runner.calls[0], threading.Event)

    def test_stop_sets_the_cancel_event_a_run_is_watching(self) -> None:
        seen: list[bool] = []
        started = threading.Event()
        finished = threading.Event()

        def runner(*, cancel_event: threading.Event | None = None) -> str:
            started.set()
            assert cancel_event is not None
            seen.append(cancel_event.wait(_WAIT))
            finished.set()
            return "cancelled"

        scheduler = self._scheduler(runner)
        scheduler.start()
        scheduler.request_now()
        self.assertTrue(started.wait(_WAIT))
        scheduler.stop()
        self.assertTrue(finished.wait(_WAIT))
        self.assertEqual(seen, [True])
        self.assertIsNone(scheduler.next_run_at)
        self.assertFalse(scheduler.started)

    def test_request_now_is_refused_while_a_sync_runs(self) -> None:
        runner = _Recorder()
        runner.release.clear()
        scheduler = self._scheduler(runner)
        scheduler.start()
        self.assertTrue(scheduler.request_now())
        runner.wait_for_call()
        self.assertTrue(scheduler.running)
        self.assertFalse(scheduler.request_now())
        runner.release.set()

    def test_hold_blocks_a_concurrent_turn_and_defers_it(self) -> None:
        """A held slot postpones the thread's turn instead of losing it."""
        runner = _Recorder()
        scheduler = self._scheduler(runner, interval=0.01, defer_retry=0.01)
        with scheduler.hold() as taken:
            self.assertTrue(taken)
            scheduler.start()
            # The interval elapses several times over while the slot is
            # held, and not one of those turns runs the runner.
            self.assertFalse(runner.called.wait(0.2))
            self.assertEqual(runner.calls, [])
        # Released: the deferred turn arrives on its own.
        runner.wait_for_call()

    def test_hold_is_refused_while_a_sync_runs(self) -> None:
        runner = _Recorder()
        runner.release.clear()
        scheduler = self._scheduler(runner)
        scheduler.start()
        scheduler.request_now()
        runner.wait_for_call()
        with scheduler.hold() as taken:
            self.assertFalse(taken)
        runner.release.set()

    def test_releasing_a_hold_counts_as_a_sync_just_happened(self) -> None:
        scheduler = self._scheduler(_Recorder(), interval=_NEVER)
        scheduler.start()
        before = scheduler.next_run_at
        with scheduler.hold():
            pass
        after = scheduler.next_run_at
        assert before is not None and after is not None
        self.assertGreater(after, before)

    def test_a_failing_runner_is_reported_and_the_thread_survives(self) -> None:
        outcomes: list[SyncOutcome[str]] = []
        seen = threading.Event()
        calls: list[int] = []

        def runner(**_kwargs: object) -> str:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("the server is down")
            return "recovered"

        def on_finished(outcome: SyncOutcome[str]) -> None:
            outcomes.append(outcome)
            seen.set()

        scheduler = self._scheduler(runner, interval=0.01, on_finished=on_finished)
        scheduler.start()
        self.assertTrue(seen.wait(_WAIT))
        seen.clear()
        self.assertTrue(seen.wait(_WAIT))
        first, second = outcomes[0], outcomes[1]
        self.assertIsInstance(first.error, RuntimeError)
        self.assertIsNone(first.result)
        self.assertIsNone(second.error)
        self.assertEqual(second.result, "recovered")

    def test_a_failing_listener_does_not_end_the_thread(self) -> None:
        runner = _Recorder()
        calls: list[int] = []

        def explode(_outcome: SyncOutcome[str]) -> None:
            calls.append(1)
            raise RuntimeError("the app is gone")

        scheduler = self._scheduler(runner, interval=0.01, on_finished=explode)
        with self.assertLogs("chronos.scheduler", level="WARNING"):
            scheduler.start()
            runner.wait_for_call()
            runner.wait_for_call()
        self.assertGreaterEqual(len(calls), 2)

    def test_outcomes_say_whether_the_user_asked(self) -> None:
        outcomes: list[SyncOutcome[str]] = []
        seen = threading.Event()

        def on_finished(outcome: SyncOutcome[str]) -> None:
            outcomes.append(outcome)
            seen.set()

        scheduler = self._scheduler(
            _Recorder(), interval=_NEVER, on_finished=on_finished
        )
        scheduler.start()
        scheduler.request_now()
        self.assertTrue(seen.wait(_WAIT))
        self.assertTrue(outcomes[0].manual)

        seen.clear()
        scheduler.stop()
        periodic = self._scheduler(_Recorder(), interval=0.01, on_finished=on_finished)
        periodic.start()
        self.assertTrue(seen.wait(_WAIT))
        self.assertFalse(outcomes[-1].manual)

    def test_a_started_scheduler_is_not_started_twice(self) -> None:
        runner = _Recorder()
        scheduler = self._scheduler(runner)
        scheduler.start()
        first = scheduler.next_run_at
        scheduler.start()
        self.assertEqual(scheduler.next_run_at, first)

    def test_stopping_what_never_started_is_harmless(self) -> None:
        scheduler = self._scheduler(_Recorder())
        scheduler.stop()
        scheduler.stop()
        self.assertFalse(scheduler.started)

    def test_a_run_that_ignores_the_cancel_event_is_left_behind(self) -> None:
        """`stop` must not wait out a sync that cannot be interrupted."""
        runner = _Recorder()
        runner.release.clear()
        scheduler = self._scheduler(runner)
        scheduler.start()
        scheduler.request_now()
        runner.wait_for_call()
        with self.assertLogs("chronos.scheduler", level="WARNING") as logs:
            scheduler.stop(timeout=0.05)
        self.assertIn("did not stop", "".join(logs.output))
        runner.release.set()

    def _settle(self, scheduler: PeriodicSync[str]) -> None:
        """Wait until the slot is free again, so the deadline is final."""
        for _ in range(int(_WAIT / 0.01)):
            if not scheduler.running:
                return
            threading.Event().wait(0.01)
        raise AssertionError("the sync never finished")
