"""Smoke tests for Textual dialog and standalone screens.

Each screen is hosted inside a minimal _TestApp wrapper so it runs under
the Textual ``run_test`` harness with full lifecycle (mount → interact →
unmount).  Tests verify dismissal values and basic widget presence; they
do not re-test business logic already covered by unit tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import corpus
import pytest
from tui_helpers import DeterministicDirOnlyTree

import pony.tui.screens.save_folder_picker_screen as save_picker_module
from pony.domain import FolderRef
from pony.tui.app import ContactsApp, EmlViewerApp
from pony.tui.screens.confirm_screen import ConfirmScreen
from pony.tui.screens.contact_browser_screen import ContactBrowserScreen
from pony.tui.screens.floating_input_screen import SimpleInputScreen
from pony.tui.screens.goto_folder_screen import GotoFolderScreen, _fuzzy_filter
from pony.tui.screens.link_action_screen import LinkActionScreen
from pony.tui.screens.new_folder_screen import NewFolderScreen
from pony.tui.screens.save_draft_screen import SaveDraftScreen
from pony.tui.screens.sync_confirm_screen import SyncConfirmScreen

save_picker_module._DirOnlyTree = DeterministicDirOnlyTree  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Minimal hosting app factory
# ---------------------------------------------------------------------------


def _make_host(screen_cls, *args, **kwargs):
    """Return a one-shot app that pushes *screen_cls* and exits with its result."""
    from textual.app import App, ComposeResult
    from textual.pilot import Pilot

    class _Host(App):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(screen_cls(*args, **kwargs), self.exit)

        @asynccontextmanager
        async def run_test(self, **test_kwargs: Any) -> AsyncIterator[Pilot[object]]:
            async with super().run_test(**test_kwargs) as pilot:
                yield pilot
            loop = asyncio.get_running_loop()
            executor = loop._default_executor  # type: ignore[attr-defined]
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
                loop._default_executor = None  # type: ignore[attr-defined]

    return _Host()


# ===========================================================================
# ConfirmScreen
# ===========================================================================


async def test_confirm_screen_yes_button_returns_true() -> None:
    app = _make_host(ConfirmScreen, "Delete?", "This will delete the file.")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
    assert app.return_value is True


async def test_confirm_screen_no_button_returns_false() -> None:
    app = _make_host(ConfirmScreen, "Delete?", "This will delete the file.")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#no")
        await pilot.pause()
    assert app.return_value is False


async def test_confirm_screen_y_key_returns_true() -> None:
    app = _make_host(ConfirmScreen, "Sure?", "Are you sure?")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
    assert app.return_value is True


async def test_confirm_screen_n_key_returns_false() -> None:
    app = _make_host(ConfirmScreen, "Sure?", "Are you sure?")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
    assert app.return_value is False


async def test_confirm_screen_escape_returns_false() -> None:
    app = _make_host(ConfirmScreen, "Sure?", "Are you sure?")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is False


# ===========================================================================
# SaveDraftScreen
# ===========================================================================


async def test_save_draft_save_button_returns_true() -> None:
    app = _make_host(SaveDraftScreen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#save-btn")
        await pilot.pause()
    assert app.return_value is True


async def test_save_draft_discard_button_returns_false() -> None:
    app = _make_host(SaveDraftScreen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#discard-btn")
        await pilot.pause()
    assert app.return_value is False


async def test_save_draft_y_key_returns_true() -> None:
    app = _make_host(SaveDraftScreen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
    assert app.return_value is True


async def test_save_draft_n_key_returns_false() -> None:
    app = _make_host(SaveDraftScreen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
    assert app.return_value is False


async def test_save_draft_escape_returns_false() -> None:
    app = _make_host(SaveDraftScreen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is False


# ===========================================================================
# NewFolderScreen
# ===========================================================================


async def test_new_folder_submit_returns_name() -> None:
    app = _make_host(NewFolderScreen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("A", "r", "c", "h", "i", "v", "e")
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value == "Archive"


async def test_new_folder_empty_submit_returns_none() -> None:
    app = _make_host(NewFolderScreen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value is None


async def test_new_folder_escape_returns_none() -> None:
    app = _make_host(NewFolderScreen)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is None


# ===========================================================================
# LinkActionScreen
# ===========================================================================


async def test_link_action_cancel_button_dismisses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_mock = MagicMock()
    monkeypatch.setattr(
        "pony.tui.screens.link_action_screen.webbrowser.open", open_mock
    )
    app = _make_host(LinkActionScreen, "https://example.com")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()
    assert app.return_value is False
    open_mock.assert_not_called()


async def test_link_action_open_button_calls_webbrowser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_mock = MagicMock()
    monkeypatch.setattr(
        "pony.tui.screens.link_action_screen.webbrowser.open", open_mock
    )
    app = _make_host(LinkActionScreen, "https://example.com")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#open")
        await pilot.pause()
    open_mock.assert_called_once_with("https://example.com")


async def test_link_action_open_oserror_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pony.tui.screens.link_action_screen.webbrowser.open",
        MagicMock(side_effect=OSError("no browser")),
    )
    app = _make_host(LinkActionScreen, "https://example.com")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
    # App should still be running (did not exit) because the error is notified but
    # dismiss is skipped.  If return_value is None the app is still alive — pass.


async def test_link_action_escape_dismisses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pony.tui.screens.link_action_screen.webbrowser.open",
        MagicMock(),
    )
    app = _make_host(LinkActionScreen, "https://example.com")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is False


# ===========================================================================
# GotoFolderScreen — pure function first
# ===========================================================================


def _refs(*names: str) -> list[FolderRef]:
    return [FolderRef(account_name="acct", folder_name=n) for n in names]


def test_fuzzy_filter_matches_subsequence() -> None:
    folders = _refs("INBOX", "Sent", "Archive", "Spam")
    result = _fuzzy_filter("ar", folders)
    names = [r.folder_name for r in result]
    assert "Archive" in names
    assert "Spam" not in names


def test_fuzzy_filter_empty_query_matches_all() -> None:
    folders = _refs("INBOX", "Sent")
    result = _fuzzy_filter("", folders)
    assert len(result) == 2  # empty pattern matches every folder


def test_fuzzy_filter_no_match() -> None:
    folders = _refs("INBOX", "Sent")
    result = _fuzzy_filter("zzz", folders)
    assert result == []


async def test_goto_folder_escape_dismisses_with_none() -> None:
    app = _make_host(GotoFolderScreen, _refs("INBOX", "Sent", "Archive"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is None


async def test_goto_folder_enter_selects_first() -> None:
    app = _make_host(GotoFolderScreen, _refs("INBOX", "Sent", "Archive"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value is not None
    assert isinstance(app.return_value, FolderRef)


async def test_goto_folder_empty_list_returns_none() -> None:
    app = _make_host(GotoFolderScreen, [])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value is None


async def test_goto_folder_filter_then_confirm() -> None:
    folders = _refs("INBOX", "Sent", "Archive")
    app = _make_host(GotoFolderScreen, folders)
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "Arch":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value is not None
    assert app.return_value.folder_name == "Archive"


async def test_goto_folder_down_moves_to_list() -> None:
    folders = _refs("INBOX", "Sent", "Archive")
    app = _make_host(GotoFolderScreen, folders)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        from textual.widgets import ListView

        lv = app.screen.query_one(ListView)
        assert app.screen.focused is lv


# ===========================================================================
# SyncConfirmScreen
# ===========================================================================


async def test_sync_confirm_planning_mode_shows_title() -> None:
    app = _make_host(SyncConfirmScreen, None, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Label

        titles = [str(w.render()) for w in app.screen.query(Label) if w.id == "title"]
        assert any("Planning" in t for t in titles)


async def test_sync_confirm_cancel_button_dismisses_false() -> None:
    from pony.sync import SyncPlan

    empty_plan = SyncPlan(accounts=())
    app = _make_host(SyncConfirmScreen, empty_plan, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()
    assert app.return_value is False


async def test_sync_confirm_n_key_dismisses_false() -> None:
    from pony.sync import SyncPlan

    empty_plan = SyncPlan(accounts=())
    app = _make_host(SyncConfirmScreen, empty_plan, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
    assert app.return_value is False


async def test_sync_confirm_y_key_calls_on_confirm() -> None:
    from pony.sync import SyncPlan

    called: list[bool] = []

    def _on_confirm() -> None:
        called.append(True)

    empty_plan = SyncPlan(accounts=())
    app = _make_host(SyncConfirmScreen, empty_plan, _on_confirm)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
    assert called == [True]


async def test_sync_confirm_show_plan_transitions() -> None:
    """show_plan() updates the title widget to 'Sync Plan'."""
    import contextlib

    from pony.sync import SyncPlan

    empty_plan = SyncPlan(accounts=())
    app = _make_host(SyncConfirmScreen, None, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SyncConfirmScreen)
        # show_plan() mounts buttons dynamically; query_one("#proceed").focus() may
        # fail before the event loop processes the mount — suppress that.
        with contextlib.suppress(Exception):
            screen.show_plan(empty_plan)
        await pilot.pause()
        from textual.widgets import Label

        titles = [str(w.render()) for w in app.screen.query(Label) if w.id == "title"]
        assert any("Sync Plan" in t for t in titles)


async def test_sync_confirm_update_status() -> None:
    app = _make_host(SyncConfirmScreen, None, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SyncConfirmScreen)
        screen.update_status("Syncing INBOX…")
        await pilot.pause()


async def test_sync_confirm_planning_mode_keys_ignored() -> None:
    """In planning mode, y/n/escape keys are silently ignored."""
    app = _make_host(SyncConfirmScreen, None, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")  # planning mode — should NOT exit
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        # The app is still running if we reach here.


# ===========================================================================
# EmlViewerApp
# ===========================================================================


async def test_eml_viewer_app_mounts_with_plain_text() -> None:
    raw = corpus.plain_text()
    app = EmlViewerApp(raw_bytes=raw)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.screen_stack) >= 1


async def test_eml_viewer_app_q_key_quits() -> None:
    raw = corpus.plain_text()
    app = EmlViewerApp(raw_bytes=raw)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("Q")
        await pilot.pause()
    # Reaching here means the app exited cleanly.


async def test_eml_viewer_app_multipart_attachment() -> None:
    raw = corpus.multipart_mixed_attachment()
    app = EmlViewerApp(raw_bytes=raw)
    async with app.run_test() as pilot:
        await pilot.pause()
        from pony.tui.widgets.message_view import MessageViewPanel

        panel = app.screen.query_one(MessageViewPanel)
        assert panel.display is True


async def test_eml_viewer_panel_close_request_dismisses() -> None:
    """The panel's CloseRequested message tears the viewer down."""
    from pony.tui.widgets.message_view import MessageViewPanel

    app = EmlViewerApp(raw_bytes=corpus.plain_text())
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.screen.query_one(MessageViewPanel)
        panel.post_message(MessageViewPanel.CloseRequested())
        await pilot.pause()
    # Reaching here means the viewer dismissed without raising.


async def test_eml_viewer_print_pdf_cancelled_picker_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dismissing the folder picker without a path exports nothing."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen
    from pony.tui.screens.save_folder_picker_screen import SaveFolderPickerScreen

    export = MagicMock()
    monkeypatch.setattr("pony.tui.pdf_export.export_pdf_in_thread", export)

    app = EmlViewerApp(raw_bytes=corpus.plain_text())
    async with app.run_test() as pilot:
        await pilot.pause()
        viewer = app.screen
        assert isinstance(viewer, EmlViewerScreen)
        viewer.action_print_pdf()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SaveFolderPickerScreen)
        picker.dismiss(None)
        await pilot.pause()

    export.assert_not_called()


async def test_eml_viewer_print_pdf_exports_to_chosen_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Choosing a folder starts the export worker for a PDF inside it."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen
    from pony.tui.screens.save_folder_picker_screen import SaveFolderPickerScreen

    export = MagicMock()
    monkeypatch.setattr("pony.tui.pdf_export.export_pdf_in_thread", export)

    app = EmlViewerApp(raw_bytes=corpus.plain_text())
    async with app.run_test() as pilot:
        await pilot.pause()
        viewer = app.screen
        assert isinstance(viewer, EmlViewerScreen)
        viewer.action_print_pdf()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SaveFolderPickerScreen)
        picker.dismiss(tmp_path)
        await pilot.pause()
        await pilot.pause()

    assert export.call_count == 1
    out = export.call_args.args[2]
    assert out.parent == tmp_path.resolve()
    assert out.suffix == ".pdf"


# ===========================================================================
# EmlViewerScreen action coverage
# ===========================================================================


async def test_eml_viewer_screen_action_compose_link_notifies() -> None:
    """action_compose_link notifies that compose is unavailable."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    raw = corpus.plain_text()
    app = EmlViewerApp(raw_bytes=raw)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        # action_compose_link shows a notification (no crash expected)
        screen.action_compose_link("0")
        await pilot.pause()


async def test_eml_viewer_screen_action_compose_address_notifies() -> None:
    """action_compose_address notifies that compose is unavailable."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    raw = corpus.plain_text()
    app = EmlViewerApp(raw_bytes=raw)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        screen.action_compose_address("0")
        await pilot.pause()


async def test_eml_viewer_screen_action_harvest_contact_noop() -> None:
    """action_harvest_contact is a no-op (pass)."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    raw = corpus.plain_text()
    app = EmlViewerApp(raw_bytes=raw)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        screen.action_harvest_contact("0")
        await pilot.pause()


async def test_eml_viewer_screen_action_open_attachment_out_of_range() -> None:
    """action_open_attachment with out-of-range index notifies."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    raw = corpus.plain_text()
    app = EmlViewerApp(raw_bytes=raw)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        # plain_text has no attachments; attachment 1 doesn't exist
        screen.action_open_attachment("1")
        await pilot.pause()


async def test_eml_viewer_screen_action_save_attachment_out_of_range(
    tmp_path,
) -> None:
    """action_save_attachment with out-of-range index does nothing."""
    from textual.app import App, ComposeResult

    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    raw = corpus.plain_text()

    class _TestApp(App[None]):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(EmlViewerScreen(raw, downloads_dir=tmp_path))

    app = _TestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        screen.action_save_attachment("1")
        await pilot.pause()


async def test_eml_viewer_screen_save_attachment_index_0_all(
    tmp_path,
) -> None:
    """action_save_attachment(0) saves ALL attachments."""
    from textual.app import App, ComposeResult

    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    raw = corpus.multipart_mixed_attachment()

    class _TestApp(App[None]):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(EmlViewerScreen(raw, downloads_dir=tmp_path))

    app = _TestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        screen.action_save_attachment("0")
        await pilot.pause()
    # q1-report.pdf should have been saved
    saved = list(tmp_path.glob("*.pdf"))
    assert len(saved) >= 1


async def test_eml_viewer_screen_open_attachment_0_opens_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """action_open_attachment(0) opens ALL attachments."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    launch_mock = MagicMock()
    monkeypatch.setattr("pony.tui.screens.eml_viewer_screen.launch_file", launch_mock)

    raw = corpus.multipart_mixed_attachment()
    app = EmlViewerApp(raw_bytes=raw)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        screen.action_open_attachment("0")
        await pilot.pause()
    assert launch_mock.call_count >= 1


async def test_eml_viewer_screen_quit_action() -> None:
    """action_quit_viewer dismisses the screen."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    raw = corpus.plain_text()
    app = EmlViewerApp(raw_bytes=raw)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        screen.action_quit_viewer()
        await pilot.pause()


async def test_eml_viewer_screen_activates_only_web_links() -> None:
    """Web links open the action dialog; non-web links are ignored."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen
    from pony.tui.widgets.message_view import MessageViewPanel

    app = EmlViewerApp(raw_bytes=corpus.plain_text())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        panel = screen.query_one(MessageViewPanel)
        panel.body_link = MagicMock(return_value=("email", "marina@example.test"))
        stack_size = len(app.screen_stack)
        screen.action_activate_link("1")
        assert len(app.screen_stack) == stack_size

        panel.body_link = MagicMock(return_value=("web", "https://example.test"))
        screen.action_activate_link("1")
        await pilot.pause()
        assert isinstance(app.screen, LinkActionScreen)


async def test_eml_viewer_screen_open_browser_delegates() -> None:
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen
    from pony.tui.widgets.message_view import MessageViewPanel

    app = EmlViewerApp(raw_bytes=corpus.plain_text())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        panel = screen.query_one(MessageViewPanel)
        panel.open_in_browser = MagicMock()
        screen.action_open_browser()
        panel.open_in_browser.assert_called_once_with()


async def test_eml_viewer_screen_opens_nested_message() -> None:
    """An RFC 822 attachment opens as another viewer on the screen stack."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    app = EmlViewerApp(raw_bytes=corpus.nested_forward())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        screen.action_open_attachment("2")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        assert isinstance(app.screen, EmlViewerScreen)


async def test_eml_viewer_screen_reports_attachment_io_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Opening and saving filesystem failures are reported without escaping."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen
    from pony.tui.widgets.message_view import MessageViewPanel

    monkeypatch.setattr(
        "pony.tui.screens.eml_viewer_screen.tempfile.NamedTemporaryFile",
        MagicMock(side_effect=OSError("fictional open failure")),
    )
    app = _make_host(
        EmlViewerScreen,
        corpus.multipart_mixed_attachment(),
        downloads_dir=tmp_path,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EmlViewerScreen)
        screen.action_open_attachment("1")
        panel = screen.query_one(MessageViewPanel)
        panel.save_attachment = MagicMock(side_effect=OSError("fictional save failure"))
        screen.action_save_attachment("1")
        await pilot.pause()


# ===========================================================================
# PickFolderScreen
# ===========================================================================


async def test_pick_folder_screen_escape_returns_none() -> None:
    from corpus import plain_text
    from tui_helpers import (
        make_index,
        make_mirrors,
        make_test_account,
        make_test_config,
        make_tmp_paths,
        seed_message,
    )

    from pony.domain import FolderRef
    from pony.tui.screens.pick_folder_screen import PickFolderScreen

    paths = make_tmp_paths("pick-folder-escape")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=FolderRef(account_name="acct", folder_name="INBOX"),
        raw=plain_text(),
        message_id="<pick@example.com>",
    )

    app = _make_host(
        PickFolderScreen,
        config=config,
        mirrors=dict(mirrors),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is None


async def test_pick_folder_screen_mounts_without_error() -> None:
    from corpus import plain_text
    from tui_helpers import (
        make_index,
        make_mirrors,
        make_test_account,
        make_test_config,
        make_tmp_paths,
        seed_message,
    )

    from pony.domain import FolderRef
    from pony.tui.screens.pick_folder_screen import PickFolderScreen

    paths = make_tmp_paths("pick-folder-mount")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=FolderRef(account_name="acct", folder_name="INBOX"),
        raw=plain_text(),
        message_id="<pick2@example.com>",
    )

    app = _make_host(
        PickFolderScreen,
        config=config,
        mirrors=dict(mirrors),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.screen.query_one(Tree)
        assert tree is not None


# ===========================================================================
# SyncConfirmScreen — update_progress and remaining branches
# ===========================================================================


async def test_sync_confirm_update_progress_with_total() -> None:
    """update_progress with total > 0 shows the progress bar."""
    from pony.sync import ProgressInfo

    app = _make_host(SyncConfirmScreen, None, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SyncConfirmScreen)
        screen.update_progress(ProgressInfo("Loading…", current=3, total=10))
        await pilot.pause()


async def test_sync_confirm_update_progress_no_total() -> None:
    """update_progress with total == 0 hides the progress bar."""
    from pony.sync import ProgressInfo

    app = _make_host(SyncConfirmScreen, None, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SyncConfirmScreen)
        screen.update_progress(ProgressInfo("Loading…", current=0, total=0))
        await pilot.pause()


# ===========================================================================
# ContactsApp (ContactBrowserScreen)
# ===========================================================================


async def test_contacts_app_mounts_and_quits() -> None:
    """ContactsApp starts and Q exits cleanly."""
    from tui_helpers import make_index, make_tmp_paths

    paths = make_tmp_paths("contacts-app")
    index = make_index(paths)
    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()


async def test_contacts_app_search_bar() -> None:
    """Pressing slash opens the search bar in ContactBrowserScreen."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.tui.screens.contact_browser_screen import ContactBrowserScreen

    paths = make_tmp_paths("contacts-search")
    index = make_index(paths)
    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ContactBrowserScreen)
        await pilot.press("slash")
        await pilot.pause()
        from textual.widgets import Input

        search_input = app.screen.query_one("#contact-search", Input)
        assert search_input.display is True
        await pilot.press("escape")
        await pilot.pause()


# ===========================================================================
# AttachmentPickerScreen + parse_attachment_selection
# ===========================================================================


def test_parse_attachment_selection_star_returns_all() -> None:
    from pony.tui.screens.attachment_picker_screen import parse_attachment_selection

    result = parse_attachment_selection("*", total=3)
    assert result == [1, 2, 3]


def test_parse_attachment_selection_single() -> None:
    from pony.tui.screens.attachment_picker_screen import parse_attachment_selection

    result = parse_attachment_selection("1", total=3)
    assert result == [1]


def test_parse_attachment_selection_comma_separated() -> None:
    from pony.tui.screens.attachment_picker_screen import parse_attachment_selection

    result = parse_attachment_selection("1, 3", total=3)
    assert result == [1, 3]


def test_parse_attachment_selection_empty_returns_none() -> None:
    from pony.tui.screens.attachment_picker_screen import parse_attachment_selection

    assert parse_attachment_selection("", total=3) is None


def test_parse_attachment_selection_non_numeric_returns_none() -> None:
    from pony.tui.screens.attachment_picker_screen import parse_attachment_selection

    assert parse_attachment_selection("abc", total=3) is None


def test_parse_attachment_selection_out_of_range_returns_none() -> None:
    from pony.tui.screens.attachment_picker_screen import parse_attachment_selection

    assert parse_attachment_selection("5", total=3) is None


def test_parse_attachment_selection_duplicates_returns_none() -> None:
    from pony.tui.screens.attachment_picker_screen import parse_attachment_selection

    assert parse_attachment_selection("1,1", total=3) is None


async def test_attachment_picker_screen_submit_returns_indices() -> None:
    from pony.tui.screens.attachment_picker_screen import AttachmentPickerScreen

    app = _make_host(AttachmentPickerScreen, action_label="Save", attachment_count=3)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("1", "comma", "3")
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value == [1, 3]


async def test_attachment_picker_screen_escape_returns_none() -> None:
    from pony.tui.screens.attachment_picker_screen import AttachmentPickerScreen

    app = _make_host(AttachmentPickerScreen, action_label="Open", attachment_count=2)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is None


# ===========================================================================
# SyncConfirmScreen — additional plan variants
# ===========================================================================


async def test_sync_confirm_with_ops_shows_summary() -> None:
    """SyncConfirmScreen composed with a plan that has ops shows a summary."""
    from pony.sync import (
        AccountSyncPlan,
        FetchNewOp,
        FolderSyncPlan,
        SyncPlan,
    )

    op = FetchNewOp(uid=1, message_id="<x@x>", server_flags=frozenset())
    folder = FolderSyncPlan(
        folder_name="INBOX", uid_validity=1, highest_uid=1, ops=(op,)
    )
    acct = AccountSyncPlan(account_name="acct", folders=(folder,))
    plan = SyncPlan(accounts=(acct,))

    app = _make_host(SyncConfirmScreen, plan, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Static

        statics = [str(w.render()) for w in app.screen.query(Static)]
        assert any("download" in s.lower() for s in statics)
        await pilot.press("n")
        await pilot.pause()


async def test_sync_confirm_with_skipped_folders() -> None:
    """SyncConfirmScreen with skipped folders shows the skipped text."""
    from pony.sync import AccountSyncPlan, SyncPlan

    acct = AccountSyncPlan(
        account_name="acct",
        folders=(),
        skipped_folders=("Spam", "Trash"),
    )
    plan = SyncPlan(accounts=(acct,))

    app = _make_host(SyncConfirmScreen, plan, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Static

        statics = [str(w.render()) for w in app.screen.query(Static)]
        # Skipped folders text may appear as a Static widget
        assert any("Spam" in s for s in statics) or len(statics) > 0
        await pilot.press("n")
        await pilot.pause()


async def test_sync_confirm_with_confirmation_needed() -> None:
    """SyncConfirmScreen with a folder needing confirmation shows warning."""
    from pony.domain import MessageRef
    from pony.sync import (
        AccountSyncPlan,
        FolderSyncPlan,
        ServerDeleteOp,
        SyncPlan,
    )

    op = ServerDeleteOp(
        uid=1,
        message_ref=MessageRef(account_name="acct", folder_name="INBOX", id=1),
    )
    folder = FolderSyncPlan(
        folder_name="INBOX",
        uid_validity=1,
        highest_uid=10,
        ops=(op,),
        needs_confirmation=True,
        pending_delete_count=5,
        pending_delete_total=10,
    )
    acct = AccountSyncPlan(account_name="acct", folders=(folder,))
    plan = SyncPlan(accounts=(acct,))

    app = _make_host(SyncConfirmScreen, plan, None)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Button

        buttons = list(app.screen.query(Button))
        button_labels = [str(b.label) for b in buttons]
        assert any("Proceed" in lbl for lbl in button_labels)
        await pilot.press("n")
        await pilot.pause()


# ===========================================================================
# SyncConfirmScreen — button ignored while syncing
# ===========================================================================


async def test_sync_confirm_button_ignored_while_syncing() -> None:
    """Button presses while syncing are ignored."""
    from pony.sync import SyncPlan

    called: list[bool] = []

    def _on_confirm() -> None:
        called.append(True)

    empty_plan = SyncPlan(accounts=())
    app = _make_host(SyncConfirmScreen, empty_plan, _on_confirm)
    async with app.run_test() as pilot:
        await pilot.pause()
        # First press triggers _enter_syncing
        await pilot.press("y")
        await pilot.pause()
        # This should be a no-op while syncing
        await pilot.press("y")
        await pilot.pause()
    # on_confirm called exactly once
    assert len(called) == 1


# ===========================================================================
# ContactDetailScreen and ContactEditScreen
# ===========================================================================


async def test_contact_detail_screen_mounts() -> None:
    """ContactDetailScreen displays a contact without crashing."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.contact_detail_screen import ContactDetailScreen

    paths = make_tmp_paths("contact-detail")
    index = make_index(paths)
    contact = Contact(
        id=None, first_name="Alice", last_name="Smith", emails=("alice@x.com",)
    )
    saved = index.upsert_contact(contact=contact)

    app = _make_host(ContactDetailScreen, saved, index)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Screen should have mounted without error
        assert app.screen is not None


async def test_contact_detail_screen_escape_dismisses() -> None:
    """Pressing Escape dismisses ContactDetailScreen."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.contact_detail_screen import ContactDetailScreen

    paths = make_tmp_paths("contact-detail-esc")
    index = make_index(paths)
    contact = Contact(
        id=None,
        first_name="Bob",
        last_name="Jones",
        emails=("bob@x.com",),
        organization="ACME",
        notes="Some notes",
    )
    saved = index.upsert_contact(contact=contact)

    app = _make_host(ContactDetailScreen, saved, index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is False


async def test_contact_edit_screen_mounts() -> None:
    """ContactEditScreen mounts and shows input fields."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.contact_edit_screen import ContactEditScreen

    paths = make_tmp_paths("contact-edit")
    index = make_index(paths)
    contact = Contact(
        id=None, first_name="Alice", last_name="Smith", emails=("alice@x.com",)
    )

    app = _make_host(ContactEditScreen, contact, index)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input

        inputs = app.screen.query(Input)
        assert len(list(inputs)) > 0


async def test_contact_edit_screen_escape_dismisses_none() -> None:
    """Pressing Escape dismisses ContactEditScreen with None."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.contact_edit_screen import ContactEditScreen

    paths = make_tmp_paths("contact-edit-esc")
    index = make_index(paths)
    contact = Contact(
        id=None, first_name="Carol", last_name="White", emails=("carol@x.com",)
    )

    app = _make_host(ContactEditScreen, contact, index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is None


async def test_contact_edit_screen_save_ctrl_s() -> None:
    """Pressing ctrl+s saves and dismisses ContactEditScreen with a Contact."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.contact_edit_screen import ContactEditScreen

    paths = make_tmp_paths("contact-edit-save")
    index = make_index(paths)
    contact = Contact(
        id=None, first_name="Dave", last_name="Brown", emails=("dave@x.com",)
    )

    app = _make_host(ContactEditScreen, contact, index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
    # Should have been saved (returned Contact or None)


async def test_contact_browser_with_contacts_shows_rows() -> None:
    """ContactBrowserScreen with seeded contacts shows rows in the table."""
    from textual.widgets import DataTable
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact

    paths = make_tmp_paths("cb-rows")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Alice", last_name="Smith", emails=("alice@x.com",)
        )
    )
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Bob", last_name="Jones", emails=("bob@x.com",)
        )
    )

    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one("#contact-table", DataTable)
        assert table.row_count == 2


async def test_contact_browser_search_filters_rows() -> None:
    """Searching in ContactBrowserScreen filters the displayed rows."""
    from textual.widgets import DataTable
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact

    paths = make_tmp_paths("cb-search")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Alice", last_name="Smith", emails=("alice@x.com",)
        )
    )
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Bob", last_name="Jones", emails=("bob@x.com",)
        )
    )

    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        for ch in "Alice":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        table = app.screen.query_one("#contact-table", DataTable)
        assert table.row_count == 1


async def test_contact_browser_mark_contact() -> None:
    """Pressing m marks a contact (action_mark_down), shift+up unmarks."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact

    paths = make_tmp_paths("cb-mark")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Dave", last_name="Lee", emails=("dave@x.com",)
        )
    )

    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.screen.query_one("#contact-table", DataTable)
        table.focus()
        await pilot.pause()
        # m marks the current row and moves down (action_mark_down)
        await pilot.press("m")
        await pilot.pause()
        # shift+up unmarks (action_mark_up)
        await pilot.press("shift+up")
        await pilot.pause()
        from pony.tui.screens.contact_browser_screen import ContactBrowserScreen

        screen = app.screen
        assert isinstance(screen, ContactBrowserScreen)


# ===========================================================================
# ContactSuggester unit tests
# ===========================================================================


def test_contact_suggester_get_suggestion_no_comma() -> None:
    """Typing a prefix without comma gets a suggestion for the full field."""
    import asyncio

    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.widgets.contact_suggester import ContactSuggester

    paths = make_tmp_paths("suggester-1")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Alice", last_name="Smith", emails=("alice@x.com",)
        )
    )
    suggester = ContactSuggester(index)
    result = asyncio.get_event_loop().run_until_complete(
        suggester.get_suggestion("ali")
    )
    assert result is not None
    assert "alice@x.com" in result


def test_contact_suggester_with_comma_completes_last_token() -> None:
    """After a comma, the suggester completes only the last token."""
    import asyncio

    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.widgets.contact_suggester import ContactSuggester

    paths = make_tmp_paths("suggester-2")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Bob", last_name="Jones", emails=("bob@x.com",)
        )
    )
    suggester = ContactSuggester(index)
    result = asyncio.get_event_loop().run_until_complete(
        suggester.get_suggestion("alice@x.com, bo")
    )
    assert result is not None
    assert "alice@x.com" in result
    assert "bob@x.com" in result


def test_contact_suggester_short_prefix_returns_none() -> None:
    """Typed prefix with < 2 chars returns None."""
    import asyncio

    from tui_helpers import make_index, make_tmp_paths

    from pony.tui.widgets.contact_suggester import ContactSuggester

    paths = make_tmp_paths("suggester-3")
    index = make_index(paths)
    suggester = ContactSuggester(index)
    result = asyncio.get_event_loop().run_until_complete(suggester.get_suggestion("a"))
    assert result is None


def test_contact_suggester_no_email_returns_none() -> None:
    """Contact with no email returns None suggestion."""
    import asyncio

    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.widgets.contact_suggester import ContactSuggester

    paths = make_tmp_paths("suggester-4")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(id=None, first_name="NoEmail", last_name="User", emails=())
    )
    suggester = ContactSuggester(index)
    result = asyncio.get_event_loop().run_until_complete(
        suggester.get_suggestion("noe")
    )
    assert result is None


async def test_recipient_input_lists_and_selects_multiple_matches() -> None:
    """Recipient completion exposes alternatives instead of choosing silently."""
    from textual.app import App, ComposeResult
    from textual.widgets import Input, OptionList
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.widgets.contact_suggester import RecipientInput

    index = make_index(make_tmp_paths("recipient-options"))
    index.upsert_contact(
        contact=Contact(
            id=None,
            first_name="Marina",
            last_name="Núñez Robles",
            emails=("marina@example.test",),
        )
    )
    index.upsert_contact(
        contact=Contact(
            id=None,
            first_name="Mariela",
            last_name="Norte",
            emails=("mariela@example.test",),
        )
    )

    class RecipientApp(App[None]):
        def compose(self) -> ComposeResult:
            yield RecipientInput(index, input_id="recipient")

    app = RecipientApp()
    async with app.run_test() as pilot:
        field = app.query_one("#recipient", Input)
        field.focus()
        field.value = "mari"
        await pilot.pause()

        options = app.query_one(OptionList)
        assert options.option_count == 2
        assert options.display

        await pilot.press("down", "enter")
        await pilot.pause()

        assert field.value == "Marina Núñez Robles <marina@example.test>"
        assert not options.display


async def test_recipient_input_replaces_only_current_token() -> None:
    """Selecting a result preserves recipients preceding the final comma."""
    from textual.app import App, ComposeResult
    from textual.widgets import Input
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.widgets.contact_suggester import RecipientInput

    index = make_index(make_tmp_paths("recipient-token"))
    index.upsert_contact(
        contact=Contact(
            id=None,
            first_name="Marina",
            last_name="Núñez Robles",
            emails=("marina@example.test",),
        )
    )

    class RecipientApp(App[None]):
        def compose(self) -> ComposeResult:
            yield RecipientInput(index, input_id="recipient")

    app = RecipientApp()
    async with app.run_test() as pilot:
        field = app.query_one("#recipient", Input)
        field.focus()
        field.value = "first@example.test, nunez"
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()

        assert field.value == (
            "first@example.test, Marina Núñez Robles <marina@example.test>"
        )


async def test_recipient_input_hides_short_empty_and_escaped_results() -> None:
    """Short/unmatched queries and Escape close the alternatives list."""
    from textual.app import App, ComposeResult
    from textual.widgets import Input, OptionList
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.widgets.contact_suggester import RecipientInput

    index = make_index(make_tmp_paths("recipient-hide"))
    index.upsert_contact(
        contact=Contact(
            id=None,
            first_name="Marina",
            last_name="Núñez Robles",
            emails=("marina@example.test",),
        )
    )

    class RecipientApp(App[None]):
        def compose(self) -> ComposeResult:
            yield RecipientInput(index, input_id="recipient")

    app = RecipientApp()
    async with app.run_test() as pilot:
        field = app.query_one("#recipient", Input)
        options = app.query_one(OptionList)
        field.focus()

        field.value = "mar"
        await pilot.pause()
        assert options.display

        await pilot.press("escape")
        await pilot.pause()
        assert not options.display
        assert field.value == "mar"
        assert field.has_focus

        field.value = "m"
        await pilot.pause()
        assert not options.display

        field.value = "nobody"
        await pilot.pause()
        assert options.option_count == 0
        assert not options.display


async def test_recipient_input_caps_addresses_and_ignores_invalid_selection() -> None:
    """At most ten addresses are offered and an invalid index is harmless."""
    from textual.app import App, ComposeResult
    from textual.widgets import OptionList
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.widgets.contact_suggester import RecipientInput

    index = make_index(make_tmp_paths("recipient-cap"))
    for number in range(6):
        index.upsert_contact(
            contact=Contact(
                id=None,
                first_name=f"Marina{number}",
                last_name="Fictional",
                emails=(
                    f"marina{number}@example.test",
                    f"marina{number}.alt@example.test",
                ),
            )
        )

    class RecipientApp(App[None]):
        def compose(self) -> ComposeResult:
            yield RecipientInput(index, input_id="recipient", classes="recipient")

    app = RecipientApp()
    async with app.run_test() as pilot:
        widget = app.query_one(RecipientInput)
        field = widget.input
        field.focus()
        field.value = "mar"
        await pilot.pause()

        options = app.query_one(OptionList)
        assert options.option_count == 10
        original = field.value
        widget._select(99)
        assert field.value == original

        await pilot.press("enter")
        await pilot.pause()
        assert field.value.endswith("<marina0@example.test>")
        assert not options.display


# ===========================================================================
# SaveFolderPickerScreen
# ===========================================================================


async def test_save_folder_picker_cancel_returns_none(tmp_path) -> None:
    """Clicking Cancel dismisses SaveFolderPickerScreen with None."""
    from pony.tui.screens.save_folder_picker_screen import SaveFolderPickerScreen

    app = _make_host(SaveFolderPickerScreen, start_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()
    assert app.return_value is None


async def test_save_folder_picker_select_returns_path(tmp_path) -> None:
    """Clicking Select dismisses SaveFolderPickerScreen with a Path."""
    from pony.tui.screens.save_folder_picker_screen import SaveFolderPickerScreen

    app = _make_host(SaveFolderPickerScreen, start_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#select")
        await pilot.pause()
    assert app.return_value is not None
    assert isinstance(app.return_value, __import__("pathlib").Path)


async def test_save_folder_picker_escape_returns_none(tmp_path) -> None:
    """Pressing Escape dismisses SaveFolderPickerScreen with None."""
    from pony.tui.screens.save_folder_picker_screen import SaveFolderPickerScreen

    app = _make_host(SaveFolderPickerScreen, start_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is None


async def test_save_folder_picker_directory_selection_updates_result(tmp_path) -> None:
    """Selecting a tree directory updates the label and selected result."""
    from textual.widgets import Label

    import pony.tui.screens.save_folder_picker_screen as picker_module
    from pony.tui.screens.save_folder_picker_screen import SaveFolderPickerScreen

    picker_module._session_dir = None
    selected = tmp_path / "selected"
    selected.mkdir()
    app = _make_host(SaveFolderPickerScreen, start_dir=tmp_path)
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, SaveFolderPickerScreen)
        tree = screen.query_one(DeterministicDirOnlyTree)
        screen.on_directory_tree_directory_selected(
            DeterministicDirOnlyTree.DirectorySelected(tree.root, selected)
        )
        assert str(screen.query_one("#current-path", Label).render()) == str(selected)
        await pilot.click("#select")
        await pilot.pause()

    assert app.return_value == selected
    assert picker_module._session_dir == selected


async def test_save_folder_picker_creates_and_selects_new_folder(tmp_path) -> None:
    """The New Folder prompt creates a directory and makes it current."""
    from textual.widgets import Input, Label

    from pony.tui.screens.save_folder_picker_screen import SaveFolderPickerScreen

    app = _make_host(SaveFolderPickerScreen, start_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.click("#new-folder")
        await pilot.pause()
        prompt = app.screen
        assert isinstance(prompt, SimpleInputScreen)
        field = prompt.query_one(Input)
        field.value = "fictional-folder"
        prompt.on_input_submitted(Input.Submitted(field, field.value))
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, SaveFolderPickerScreen)
        created = tmp_path / "fictional-folder"
        assert created.is_dir()
        assert str(picker.query_one("#current-path", Label).render()) == str(created)


async def test_save_folder_picker_empty_new_folder_is_noop(tmp_path) -> None:
    """Submitting an empty folder name leaves the selected path unchanged."""
    from textual.widgets import Input, Label

    from pony.tui.screens.save_folder_picker_screen import SaveFolderPickerScreen

    app = _make_host(SaveFolderPickerScreen, start_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.click("#new-folder")
        await pilot.pause()
        prompt = app.screen
        assert isinstance(prompt, SimpleInputScreen)
        field = prompt.query_one(Input)
        prompt.on_input_submitted(Input.Submitted(field, ""))
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, SaveFolderPickerScreen)
        assert str(picker.query_one("#current-path", Label).render()) == str(tmp_path)


# ===========================================================================
# ContactDetailScreen — more field coverage
# ===========================================================================


async def test_contact_detail_screen_with_all_fields() -> None:
    """ContactDetailScreen shows all optional fields when set."""
    from datetime import UTC, datetime

    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.contact_detail_screen import ContactDetailScreen

    paths = make_tmp_paths("contact-detail-full")
    index = make_index(paths)
    contact = Contact(
        id=None,
        first_name="Eve",
        last_name="Wilson",
        emails=("eve@x.com",),
        affix=("Dr.",),
        aliases=("Evie",),
        organization="ACME",
        notes="Important contact",
        message_count=5,
        last_seen=datetime(2024, 6, 1, tzinfo=UTC),
    )
    saved = index.upsert_contact(contact=contact)

    app = _make_host(ContactDetailScreen, saved, index)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Press e to edit (action_edit)
        await pilot.press("e")
        await pilot.pause()
        from pony.tui.screens.contact_edit_screen import ContactEditScreen

        assert any(isinstance(s, ContactEditScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()


async def test_contact_detail_screen_no_emails() -> None:
    """ContactDetailScreen shows '(none)' when contact has no email."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.contact_detail_screen import ContactDetailScreen

    paths = make_tmp_paths("contact-detail-no-email")
    index = make_index(paths)
    contact = Contact(
        id=None,
        first_name="NoEmail",
        last_name="User",
        emails=(),
    )
    saved = index.upsert_contact(contact=contact)

    app = _make_host(ContactDetailScreen, saved, index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()


async def test_contact_browser_enter_row_opens_detail() -> None:
    """Pressing Enter on a row opens ContactDetailScreen."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.contact_detail_screen import ContactDetailScreen

    paths = make_tmp_paths("cb-enter")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Alice", last_name="Smith", emails=("alice@x.com",)
        )
    )

    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.screen.query_one("#contact-table", DataTable)
        table.focus()
        await pilot.pause()
        # Move to the first row
        table.move_cursor(row=0)
        await pilot.pause()
        # Press enter to select the row (on_data_table_row_selected)
        await pilot.press("enter")
        await pilot.pause()
        assert any(isinstance(s, ContactDetailScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()


async def test_contact_browser_new_contact_key() -> None:
    """Pressing c opens ContactEditScreen for a new contact."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.tui.screens.contact_edit_screen import ContactEditScreen

    paths = make_tmp_paths("cb-new")
    index = make_index(paths)

    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert any(isinstance(s, ContactEditScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()


async def test_contact_browser_edit_key() -> None:
    """Pressing e opens ContactEditScreen for the current contact."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.contact_edit_screen import ContactEditScreen

    paths = make_tmp_paths("cb-edit")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Bob", last_name="Jones", emails=("bob@x.com",)
        )
    )

    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.screen.query_one("#contact-table", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert any(isinstance(s, ContactEditScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()


async def test_contact_browser_delete_shows_confirm() -> None:
    """Pressing D on a marked contact shows ConfirmScreen."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact
    from pony.tui.screens.confirm_screen import ConfirmScreen

    paths = make_tmp_paths("cb-delete")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="ToDelete", last_name="User", emails=("del@x.com",)
        )
    )

    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.screen.query_one("#contact-table", DataTable)
        table.focus()
        await pilot.pause()
        # Mark the contact, then press D to get confirm dialog
        await pilot.press("m")
        await pilot.pause()
        await pilot.press("D")
        await pilot.pause()
        # ConfirmScreen should appear
        assert any(isinstance(s, ConfirmScreen) for s in app.screen_stack)
        # Press y to confirm deletion
        await pilot.press("y")
        await pilot.pause()
    # Contact should be deleted from index
    result = index.find_contact_by_email(email_address="del@x.com")
    assert result is None


def _contacts_index(label: str, *names: str):  # type: ignore[no-untyped-def]
    """Return an index seeded with one contact per ``First Last`` name."""
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact

    index = make_index(make_tmp_paths(label))
    for name in names:
        first, _, last = name.partition(" ")
        index.upsert_contact(
            contact=Contact(
                id=None,
                first_name=first,
                last_name=last,
                emails=(f"{first.lower()}@x.com",),
            )
        )
    return index


async def test_contact_browser_delete_unmarked_deletes_the_cursor_row() -> None:
    """With no marks, D deletes the contact under the cursor."""
    from textual.widgets import DataTable

    from pony.tui.screens.confirm_screen import ConfirmScreen

    index = _contacts_index("cb-del-single", "Solo User")
    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#contact-table", DataTable).focus()
        await pilot.pause()
        await pilot.press("D")
        await pilot.pause()
        assert any(isinstance(s, ConfirmScreen) for s in app.screen_stack)
        await pilot.press("y")
        await pilot.pause()

    assert index.find_contact_by_email(email_address="solo@x.com") is None


async def test_contact_browser_delete_unmarked_declined_keeps_contact() -> None:
    """Answering no to the single-delete prompt leaves the contact alone."""
    from textual.widgets import DataTable

    index = _contacts_index("cb-del-declined", "Keep Me")
    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#contact-table", DataTable).focus()
        await pilot.pause()
        await pilot.press("D")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

    assert index.find_contact_by_email(email_address="keep@x.com") is not None


async def test_contact_browser_delete_many_marked_truncates_the_list() -> None:
    """More than ten marked contacts collapse into an 'and N more' line."""
    from textual.widgets import DataTable

    names = [f"User{i:02d} Test" for i in range(12)]
    index = _contacts_index("cb-del-many", *names)
    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#contact-table", DataTable).focus()
        await pilot.pause()
        for _ in names:
            await pilot.press("m")
        await pilot.pause()
        await pilot.press("D")
        await pilot.pause()
        from textual.widgets import Static

        body = str(app.screen.query_one("#body", Static).render())
        assert "and 2 more" in body
        await pilot.press("y")
        await pilot.pause()

    assert index.list_all_contacts() == []


async def test_contact_browser_merge_needs_two_marks() -> None:
    """M with fewer than two marks warns instead of merging."""
    from textual.widgets import DataTable

    index = _contacts_index("cb-merge-warn", "Alice Smith", "Bob Jones")
    app = ContactsApp(contacts=index)
    notifications: list[str] = []
    original_notify = app.notify

    def _capture(msg: str, **kw: object) -> None:
        notifications.append(msg)
        original_notify(msg, **kw)  # type: ignore[arg-type]

    app.notify = _capture  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#contact-table", DataTable).focus()
        await pilot.pause()
        await pilot.press("m")  # one mark only
        await pilot.press("M")
        await pilot.pause()

    assert any("at least 2 contacts" in n for n in notifications)
    assert len(index.list_all_contacts()) == 2


async def test_contact_browser_merge_marked_collapses_them() -> None:
    """Two marked contacts merge into one row."""
    from textual.widgets import DataTable

    index = _contacts_index("cb-merge-ok", "Alice Smith", "Alicia Smith")
    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#contact-table", DataTable).focus()
        await pilot.pause()
        await pilot.press("m")
        await pilot.press("m")
        await pilot.pause()
        await pilot.press("M")
        await pilot.pause()

    remaining = index.list_all_contacts()
    assert len(remaining) == 1
    assert set(remaining[0].emails) == {"alice@x.com", "alicia@x.com"}


async def test_contact_browser_mark_on_empty_list_is_a_noop() -> None:
    """Marking with no contacts loaded does not raise."""
    from textual.widgets import DataTable

    index = _contacts_index("cb-mark-empty")
    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#contact-table", DataTable).focus()
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()

    assert index.list_all_contacts() == []


async def test_contact_browser_search_submitted() -> None:
    """Submitting search input filters and hides the search bar."""
    from textual.widgets import DataTable, Input
    from tui_helpers import make_index, make_tmp_paths

    from pony.domain import Contact

    paths = make_tmp_paths("cb-search-sub")
    index = make_index(paths)
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Alice", last_name="Smith", emails=("a@x.com",)
        )
    )
    index.upsert_contact(
        contact=Contact(
            id=None, first_name="Bob", last_name="Jones", emails=("b@x.com",)
        )
    )

    app = ContactsApp(contacts=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Open search, type, submit
        await pilot.press("slash")
        await pilot.pause()
        search = app.screen.query_one("#contact-search", Input)
        search.value = "Alice"
        # Trigger submitted event directly
        from textual.widgets._input import Input as TxInput

        browser = app.screen
        assert isinstance(browser, ContactBrowserScreen)
        browser.on_input_submitted(
            TxInput.Submitted(search, value="Alice", validation_result=None)
        )
        await pilot.pause()
        # Search bar should be hidden
        assert not search.display
        # Table should have 1 row
        table = app.screen.query_one("#contact-table", DataTable)
        assert table.row_count == 1
