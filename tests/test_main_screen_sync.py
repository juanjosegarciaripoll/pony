"""Focused sync orchestration tests for :mod:`pony.tui.screens.main_screen`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from textual.worker import WorkerState
from tui_helpers import build_pony_app

from pony.sync import (
    AccountSyncPlan,
    AccountSyncResult,
    FetchNewOp,
    FolderSyncPlan,
    FolderSyncResult,
    ProgressInfo,
    SyncPlan,
    SyncResult,
    describe_sync_result,
)
from pony.tui.screens.main_screen import MainScreen
from pony.tui.screens.sync_confirm_screen import SyncConfirmScreen


def _main(app: object) -> MainScreen:
    return next(
        screen
        for screen in app.screen_stack  # type: ignore[attr-defined]
        if isinstance(screen, MainScreen)
    )


def _worker(
    *,
    name: str,
    state: WorkerState,
    result: object = None,
    error: Exception | None = None,
) -> Any:
    """Return a stand-in for a Textual ``Worker`` with just the fields read.

    The state is Textual's own enum, not a look-alike.  Production reads
    the members off the instance (``worker.state == worker.state.ERROR``),
    so a hand-rolled enum satisfied every comparison by construction and
    the tests could not tell a right member name from a wrong one.  The
    error and exec paths are also driven through a real ``Worker`` in
    ``test_a_failing_plan_reaches_the_screen_through_a_real_worker`` and
    ``test_a_confirmed_plan_executes_through_a_real_worker`` below.
    """
    return SimpleNamespace(name=name, state=state, result=result, error=error)


def _notifications(app: object) -> list[str]:
    messages: list[str] = []
    original = app.notify  # type: ignore[attr-defined]

    def capture(message: str, **kwargs: object) -> None:
        messages.append(message)
        original(message, **kwargs)

    app.notify = capture  # type: ignore[attr-defined]
    return messages


async def test_the_foreground_sync_requires_credentials() -> None:
    """`g` builds the engine itself, so it is the one that has to check.

    ``ctrl+g`` no longer does: the periodic sync belongs to the
    application, which is constructed with a credentials provider and so
    cannot be without one.
    """
    app, *_ = build_pony_app(label="main-sync-no-credentials")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._credentials = None
        screen.action_sync()
        await pilot.pause()

    assert messages == ["No credentials provider."]


def test_the_sync_summary_names_only_the_accounts_that_changed() -> None:
    """One sentence, built in the model so both front ends say it."""
    result = SyncResult(
        accounts=(
            AccountSyncResult(
                account_name="fictional",
                folders=(
                    FolderSyncResult(
                        folder_name="INBOX",
                        fetched=2,
                        flag_conflicts_merged=3,
                        appended_to_server=1,
                        moved_to_server=2,
                        expunged_on_server=1,
                    ),
                ),
            ),
            AccountSyncResult(
                account_name="unchanged",
                folders=(FolderSyncResult(folder_name="Archive"),),
            ),
        )
    )

    assert describe_sync_result(None) == "Sync complete."
    assert describe_sync_result(result) == (
        "Sync complete.  fictional: +2 msgs, 3 merged, 4 pushed"
    )


async def test_plan_error_dismisses_modal_and_notifies() -> None:
    app, *_ = build_pony_app(label="main-sync-plan-error")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        modal = SyncConfirmScreen.planning()
        # `action_sync` is what normally hands the screen its dialog;
        # these tests push one themselves, so they hand it over too.
        screen._sync_screen = modal
        app.push_screen(modal)
        await pilot.pause()
        screen._on_plan_complete(
            _worker(
                name="sync-plan",
                state=WorkerState.ERROR,
                error=RuntimeError("fictional planning failure"),
            )
        )
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

    assert messages == ["Sync planning failed: fictional planning failure"]


async def test_nonempty_plan_is_shown_for_confirmation() -> None:
    app, *_ = build_pony_app(label="main-sync-plan-ready")
    plan = SyncPlan(
        accounts=(AccountSyncPlan(account_name="acct", folders=(), creates=("New",)),)
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        modal = SyncConfirmScreen.planning()
        modal.show_plan = Mock()
        screen._sync_screen = modal
        app.push_screen(modal)
        await pilot.pause()
        screen._on_plan_complete(
            _worker(name="sync-plan", state=WorkerState.SUCCESS, result=plan)
        )
        assert screen._sync_plan is plan
        modal.show_plan.assert_called_once_with(plan)


async def test_start_sync_worker_executes_confirmed_plan() -> None:
    app, *_ = build_pony_app(label="main-sync-start-exec")
    plan = SyncPlan(
        accounts=(
            AccountSyncPlan(
                account_name="acct",
                folders=(
                    FolderSyncPlan(
                        folder_name="INBOX",
                        uid_validity=1,
                        highest_uid=1,
                        ops=(),
                        needs_confirmation=True,
                    ),
                ),
            ),
        )
    )
    service = Mock()
    service.execute.return_value = SyncResult(accounts=())

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._sync_service = service
        screen._sync_plan = plan
        screen.run_worker = Mock()
        screen._start_sync_worker()

        run = screen.run_worker.call_args.args[0]
        assert run() == SyncResult(accounts=())
        service.execute.assert_called_once()
        assert service.execute.call_args.kwargs["confirmed_folders"] == frozenset(
            {"INBOX"}
        )


async def test_exec_callbacks_and_progress_route_through_modal() -> None:
    app, *_ = build_pony_app(label="main-sync-exec-callbacks")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        modal = SyncConfirmScreen.planning()
        modal.update_progress = Mock()
        modal.dismiss = Mock()
        screen._sync_screen = modal
        app.push_screen(modal)
        await pilot.pause()

        info = ProgressInfo("Scanning", current=1, total=2)
        screen._sync_progress(info)
        modal.update_progress.assert_called_once_with(info)

        screen._on_exec_complete(
            _worker(
                name="sync-exec",
                state=WorkerState.ERROR,
                error=RuntimeError("fictional execution failure"),
            )
        )
        modal.dismiss.assert_called_once_with(None)
        assert messages[-1] == "Sync failed: fictional execution failure"

        modal.dismiss.reset_mock()
        screen._sync_screen = modal
        screen._on_exec_complete(
            _worker(
                name="sync-exec",
                state=WorkerState.SUCCESS,
                result=SyncResult(accounts=()),
            )
        )
        modal.dismiss.assert_called_once_with(True)
        assert messages[-1] == "Sync complete."


async def test_worker_completion_routes_by_worker_name() -> None:
    """``on_worker_state_changed`` dispatches on the worker's name."""
    app, *_ = build_pony_app(label="main-sync-dispatch")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._on_plan_complete = Mock()  # type: ignore[method-assign]
        screen._on_exec_complete = Mock()  # type: ignore[method-assign]

        # "sync-bg" is gone — the background sync is a thread the
        # application owns, not a worker this screen runs — so it must now
        # fall through like any other name.
        for name in ("sync-plan", "sync-exec", "sync-bg", "something-else"):
            event = SimpleNamespace(
                worker=_worker(name=name, state=WorkerState.SUCCESS),
            )
            screen.on_worker_state_changed(event)  # type: ignore[arg-type]
        await pilot.pause()

        screen._on_plan_complete.assert_called_once()
        screen._on_exec_complete.assert_called_once()

        # A worker that has not finished is not a completion.  Only
        # Textual's own enum has these members, which is why the states
        # the gate exists to reject were never passed to it before.
        for state in (
            WorkerState.PENDING,
            WorkerState.RUNNING,
            WorkerState.CANCELLED,
        ):
            event = SimpleNamespace(
                worker=_worker(name="sync-plan", state=state),
            )
            screen.on_worker_state_changed(event)  # type: ignore[arg-type]
        await pilot.pause()

        screen._on_plan_complete.assert_called_once()


async def test_sync_callbacks_are_safe_with_no_modal_on_screen() -> None:
    """A background sync has no confirm modal, so every guard must hold."""
    app, *_ = build_pony_app(label="main-sync-no-modal")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        assert not isinstance(app.screen, SyncConfirmScreen)

        # Progress with nothing to show it in.
        screen._sync_progress(ProgressInfo("Scanning", current=1, total=2))

        # Planning failed.
        screen._on_plan_complete(
            _worker(
                name="sync-plan",
                state=WorkerState.ERROR,
                error=RuntimeError("fictional planning failure"),
            )
        )

        # Planning produced nothing to do.
        screen._on_plan_complete(
            _worker(
                name="sync-plan",
                state=WorkerState.SUCCESS,
                result=SyncPlan(accounts=()),
            )
        )

        # Execution finished.
        screen._on_exec_complete(
            _worker(
                name="sync-exec",
                state=WorkerState.SUCCESS,
                result=SyncResult(accounts=()),
            )
        )
        await pilot.pause()

    assert any("fictional planning failure" in m for m in messages)
    assert any("Nothing to sync" in m or "up to date" in m.lower() for m in messages)


async def test_a_nonempty_plan_without_a_modal_is_still_stored() -> None:
    """The plan is kept so a later confirm can execute it."""
    app, *_ = build_pony_app(label="main-sync-plan-stored")

    plan = SyncPlan(
        accounts=(
            AccountSyncPlan(
                account_name="acct",
                folders=(
                    FolderSyncPlan(
                        folder_name="INBOX",
                        uid_validity=1,
                        highest_uid=1,
                        ops=(
                            FetchNewOp(
                                uid=1,
                                message_id="<planned@example.com>",
                                server_flags=frozenset(),
                            ),
                        ),
                    ),
                ),
            ),
        )
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._on_plan_complete(
            _worker(name="sync-plan", state=WorkerState.SUCCESS, result=plan)
        )
        await pilot.pause()

        assert screen._sync_plan is plan


async def test_cancelling_the_confirmation_notifies_and_refreshes() -> None:
    """Dismissing the modal with False means the user said no."""
    app, *_ = build_pony_app(label="main-sync-cancelled")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        captured: list[object] = []

        def _capture(_screen: object, callback: object = None, **_kw: object) -> None:
            captured.append(callback)

        app.push_screen = Mock(side_effect=_capture)
        screen.run_worker = Mock()  # type: ignore[method-assign]
        screen.action_sync()
        await pilot.pause()

        on_dismiss = captured[0]
        assert callable(on_dismiss)
        on_dismiss(False)
        await pilot.pause()

    assert any("Sync cancelled." in m for m in messages)


async def test_starting_the_exec_worker_without_a_plan_is_a_noop() -> None:
    """Nothing to execute when planning never produced a plan."""
    app, *_ = build_pony_app(label="main-sync-no-plan")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._sync_service = None
        screen._sync_plan = None
        screen.run_worker = Mock()  # type: ignore[method-assign]

        screen._start_sync_worker()
        await pilot.pause()

        screen.run_worker.assert_not_called()


async def test_the_plan_still_reaches_the_dialog_under_the_help_screen() -> None:
    """`f1` over the modal used to strand it with no way out.

    `DialogScreen` does not refuse app-level keys, so `f1` pushes help
    on top of the sync dialog.  The completion handlers asked whether
    `self.app.screen` *was* the dialog — false with help in front — so
    the plan never arrived, nothing dismissed anything, and the
    scheduler's slot stayed claimed for the rest of the session.
    """
    from pony.tui.screens.help_screen import HelpScreen

    app, *_ = build_pony_app(label="main-sync-plan-under-help")
    plan = SyncPlan(
        accounts=(AccountSyncPlan(account_name="acct", folders=(), creates=("New",)),)
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        modal = SyncConfirmScreen.planning()
        modal.show_plan = Mock()
        screen._sync_screen = modal
        app.push_screen(modal)
        await pilot.pause()
        app.push_screen(HelpScreen())
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)

        screen._on_plan_complete(
            _worker(name="sync-plan", state=WorkerState.SUCCESS, result=plan)
        )
        await pilot.pause()

        modal.show_plan.assert_called_once_with(plan)
        # And progress still lands on the dialog, not on whatever is on top.
        info = ProgressInfo("Scanning", current=1, total=2)
        modal.update_progress = Mock()
        screen._sync_progress(info)
        modal.update_progress.assert_called_once_with(info)


async def test_a_sync_that_fails_under_the_help_screen_still_closes() -> None:
    """Popping what is above it first is what makes the dialog closable.

    ``Screen.dismiss`` pops the *top* of the stack, so dismissing the
    dialog with help in front would close the help screen and fire the
    dialog's callback — leaving the modal behind and the slot claimed.
    """
    from pony.tui.screens.help_screen import HelpScreen

    app, *_ = build_pony_app(label="main-sync-fail-under-help")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen.action_sync = Mock()  # type: ignore[method-assign]
        modal = SyncConfirmScreen.planning()
        screen._sync_screen = modal
        app.push_screen(modal)
        await pilot.pause()
        app.push_screen(HelpScreen())
        await pilot.pause()

        screen._on_plan_complete(
            _worker(
                name="sync-plan",
                state=WorkerState.ERROR,
                error=RuntimeError("fictional planning failure"),
            )
        )
        await pilot.pause()

        # Both screens are gone and the reader is back.
        assert isinstance(app.screen, MainScreen)
        assert modal not in app.screen_stack
        assert screen._sync_screen is None

    assert messages == ["Sync planning failed: fictional planning failure"]


async def test_escape_backs_out_of_the_dialog_while_it_is_still_planning() -> None:
    """`on_key` returned early on `_planning`, swallowing every way out."""
    app, *_ = build_pony_app(label="main-sync-escape-planning")

    async with app.run_test() as pilot:
        await pilot.pause()
        modal = SyncConfirmScreen.planning()
        app.push_screen(modal)
        await pilot.pause()
        assert app.screen is modal

        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, MainScreen)


async def test_a_dialog_that_is_already_syncing_refuses_to_be_abandoned() -> None:
    """A half-applied sync is not something this dialog can walk away from."""
    app, *_ = build_pony_app(label="main-sync-escape-syncing")

    async with app.run_test() as pilot:
        await pilot.pause()
        modal = SyncConfirmScreen.planning()
        app.push_screen(modal)
        await pilot.pause()
        modal._planning = False
        modal._syncing = True

        await pilot.press("escape")
        await pilot.press("q")
        await pilot.pause()

        assert app.screen is modal


async def test_a_failed_background_sync_still_repaints_the_list() -> None:
    """A rolled-back run leaves the list claiming state the server lost."""
    from chronos.scheduler import SyncOutcome

    app, *_ = build_pony_app(label="main-sync-failed-repaint")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._refresh_after_sync = Mock()  # type: ignore[method-assign]

        screen.on_sync_finished(
            SyncOutcome(
                result=None,
                error=RuntimeError("fictional failure"),
                manual=True,
            ),
        )
        await pilot.pause()

        screen._refresh_after_sync.assert_called_once()


async def test_a_failing_plan_reaches_the_screen_through_a_real_worker(
    monkeypatch: Any,
) -> None:
    """The error arm, driven by Textual's own Worker rather than a stand-in.

    Every other test on this path hands ``_on_plan_complete`` a
    ``SimpleNamespace``, so nothing checked that a real failed worker
    carries its state and its exception where production looks for them.
    It did not get that far until `run_worker` was given
    ``exit_on_error=False``: Textual's default sent the failure to the
    application's exception handler, so the arm this exercises could
    never run.
    """
    from pony.sync import ImapSyncService

    def _boom(_self: ImapSyncService, **_kw: object) -> SyncPlan:
        raise RuntimeError("fictional planning failure")

    monkeypatch.setattr(ImapSyncService, "plan", _boom)
    app, *_ = build_pony_app(label="main-sync-real-plan-error")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        for _ in range(8):
            await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert _main(app)._sync_screen is None

    assert "Sync planning failed: fictional planning failure" in messages


def _one_folder_plan() -> SyncPlan:
    return SyncPlan(
        accounts=(
            AccountSyncPlan(
                account_name="acct",
                folders=(
                    FolderSyncPlan(
                        folder_name="INBOX",
                        uid_validity=1,
                        highest_uid=1,
                        ops=(
                            FetchNewOp(
                                uid=1,
                                message_id="<real-exec@example.com>",
                                server_flags=frozenset(),
                            ),
                        ),
                    ),
                ),
            ),
        )
    )


def _one_message_result() -> SyncResult:
    return SyncResult(
        accounts=(
            AccountSyncResult(
                account_name="acct",
                folders=(FolderSyncResult(folder_name="INBOX", fetched=1),),
            ),
        )
    )


async def test_the_exec_worker_reports_its_result_without_being_faked() -> None:
    """A real Textual worker runs the plan and a real handler reports it.

    ``_start_sync_worker`` is otherwise only reached with
    ``screen.run_worker`` replaced by a ``Mock``, and ``_on_exec_complete``
    only with a ``SimpleNamespace`` in place of the worker, so the two
    halves were never joined up.
    """
    plan = _one_folder_plan()
    result = _one_message_result()
    service = Mock()
    service.execute.return_value = result

    app, *_ = build_pony_app(label="main-sync-real-exec-worker")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._sync_service = service
        screen._sync_plan = plan
        screen._start_sync_worker()
        for _ in range(12):
            if describe_sync_result(result) in messages:
                break
            await pilot.pause()

    service.execute.assert_called_once()
    assert service.execute.call_args.args[0] is plan
    assert describe_sync_result(result) in messages


async def test_a_confirmed_plan_executes_through_a_real_worker(
    monkeypatch: Any,
) -> None:
    """Plan, confirm with ``y``, execute -- no mocked worker anywhere."""
    from pony.sync import ImapSyncService

    plan = _one_folder_plan()
    result = _one_message_result()
    executed: list[object] = []

    def _execute(_self: ImapSyncService, given: SyncPlan, **_kw: object) -> SyncResult:
        executed.append(given)
        return result

    monkeypatch.setattr(ImapSyncService, "plan", lambda _self, **_kw: plan)
    monkeypatch.setattr(ImapSyncService, "execute", _execute)
    app, *_ = build_pony_app(label="main-sync-real-exec")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        for _ in range(8):
            await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, SyncConfirmScreen)
        for _ in range(20):
            if dialog.query("#proceed").nodes:
                break
            await pilot.pause()
        await pilot.press("y")
        for _ in range(20):
            if isinstance(app.screen, MainScreen):
                break
            await pilot.pause()
        assert isinstance(app.screen, MainScreen)

    assert executed == [plan]
    assert describe_sync_result(result) in messages
