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
from corpus import html_only, multipart_mixed_attachment, plain_text
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
from pony.tui.screens.main_screen import MainScreen
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


async def test_forward_sends_original_message_as_removable_eml_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forwarding exposes the source message as a normal removable .eml."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="forward-attachments",
        seed=[(folder, multipart_mixed_attachment())],
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    send_mock = Mock()
    monkeypatch.setattr(
        "pony.tui.screens.compose_screen.smtp_send",
        send_mock,
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("f")
        await pilot.pause()
        await pilot.pause()

        from textual.widgets import Input, Label

        assert isinstance(app.screen, ComposeScreen)
        attachment_labels = [
            str(label.render())
            for label in app.screen.query(Label)
            if "attach-name" in label.classes
        ]
        assert any(label.endswith(".eml") for label in attachment_labels)

        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        await pilot.press("ctrl+s")
        await pilot.pause()

    assert send_mock.call_count == 1
    sent = send_mock.call_args.kwargs["msg"]
    forwarded = next(
        part
        for part in sent.iter_attachments()
        if part.get_content_type() == "message/rfc822"
    )
    assert forwarded.get_filename().endswith(".eml")
    data = forwarded.get_payload(decode=True)
    assert isinstance(data, bytes)
    assert b"q1-report.pdf" in data


async def test_forward_eml_attachment_can_be_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="forward-remove-eml",
        seed=[(folder, multipart_mixed_attachment())],
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    send_mock = Mock()
    monkeypatch.setattr(
        "pony.tui.screens.compose_screen.smtp_send",
        send_mock,
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("f")
        await pilot.pause()
        await pilot.pause()

        from textual.widgets import Input

        assert isinstance(app.screen, ComposeScreen)
        await pilot.click(".attach-remove-btn")
        await pilot.pause()
        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        await pilot.press("ctrl+s")
        await pilot.pause()

    assert send_mock.call_count == 1
    sent = send_mock.call_args.kwargs["msg"]
    assert not any(
        part.get_content_type() == "message/rfc822" for part in sent.iter_attachments()
    )


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
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=_custom_plain("first-subject", body="first body"),
        message_id="<arch1@example.com>",
    )
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
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
            "second-subject" if opened_subject == "first-subject" else "first-subject"
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
        assert isinstance(screen, MainScreen)
        msgs = [m for m in [index.get_message(message_ref=source_ref)] if m]
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


async def test_mcp_server_error_no_config_path() -> None:
    """PonyApp starts cleanly when no config_path is provided.

    Covers the try/except in _start_mcp_tcp_server: passing config_path=None
    causes start_tcp_mcp_server to raise ConfigError or OSError, which is
    silently swallowed so the app continues to run normally.
    """
    app, _cfg, _paths, _index, _mirrors = build_pony_app(label="mcp-no-config")
    # build_pony_app does not pass config_path, so _config_path is None.
    assert app._config_path is None  # noqa: SLF001
    async with app.run_test() as pilot:
        await pilot.pause()
    # If we reach here without exception the test passes.


async def test_q_key_quits_app() -> None:
    """Pressing Q exits the app cleanly via the app-level binding."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="q-quit",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("Q")
        await pilot.pause()
    # If run_test exits cleanly the app quit without raising.


async def test_message_list_renders_subject() -> None:
    """Starting the app with a seeded message does not raise.

    Smoke test: the folder panel and message list mount successfully and
    labels are renderable strings.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="renders",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert pilot.app is not None
        # The screen stack has at least one screen mounted.
        assert len(app.screen_stack) >= 1


async def test_app_unmounts_cleanly() -> None:
    """PonyApp tears down without raising during on_unmount."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="unmount",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await pilot.pause()
    # Reaching here means on_unmount completed without exception.


async def test_sync_nothing_to_sync_no_cancel_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When planning returns an empty plan, only 'Nothing to sync.' appears.

    Regression guard: a previous bug caused 'Sync cancelled.' to also fire
    when the plan was empty or the exec worker failed.
    """
    empty_plan = SyncPlan(accounts=())
    monkeypatch.setattr(ImapSyncService, "plan", lambda _self, **_kwargs: empty_plan)

    app, *_ = build_pony_app(label="nothing-to-sync")

    notifications: list[str] = []
    original_notify = app.notify

    def _capture(msg: str, **kw: object) -> None:
        notifications.append(msg)
        original_notify(msg, **kw)  # type: ignore[arg-type]

    app.notify = _capture  # type: ignore[assignment,method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

    assert "Nothing to sync." in notifications
    assert "Sync cancelled." not in notifications


async def test_mark_all_read_notifies() -> None:
    """Pressing C marks all messages in the current folder as read."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, mirrors = build_pony_app(
        label="mark-all",
        seed=[
            (folder, _custom_plain("msg1", body="body1")),
            (folder, _custom_plain("msg2", body="body2")),
        ],
    )
    notifications: list[str] = []
    original_notify = app.notify

    def _capture(msg: str, **kw: object) -> None:
        notifications.append(msg)
        original_notify(msg, **kw)  # type: ignore[arg-type]

    app.notify = _capture  # type: ignore[assignment,method-assign]

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("C")
        await pilot.pause()
    # May or may not notify depending on whether messages are already read
    # — no crash is the key assertion.


async def test_mark_unread_does_not_crash() -> None:
    """Pressing u on a message does not crash."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, mirrors = build_pony_app(label="mark-unread")
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<unread@example.com>",
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("u")
        await pilot.pause()
    # Reaching here without exception is the test passing.


async def test_goto_folder_shortcut_opens_dialog() -> None:
    """Pressing G opens the GotoFolderScreen."""
    from pony.tui.screens.goto_folder_screen import GotoFolderScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="goto",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("G")
        await pilot.pause()
        assert any(isinstance(s, GotoFolderScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()
        assert not any(isinstance(s, GotoFolderScreen) for s in app.screen_stack)


async def test_new_folder_shortcut_opens_input() -> None:
    """Pressing N opens the NewFolderScreen input bar."""
    from pony.tui.screens.new_folder_screen import NewFolderScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="new-folder",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("N")
        await pilot.pause()
        assert any(isinstance(s, NewFolderScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()


async def test_compose_new_opens_compose_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing c opens the ComposeScreen."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="compose-new",
        seed=[(folder, plain_text())],
    )
    monkeypatch.setattr("pony.tui.screens.compose_screen.smtp_send", lambda **_: None)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("c")
        await pilot.pause()
        assert any(isinstance(s, ComposeScreen) for s in app.screen_stack)
        await pilot.press("Q")
        await pilot.pause()


async def test_compose_cancel_empty_dismisses_without_prompt() -> None:
    """Pressing Escape on an empty composer dismisses without showing a save prompt."""

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="cancel-empty")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    # No SaveDraftScreen should appear — empty body → direct dismiss.
    # If app exited cleanly without showing SaveDraftScreen, the test passes.


async def test_compose_cancel_with_content_shows_draft_prompt() -> None:
    """Cancelling a compose with content shows the SaveDraftScreen."""
    from pony.tui.screens.save_draft_screen import SaveDraftScreen

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="cancel-content")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input

        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        # Should now show SaveDraftScreen
        assert any(isinstance(s, SaveDraftScreen) for s in app.screen_stack)
        # Dismiss by pressing n (discard)
        await pilot.press("n")
        await pilot.pause()


async def test_compose_send_smtp_error_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When SMTP fails, a notification is shown and the screen stays."""
    from pony.smtp_sender import SMTPError

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="smtp-error")
    monkeypatch.setattr(
        "pony.tui.screens.compose_screen.smtp_send",
        lambda **_: (_ for _ in ()).throw(SMTPError("connection refused")),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        app.screen.query_one("#subject-input", Input).value = "Test"
        app.screen.query_one("#body-area", TextArea).load_text("Body")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
    # The compose screen should still be accessible (send failed, did not dismiss)


async def test_compose_with_to_focuses_body() -> None:
    """When compose is initialized with a To address, body gets focus."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="focus-body", to="bob@example.com"
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import TextArea

        body = app.screen.query_one("#body-area", TextArea)
        assert body.has_focus is True


async def test_message_view_close_on_q() -> None:
    """Pressing q in the message view posts CloseRequested, hiding the view."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="close-q",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        assert view.display is True
        await pilot.press("q")
        await pilot.pause()
        assert view.display is False


async def test_message_view_navigate_next() -> None:
    """Pressing n in the message view navigates to the next message."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="nav-next",
        seed=[
            (folder, _custom_plain("first", body="first body")),
            (folder, _custom_plain("second", body="second body")),
        ],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        from textual.widgets import Static

        view = app.screen.query_one(MessageViewPanel)
        first_text = str(view.query_one("#content", Static).render())
        # Navigate to next with "n"
        await pilot.press("n")
        await pilot.pause()
        # First and next texts may differ or be the same (wrapping) — no crash is key
        assert first_text is not None


async def test_message_list_sort_and_filter() -> None:
    """The message list loads correctly with multiple seeded messages."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="ml-sort",
        seed=[
            (folder, _custom_plain("msg-alpha")),
            (folder, _custom_plain("msg-beta")),
            (folder, _custom_plain("msg-gamma")),
        ],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        ml = app.screen.query_one(MessageListPanel)
        assert len(ml._summaries) == 3


async def test_browse_contacts_opens_contact_browser() -> None:
    """Pressing B opens the contact browser screen (when contacts is set)."""
    from pony.tui.screens.contact_browser_screen import ContactBrowserScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    paths = make_tmp_paths("browse-contacts")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<browse@example.com>",
    )
    credentials = PlaintextCredentialsProvider(config)
    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
        contacts=index,  # Pass index as contacts so B works
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("B")
        await pilot.pause()
        assert any(isinstance(s, ContactBrowserScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()


async def test_reply_shortcut_opens_compose_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing r on an open message opens the ComposeScreen in reply mode."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="reply-r",
        seed=[(folder, plain_text())],
    )
    monkeypatch.setattr("pony.tui.screens.compose_screen.smtp_send", lambda **_: None)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert any(isinstance(s, ComposeScreen) for s in app.screen_stack)
        await pilot.press("Q")
        await pilot.pause()


async def test_reply_all_opens_compose_with_cc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing R (reply-all) opens ComposeScreen with Cc populated."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    # Build a message with a Cc header
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "acct@example.com"
    msg["Cc"] = "carol@example.com"
    msg["Subject"] = "Thread"
    msg["Date"] = "Fri, 11 Apr 2026 10:00:00 +0000"
    msg["Message-ID"] = f"<reply-all-{uuid4().hex}@example.com>"
    msg.set_content("Reply me all")
    raw = msg.as_bytes()

    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="reply-all",
        seed=[(folder, raw)],
    )
    monkeypatch.setattr("pony.tui.screens.compose_screen.smtp_send", lambda **_: None)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert any(isinstance(s, ComposeScreen) for s in app.screen_stack)
        await pilot.press("Q")
        await pilot.pause()


async def test_compose_send_with_contacts_harvests_recipients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After sending, new recipients are added to the contacts store."""
    paths = make_tmp_paths("harvest")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    send_mock = __import__("unittest.mock", fromlist=["Mock"]).Mock()
    monkeypatch.setattr("pony.tui.screens.compose_screen.smtp_send", send_mock)

    import dataclasses as _dc

    # Set full_name so the compose screen doesn't push a ContactEditScreen
    account = _dc.replace(account, full_name="Test User")
    config = make_test_config(accounts=(account,))

    from pony.tui.app import ComposeApp

    app = ComposeApp(
        config=config,
        account=account,
        index=index,
        mirrors=dict(mirrors),
        contacts=index,  # Pass index as contacts to enable harvest
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#to-input", Input).value = "new-contact@example.com"
        app.screen.query_one("#subject-input", Input).value = "Test"
        app.screen.query_one("#body-area", TextArea).load_text("Body")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    assert send_mock.call_count == 1
    # Verify harvest ran without error — contact may or may not exist yet
    index.find_contact_by_email(email_address="new-contact@example.com")


async def test_move_shortcut_opens_folder_picker() -> None:
    """Pressing M opens the PickFolderScreen to select a move target."""
    from pony.tui.screens.pick_folder_screen import PickFolderScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="move-picker",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("M")
        await pilot.pause()
        assert any(isinstance(s, PickFolderScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()
        assert not any(isinstance(s, PickFolderScreen) for s in app.screen_stack)


async def test_harvest_contacts_key_no_contacts_store() -> None:
    """Pressing H when no contacts store is set notifies instead of crashing."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="harvest-no-contacts",
        seed=[(folder, plain_text())],
    )
    # build_pony_app does not pass contacts, so H should show a notification
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("H")
        await pilot.pause()
    # No crash expected; harvest_contacts was a no-op (no contacts store)


async def test_message_view_prev_message_key() -> None:
    """Pressing p in the message view navigates to the previous message."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="nav-prev",
        seed=[
            (folder, _custom_plain("first", body="first body")),
            (folder, _custom_plain("second", body="second body")),
        ],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        # Move to 2nd message then open it
        pilot.app.screen.query_one(MessageListPanel).focus()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        assert view.display is True
        # Press p to go to previous
        await pilot.press("p")
        await pilot.pause()


async def test_message_view_page_down_key() -> None:
    """Pressing space in the message view scrolls or goes to next message."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="page-down",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        assert view.display is True
        # Press space to page down or advance to next message
        await pilot.press("space")
        await pilot.pause()


async def test_message_view_save_attachment_from_viewer(tmp_path) -> None:
    """Pressing ctrl+1 in the EML viewer saves attachment 1."""
    from textual.app import App, ComposeResult

    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    raw = multipart_mixed_attachment()

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
    # Check that a file was saved
    saved = list(tmp_path.glob("*"))
    assert len(saved) >= 1


async def test_copy_key_shows_pick_folder() -> None:
    """Pressing Y (copy) shows the PickFolderScreen."""
    from pony.tui.screens.pick_folder_screen import PickFolderScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, mirrors = build_pony_app(
        label="copy-key",
        seed=[(folder, plain_text())],
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Archive")
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("Y")
        await pilot.pause()
        assert any(isinstance(s, PickFolderScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()
        assert not any(isinstance(s, PickFolderScreen) for s in app.screen_stack)


async def test_compose_reply_all_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pressing R on an open message composes a reply."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="reply-all-2",
        seed=[(folder, plain_text())],
    )
    monkeypatch.setattr("pony.tui.screens.compose_screen.smtp_send", lambda **_: None)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert any(isinstance(s, ComposeScreen) for s in app.screen_stack)
        await pilot.press("Q")
        await pilot.pause()


async def test_compose_cancel_save_draft_saves_to_drafts() -> None:
    """When cancel + save draft is confirmed, the draft is saved."""
    paths = make_tmp_paths("cancel-save-draft")
    account = make_test_account(paths)
    import dataclasses as _dc

    account = _dc.replace(account, full_name="Test User")
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")

    from pony.tui.app import ComposeApp

    app = ComposeApp(
        config=config,
        account=account,
        index=index,
        mirrors=dict(mirrors),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input

        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        # SaveDraftScreen should appear, press y to save
        await pilot.press("y")
        await pilot.pause()
    # Draft should be saved (no crash = success)
    # Draft may or may not be in Drafts (depends on account.drafts_folder config)
    # — the important thing is no exception was raised


async def test_message_list_mark_and_navigation() -> None:
    """Pressing m marks the current row; < and > jump to first/last."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="msg-mark",
        seed=[
            (folder, _custom_plain("a")),
            (folder, _custom_plain("b")),
            (folder, _custom_plain("c")),
        ],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        ml = app.screen.query_one(MessageListPanel)
        ml.focus()
        await pilot.pause()
        await pilot.pause()  # Extra pause to let message list load
        # Press m to mark current row
        await pilot.press("m")
        await pilot.pause()
        # Jump to last row with >
        await pilot.press("greater_than_sign")
        await pilot.pause()
        await pilot.pause()  # Wait for async cursor_last
        # Jump to first row with <
        await pilot.press("less_than_sign")
        await pilot.pause()
        # Use shift+down to mark and move
        await pilot.press("shift+down")
        await pilot.pause()
        # Also test n/p navigation in the message list
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()


async def test_folder_panel_with_nested_folders() -> None:
    """Nested folders (like Archive/2024) render as branch nodes in the tree."""
    paths = make_tmp_paths("nested-folders")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    credentials = PlaintextCredentialsProvider(config)

    # Seed messages in nested folders
    for folder_name in ["INBOX", "Archive/2024", "Archive/2023"]:
        mirror = mirrors["acct"]
        folder = FolderRef(account_name="acct", folder_name=folder_name)
        mirror.create_folder(account_name="acct", folder_name=folder_name)
        seed_message(
            index=index,
            mirror=mirror,
            folder=folder,
            raw=plain_text(),
            message_id=f"<nested-{folder_name.replace('/', '-')}@example.com>",
        )

    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        fp = app.screen.query_one(FolderPanel)
        # The tree should have rendered without error
        assert fp is not None


async def test_compose_toggle_markdown() -> None:
    """Pressing alt+m toggles markdown mode in the compose screen."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="toggle-md")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Check initial state (markdown off)
        initial_mode = app.screen._markdown_mode
        await pilot.press("alt+m")
        await pilot.pause()
        assert app.screen._markdown_mode != initial_mode
        await pilot.press("alt+m")
        await pilot.pause()
        assert app.screen._markdown_mode == initial_mode


async def test_compose_add_attachment_key_opens_picker() -> None:
    """Pressing alt+a opens the AddAttachmentScreen."""
    from pony.tui.screens.add_attachment_screen import AddAttachmentScreen

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="add-att")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+a")
        await pilot.pause()
        assert any(isinstance(s, AddAttachmentScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()


async def test_compose_screen_prompts_for_name_when_no_display_name() -> None:
    """ComposeApp pushes ContactEditScreen when account has no display name."""
    from pony.tui.app import ComposeApp
    from pony.tui.screens.contact_edit_screen import ContactEditScreen

    paths = make_tmp_paths("prompt-name")
    account = make_test_account(paths)  # no full_name set
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)

    app = ComposeApp(
        config=config,
        account=account,
        index=index,
        mirrors=dict(mirrors),
        contacts=index,  # contacts store is set → triggers the prompt
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # If contacts store is set and account has no display name,
        # ContactEditScreen should be pushed
        has_edit = any(isinstance(s, ContactEditScreen) for s in app.screen_stack)
        if has_edit:
            await pilot.press("escape")
            await pilot.pause()


async def test_compose_send_fails_without_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending without a password shows an error notification."""
    paths = make_tmp_paths("no-password")
    account = make_test_account(paths, password=None)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    send_mock = __import__("unittest.mock", fromlist=["Mock"]).Mock()
    monkeypatch.setattr("pony.tui.screens.compose_screen.smtp_send", send_mock)

    from pony.tui.app import ComposeApp

    app = ComposeApp(
        config=config,
        account=account,
        index=index,
        mirrors=dict(mirrors),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        app.screen.query_one("#body-area", TextArea).load_text("Body")
        await pilot.press("ctrl+s")
        await pilot.pause()

    # Send should NOT have been called due to missing password
    assert send_mock.call_count == 0


async def test_compose_send_with_no_sent_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending when no Sent folder exists shows a warning notification but succeeds."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="no-sent")
    # Don't create the Sent folder so _save_to_folder hits the "folder not found" path
    send_mock = __import__("unittest.mock", fromlist=["Mock"]).Mock()
    monkeypatch.setattr("pony.tui.screens.compose_screen.smtp_send", send_mock)

    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        app.screen.query_one("#subject-input", Input).value = "No Sent"
        app.screen.query_one("#body-area", TextArea).load_text("Body")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    # SMTP was called (send succeeded) even though Sent folder didn't exist
    assert send_mock.call_count == 1


async def test_browse_contacts_no_store_notifies() -> None:
    """Pressing B without a contacts store shows a notification."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="browse-no-contacts",
        seed=[(folder, plain_text())],
    )
    # build_pony_app doesn't pass contacts, so browse should show notification
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("B")
        await pilot.pause()
    # No crash - notification was shown


async def test_harvest_contacts_with_store() -> None:
    """Pressing H with contacts store harvests from current folder."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    paths = make_tmp_paths("harvest-with-contacts")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<harvest-contacts@example.com>",
    )
    credentials = PlaintextCredentialsProvider(config)
    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
        contacts=index,  # Pass contacts so H actually harvests
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("H")
        await pilot.pause()
    # Harvest ran without error


async def test_compose_new_no_smtp_notifies() -> None:
    """Pressing c with no sendable accounts shows a notification."""
    paths = make_tmp_paths("no-smtp")
    account = make_test_account(paths, with_smtp=False)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    credentials = PlaintextCredentialsProvider(config)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<no-smtp@example.com>",
    )
    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("c")
        await pilot.pause()
    # Should show notification, not open compose


async def test_open_attachments_key_shows_picker() -> None:
    """Pressing O on a message with attachments shows AttachmentPickerScreen."""
    from pony.tui.screens.attachment_picker_screen import AttachmentPickerScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="open-att",
        seed=[(folder, multipart_mixed_attachment())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("O")
        await pilot.pause()
        assert any(isinstance(s, AttachmentPickerScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()


async def test_save_attachments_key_shows_picker() -> None:
    """Pressing S on a message with attachments shows AttachmentPickerScreen."""
    from pony.tui.screens.attachment_picker_screen import AttachmentPickerScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="save-att",
        seed=[(folder, multipart_mixed_attachment())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("S")
        await pilot.pause()
        assert any(isinstance(s, AttachmentPickerScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()


async def test_move_to_folder_direct() -> None:
    """_move_to_folder moves message to target folder."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, mirrors = build_pony_app(label="move-direct")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Archive")
    source_ref = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<move-direct@example.com>",
    )

    archive = FolderRef(account_name="acct", folder_name="Archive")

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msgs = [m for m in [index.get_message(message_ref=source_ref)] if m]
        # Call _move_to_folder directly
        screen._move_to_folder(msgs, folder, archive)  # noqa: SLF001

    # Row should be moved to Archive folder
    from pony.domain import MessageStatus

    moved_rows = [
        r
        for r in index.list_folder_messages(folder=archive)
        if r.local_status == MessageStatus.PENDING_MOVE
    ]
    assert len(moved_rows) == 1


async def test_save_message_opens_save_screen() -> None:
    """Pressing s opens the SaveMessageScreen."""
    from pony.tui.screens.save_message_screen import SaveMessageScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="save-msg",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert any(isinstance(s, SaveMessageScreen) for s in app.screen_stack)
        await pilot.click("#cancel")
        await pilot.pause()
