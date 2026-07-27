"""Focused sync orchestration tests for :mod:`pony.tui.screens.main_screen`."""

from __future__ import annotations

from enum import Enum, auto
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

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
)
from pony.tui.screens.main_screen import MainScreen
from pony.tui.screens.sync_confirm_screen import SyncConfirmScreen


class _State(Enum):
    RUNNING = auto()
    SUCCESS = auto()
    ERROR = auto()


def _main(app: object) -> MainScreen:
    return next(
        screen
        for screen in app.screen_stack  # type: ignore[attr-defined]
        if isinstance(screen, MainScreen)
    )


def _worker(
    *, name: str, state: _State, result: object = None, error: Exception | None = None
) -> Any:
    """Return a stand-in for a Textual ``Worker`` with just the fields read."""
    return SimpleNamespace(name=name, state=state, result=result, error=error)


def _notifications(app: object) -> list[str]:
    messages: list[str] = []
    original = app.notify  # type: ignore[attr-defined]

    def capture(message: str, **kwargs: object) -> None:
        messages.append(message)
        original(message, **kwargs)

    app.notify = capture  # type: ignore[attr-defined]
    return messages


async def test_sync_actions_require_credentials() -> None:
    app, *_ = build_pony_app(label="main-sync-no-credentials")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._credentials = None
        screen.action_sync()
        screen.action_background_sync()
        await pilot.pause()

    assert messages == ["No credentials provider.", "No credentials provider."]


async def test_sync_result_summary_aggregates_changed_accounts() -> None:
    app, *_ = build_pony_app(label="main-sync-summary")
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

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        assert screen._sync_result_summary(None) == "Sync complete."
        assert screen._sync_result_summary(result) == (
            "Sync complete.  fictional: +2 msgs, 3 merged, 4 pushed"
        )


async def test_plan_error_dismisses_modal_and_notifies() -> None:
    app, *_ = build_pony_app(label="main-sync-plan-error")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        app.push_screen(SyncConfirmScreen.planning())
        await pilot.pause()
        screen._on_plan_complete(
            _worker(
                name="sync-plan",
                state=_State.ERROR,
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
        app.push_screen(modal)
        await pilot.pause()
        screen._on_plan_complete(
            _worker(name="sync-plan", state=_State.SUCCESS, result=plan)
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
        app.push_screen(modal)
        await pilot.pause()

        info = ProgressInfo("Scanning", current=1, total=2)
        screen._sync_progress(info)
        modal.update_progress.assert_called_once_with(info)

        screen._on_exec_complete(
            _worker(
                name="sync-exec",
                state=_State.ERROR,
                error=RuntimeError("fictional execution failure"),
            )
        )
        modal.dismiss.assert_called_once_with(None)
        assert messages[-1] == "Sync failed: fictional execution failure"

        modal.dismiss.reset_mock()
        screen._on_exec_complete(
            _worker(
                name="sync-exec",
                state=_State.SUCCESS,
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

        for name in ("sync-plan", "sync-exec", "sync-bg", "something-else"):
            event = SimpleNamespace(worker=_worker(name=name, state=_State.SUCCESS))
            screen.on_worker_state_changed(event)  # type: ignore[arg-type]
        await pilot.pause()

        screen._on_plan_complete.assert_called_once()
        screen._on_exec_complete.assert_called_once()


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
                state=_State.ERROR,
                error=RuntimeError("fictional planning failure"),
            )
        )

        # Planning produced nothing to do.
        screen._on_plan_complete(
            _worker(
                name="sync-plan", state=_State.SUCCESS, result=SyncPlan(accounts=())
            )
        )

        # Execution finished.
        screen._on_exec_complete(
            _worker(
                name="sync-exec",
                state=_State.SUCCESS,
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
            _worker(name="sync-plan", state=_State.SUCCESS, result=plan)
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
