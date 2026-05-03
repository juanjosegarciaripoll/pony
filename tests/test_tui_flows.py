"""End-to-end Textual TUI flow tests driven by Pilot.

Covers the highest-risk user paths through ``PonyApp`` and
``ComposeApp``.  Each flow is an ``async def`` driven by the Textual
``App.run_test()`` harness; SMTP, browser launching and external
editors are patched so no side effects escape the test process.

Every test constructs fresh repositories under ``tests/conftest.TMP_ROOT``
via ``tui_helpers.build_pony_app`` / ``build_compose_app`` — the shared
``atexit`` cleanup in ``conftest.py`` removes them on interpreter exit.
"""

from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import Mock
from uuid import uuid4

import pytest
from corpus import html_only, plain_text
from tui_helpers import (
    build_compose_app,
    build_pony_app,
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
    seed_message,
)

from pony.credentials import PlaintextCredentialsProvider
from pony.domain import (
    FolderRef,
    MessageFlag,
    MessageStatus,
)
from pony.sync import ImapSyncService, SyncPlan
from pony.tui.app import PonyApp
from pony.tui.screens.compose_screen import ComposeScreen
from pony.tui.screens.help_screen import HelpScreen
from pony.tui.widgets.folder_panel import FolderPanel
from pony.tui.widgets.message_list import MessageListPanel
from pony.tui.widgets.message_view import MessageViewPanel

# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _custom_plain(subject: str, body: str = "body content") -> bytes:
    """Distinct plain-text message with a unique Message-ID.

    ``corpus.plain_text`` is shared and emits one constant Message-ID; tests
    that seed multiple messages need distinct bodies and ids.
    """
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 11 Apr 2026 10:00:00 +0000"
    msg["Message-ID"] = f"<custom-{uuid4().hex}@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


async def _select_first_inbox(pilot: object) -> None:
    """Activate the initial INBOX cursor on a freshly-booted PonyApp."""
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Flow tests
# ---------------------------------------------------------------------------


async def test_f1_opens_and_dismisses_help() -> None:
    """F1 pushes HelpScreen; F1 again dismisses it.

    Regression guard for the app-level binding and the help screen's
    own F1→dismiss binding.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="f1",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        assert any(isinstance(s, HelpScreen) for s in app.screen_stack)
        await pilot.press("f1")
        await pilot.pause()
        assert not any(isinstance(s, HelpScreen) for s in app.screen_stack)


async def test_main_opens_message_shows_body() -> None:
    """Selecting a message routes through to MessageViewPanel."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    raw = _custom_plain("Tuesday meeting confirmed", body="Hi there")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="open",
        seed=[(folder, raw)],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        assert view.display is True
        from textual.widgets import Static

        text = str(view.query_one("#content", Static).render())
        assert "Tuesday meeting confirmed" in text


async def test_compose_send_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filling To/Subject/Body and pressing ctrl+s sends and files in Sent."""
    app, _cfg, _paths, _index, mirrors = build_compose_app(label="send-ok")
    # A Sent folder must exist in the mirror for the file-copy path to
    # resolve; without it the send still succeeds but no bytes are written.
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    send_mock = Mock()
    monkeypatch.setattr(
        "pony.tui.screens.compose_screen.smtp_send",
        send_mock,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        app.screen.query_one("#subject-input", Input).value = "Hello"
        app.screen.query_one("#body-area", TextArea).load_text("Body text")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    assert send_mock.call_count == 1
    kwargs = send_mock.call_args.kwargs
    assert kwargs["username"] == "acct"
    assert kwargs["msg"]["To"] == "bob@example.com"
    assert kwargs["msg"]["Subject"] == "Hello"

    sent_folder = FolderRef(account_name="acct", folder_name="Sent")
    keys = mirrors["acct"].list_messages(folder=sent_folder)
    assert len(keys) == 1


async def test_compose_send_requires_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting without a To address keeps the screen and skips SMTP."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="send-noto")
    send_mock = Mock()
    monkeypatch.setattr(
        "pony.tui.screens.compose_screen.smtp_send",
        send_mock,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#subject-input", Input).value = "Hi"
        app.screen.query_one("#body-area", TextArea).load_text("Body")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert any(isinstance(s, ComposeScreen) for s in app.screen_stack)

    assert send_mock.call_count == 0


async def test_open_in_browser_calls_webbrowser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``w`` on an HTML-only message hands a file URI to webbrowser."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="browser",
        seed=[(folder, html_only())],
    )
    open_mock = Mock()
    monkeypatch.setattr(
        "pony.tui.widgets.message_view.webbrowser.open",
        open_mock,
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")  # open the message
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()

    assert open_mock.call_count == 1
    uri = open_mock.call_args.args[0]
    assert uri.startswith("file://")
    assert uri.endswith(".html")


def _dated_message(subject: str, date: str, message_id: str) -> bytes:
    """Plain-text message with explicit Date and Message-ID headers."""
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id
    msg.set_content(f"{subject} body")
    return msg.as_bytes()


async def test_trash_flips_status() -> None:
    """Pressing ``d`` marks the highlighted row TRASHED, leaving peers alone."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    # Distinct Date headers keep list ordering deterministic — the panel
    # sorts ``received_at DESC``, so the newer row is row 0 and ``down``
    # lands on the older one.
    app, _cfg, _paths, index, mirrors = build_pony_app(label="trash")
    newer_ref = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=_dated_message(
            "newer",
            "Fri, 11 Apr 2026 12:00:00 +0000",
            "<newer@example.com>",
        ),
        message_id="<newer@example.com>",
    )
    older_ref = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=_dated_message(
            "older",
            "Fri, 11 Apr 2026 09:00:00 +0000",
            "<older@example.com>",
        ),
        message_id="<older@example.com>",
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        # _select_first_inbox ends with MessageViewPanel focused; bring
        # focus back to the list so "down" navigates the cursor.
        pilot.app.screen.query_one(MessageListPanel).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("D")
        await pilot.pause()

    newer_row = index.get_message(message_ref=newer_ref)
    older_row = index.get_message(message_ref=older_ref)
    assert newer_row is not None and older_row is not None
    assert newer_row.local_status == MessageStatus.ACTIVE
    assert older_row.local_status == MessageStatus.TRASHED


async def test_toggle_flagged() -> None:
    """Pressing ``!`` toggles FLAGGED on the cursor row."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, mirrors = build_pony_app(label="flag")
    ref = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<flag@example.com>",
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("exclamation_mark")
        await pilot.pause()
        row = index.get_message(message_ref=ref)
        assert row is not None
        assert MessageFlag.FLAGGED in row.local_flags
        await pilot.press("exclamation_mark")
        await pilot.pause()
        row = index.get_message(message_ref=ref)
        assert row is not None
        assert MessageFlag.FLAGGED not in row.local_flags


async def test_archive_moves_to_configured_folder() -> None:
    """Archiving relocates the mirror file and re-keys the index row."""
    paths = make_tmp_paths("archive")
    account = make_test_account(paths, archive_folder="Archive")
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    credentials = PlaintextCredentialsProvider(config)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    ref = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<arch@example.com>",
    )
    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("A")
        await pilot.pause()

    # Same-account archive keeps the row id; only the folder changes.
    archived = index.get_message(message_ref=ref)
    assert archived is not None
    assert archived.message_ref.folder_name == "Archive"
    assert archived.local_status == MessageStatus.PENDING_MOVE
    assert archived.source_folder == "INBOX"
    # Mirror file moved onto disk under .Archive/{cur,new}.
    archive_dir = account.mirror.path / ".Archive"
    files = list((archive_dir / "cur").glob("*")) + list(
        (archive_dir / "new").glob("*"),
    )
    assert len(files) == 1


async def test_archive_advances_view_to_remaining_message() -> None:
    """Archiving the open message reloads the view onto the cursor's new row.

    Regression for the bug where the cursor advanced after archive/trash
    but the view pane kept rendering the just-removed message.
    """
    from textual.widgets import Static

    paths = make_tmp_paths("archive-view")
    account = make_test_account(paths, archive_folder="Archive")
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    credentials = PlaintextCredentialsProvider(config)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    seed_message(
        index=index, mirror=mirrors["acct"], folder=folder,
        raw=_custom_plain("first-subject", body="first body"),
        message_id="<arch1@example.com>",
    )
    seed_message(
        index=index, mirror=mirrors["acct"], folder=folder,
        raw=_custom_plain("second-subject", body="second body"),
        message_id="<arch2@example.com>",
    )
    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        assert view.display is True
        before = str(view.query_one("#content", Static).render())
        opened_subject = (
            "first-subject" if "first-subject" in before else "second-subject"
        )
        await pilot.press("A")
        await pilot.pause()
        assert view.display is True
        after = str(view.query_one("#content", Static).render())
        assert opened_subject not in after
        # The surviving message should be on screen.
        survivor = (
            "second-subject"
            if opened_subject == "first-subject"
            else "first-subject"
        )
        assert survivor in after


async def test_copy_to_folder() -> None:
    """Copy via ``_copy_to_folder`` duplicates the index row and mirror file."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, mirrors = build_pony_app(label="copy")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")
    source_ref = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<src@example.com>",
    )

    drafts = FolderRef(account_name="acct", folder_name="Drafts")

    # Drive the copy directly — the picker-screen interaction is
    # covered by other Pilot tests and racy here on Windows.
    async with app.run_test():
        screen = app.screen
        msgs = [m for m in [index.get_message(message_ref=source_ref)] if m]
        # type: ignore[attr-defined]
        screen._copy_to_folder(msgs, folder, drafts)  # noqa: SLF001

    source_rows = list(index.list_folder_messages(folder=folder))
    drafts_rows = list(index.list_folder_messages(folder=drafts))
    assert len(source_rows) == 1
    assert len(drafts_rows) == 1
    # Same-account copy rewrites the Message-ID so the sync planner
    # does not mistake the duplicate for a move (per-row identity
    # makes that stricter than necessary, but the convention helps
    # IMAP threading on the server side).
    source_message_id = index.get_message(message_ref=source_ref)
    assert source_message_id is not None
    assert source_message_id.message_id == "<src@example.com>"
    assert drafts_rows[0].message_id != "<src@example.com>"
    # Mirror file written on the target side.
    drafts_dir = _cfg.accounts[0].mirror.path / ".Drafts"
    assert list((drafts_dir / "new").glob("*"))


async def test_search_dialog_returns_results() -> None:
    """`/` + query + Enter filters the list to matching rows."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="search",
        seed=[
            (folder, _custom_plain("alpha", body="alpha body")),
            (folder, _custom_plain("beta", body="beta body")),
            (folder, _custom_plain("gamma", body="gamma body")),
        ],
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("slash")
        await pilot.pause()
        for ch in "alpha":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        ml = app.screen.query_one(MessageListPanel)
        assert len(ml._summaries) == 1
        assert ml._summaries[0].subject == "alpha"


async def test_folder_tree_next_inbox() -> None:
    """`N` / `P` jump the folder cursor between per-account INBOX nodes."""
    paths = make_tmp_paths("jump")
    a1 = make_test_account(paths, name="one")
    a2 = make_test_account(paths, name="two")
    config = make_test_config(accounts=(a1, a2))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    # Give each INBOX at least one message so the mirror exposes the folder.
    for name in ("one", "two"):
        seed_message(
            index=index,
            mirror=mirrors[name],
            folder=FolderRef(account_name=name, folder_name="INBOX"),
            raw=plain_text(),
            message_id=f"<{name}@example.com>",
        )
    credentials = PlaintextCredentialsProvider(config)
    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        fp = app.screen.query_one(FolderPanel)
        first = fp.cursor_node
        assert first is not None
        assert first.data == FolderRef(account_name="one", folder_name="INBOX")
        # on_mount auto-selects the first INBOX, which shifts focus to the
        # message list; restore it so the FolderPanel bindings are active.
        fp.focus()
        await pilot.pause()
        await pilot.press("N")
        await pilot.pause()
        assert fp.cursor_node is not None
        assert fp.cursor_node.data == FolderRef(
            account_name="two",
            folder_name="INBOX",
        )
        await pilot.press("P")
        await pilot.pause()
        assert fp.cursor_node is not None
        assert fp.cursor_node.data == FolderRef(
            account_name="one",
            folder_name="INBOX",
        )


async def test_sync_nothing_to_sync_no_cancel_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When planning returns an empty plan, only 'Nothing to sync.' appears.

    Regression guard: a previous bug caused 'Sync cancelled.' to also fire
    when the plan was empty or the exec worker failed.
    """
    empty_plan = SyncPlan(accounts=())
    monkeypatch.setattr(ImapSyncService, "plan", lambda self, **kwargs: empty_plan)

    app, *_ = build_pony_app(label="nothing-to-sync")

    notifications: list[str] = []
    original_notify = app.notify

    def _capture(msg: str, **kw: object) -> None:
        notifications.append(msg)
        original_notify(msg, **kw)  # type: ignore[arg-type]

    app.notify = _capture  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

    assert "Nothing to sync." in notifications
    assert "Sync cancelled." not in notifications
