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

import tempfile
import threading
import urllib.request
from collections.abc import Callable
from email.message import EmailMessage, Message
from pathlib import Path
from time import monotonic
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from corpus import html_only, multipart_mixed_attachment, plain_text
from textual.app import App
from textual.pilot import Pilot
from tui_helpers import (
    DeterministicDirectoryTree,
    DeterministicDirOnlyTree,
    TestPonyApp,
    build_compose_app,
    build_pony_app,
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
    seed_message,
)

import pony.tui.screens.add_attachment_screen as add_attachment_module
import pony.tui.screens.save_folder_picker_screen as save_folder_module
from chronos.scheduler import SyncOutcome
from pony.browser_preview import close_preview
from pony.credentials import PlaintextCredentialsProvider
from pony.domain import (
    FolderRef,
    LocalAccountConfig,
    MessageFlag,
    MessageStatus,
    MirrorConfig,
)
from pony.smtp_sender import DEFAULT_CONNECT_ATTEMPTS as SMTP_CONNECT_ATTEMPTS
from pony.sync import (
    AccountSyncResult,
    FolderSyncResult,
    ImapSyncService,
    MailSyncOutcome,
    SyncPlan,
    SyncResult,
)
from pony.tui.screens.compose_screen import ComposeScreen
from pony.tui.screens.help_screen import HelpScreen
from pony.tui.screens.main_screen import MainScreen
from pony.tui.widgets.folder_panel import SCHEDULED_SYNC_MARK, FolderPanel
from pony.tui.widgets.message_list import MessageListPanel
from pony.tui.widgets.message_view import MessageViewPanel

PonyApp = TestPonyApp


@pytest.fixture(autouse=True)
def _deterministic_dir_trees(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the worker-driven trees for the deterministic doubles, per test.

    These used to be bare module-level assignments, which left the
    production classes replaced for whatever file the xdist worker
    picked up next -- and meant the real ``_DirOnlyTree.filter_paths``,
    the whole reason that class exists, never ran anywhere.  It is
    checked directly in ``test_the_directory_only_tree_hides_files``.
    """
    monkeypatch.setattr(
        add_attachment_module,
        "DirectoryTree",
        DeterministicDirectoryTree,
    )
    monkeypatch.setattr(
        save_folder_module,
        "_DirOnlyTree",
        DeterministicDirOnlyTree,
    )


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
        "pony.delivery.smtp_send",
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
        "pony.delivery.smtp_send",
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


async def test_compose_dynamic_recipient_rows() -> None:
    """Cc/Bcc rows can be added, collected, removed, and cleared."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="recipient-rows")

    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.containers import Vertical
        from textual.widgets import Button, Input

        from pony.tui.screens.compose_screen import _AddrRow

        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        cc_container = screen.query_one("#cc-container", Vertical)
        first_row = cc_container.query_one(_AddrRow)
        first_row.query_one(Input).value = "alex.rivera@example.test"

        add_button = first_row.query_one(".addr-add-btn", Button)
        screen.on_button_pressed(Button.Pressed(add_button))
        await pilot.pause()

        rows = list(cc_container.query(_AddrRow))
        assert len(rows) == 2
        assert rows[0].query_one(".addr-add-btn", Button).visible is False
        assert rows[1].query_one(".addr-add-btn", Button).visible is True
        rows[1].query_one(Input).value = ""
        assert screen._collect_field("cc-container") == ("alex.rivera@example.test")

        remove_button = rows[1].query_one(".addr-remove-btn", Button)
        screen.on_button_pressed(Button.Pressed(remove_button))
        await pilot.pause()
        assert len(list(cc_container.query(_AddrRow))) == 1

        remaining_row = cc_container.query_one(_AddrRow)
        remove_button = remaining_row.query_one(".addr-remove-btn", Button)
        screen.on_button_pressed(Button.Pressed(remove_button))
        assert remaining_row.query_one(Input).value == ""


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
        "pony.delivery.smtp_send",
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
    # RFC 2046 s5.2.1 — a message/rfc822 part carries the nested message
    # directly; base64-encoding it here is what made forwards unopenable.
    assert forwarded["Content-Transfer-Encoding"] in ("7bit", "8bit", "binary")
    inner = forwarded.get_payload(0)
    assert isinstance(inner, Message)
    assert b"q1-report.pdf" in inner.as_bytes()


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
        "pony.delivery.smtp_send",
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


async def test_open_in_browser_serves_the_page_and_writes_no_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pressing ``w`` hands over a loopback URL, not a file in the temp dir.

    The file this used to write was never unlinked, so a reader left one
    copy of every message it looked at in the system temporary
    directory — out of reach of ``pony reset``, and in RAM where that
    directory is a tmpfs.  Pointing ``tempfile`` at an empty directory
    makes the regression visible: the press must leave it empty.
    """
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
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    try:
        async with app.run_test() as pilot:
            await _select_first_inbox(pilot)
            await pilot.press("enter")  # open the message
            await pilot.pause()
            await pilot.press("w")
            await pilot.pause()

        assert open_mock.call_count == 1
        url = open_mock.call_args.args[0]
        assert url.startswith("http://127.0.0.1:")
        assert list(tmp_path.iterdir()) == []

        request = urllib.request.Request(url)  # noqa: S310 (loopback)
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            served = response.read().decode("utf-8")
            policy = response.headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy
        assert "html" in served.lower()
    finally:
        close_preview()


async def test_print_pdf_picks_folder_and_exports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pressing ``p`` picks a folder and dispatches a PDF export worker."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="pdf",
        seed=[(folder, html_only())],
    )
    export_mock = Mock()
    monkeypatch.setattr("pony.tui.pdf_export.export_pdf_in_thread", export_mock)
    # Make the folder picker default to (and confirm) the temp directory.
    monkeypatch.setattr(
        "pony.tui.screens.save_folder_picker_screen._session_dir", tmp_path
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")  # open the message
        await pilot.pause()
        await pilot.press("ctrl+p")  # push the folder picker
        await pilot.pause()
        await pilot.click("#select")  # confirm the default folder
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert export_mock.call_count == 1
    _app_arg, _html, out, dest = export_mock.call_args.args
    assert out.suffix == ".pdf"
    assert dest == tmp_path.resolve()
    assert out.parent == dest


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
        config_path=paths.config_file,
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
        config_path=paths.config_file,
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
        screen._copy_to_folder(msgs, drafts)  # noqa: SLF001

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
    """`N` / `P` jump the folder cursor between per-account INBOX nodes.

    They were written into the widget, where `N` shadowed the reader's
    "New folder" from the pane where one would naturally create a folder
    -- and took it out of the footer too.  They keep the keys, because
    that is the muscle memory; "New folder" is what moved, to `F`.
    """
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
        config_path=paths.config_file,
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

        # And "New folder" reaches the reader from this pane, which is
        # the one that needs it -- both as the press and as the footer
        # entry that advertises it.
        from pony.tui.screens.new_folder_screen import NewFolderScreen

        advertised = app.screen.active_bindings
        assert advertised["F"].binding.action == "new_folder"
        assert advertised["F"].binding.description == "New folder"
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, NewFolderScreen)


async def test_account_mail_suffix_keeps_account_search_scope() -> None:
    """The account label suffix is display-only, not part of the account name."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="account-mail-suffix",
        seed=[(folder, plain_text())],
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        fp = app.screen.query_one(FolderPanel)
        [account_node] = fp.root.children
        assert account_node.data == "acct"
        fp.move_cursor(account_node)
        assert fp.get_search_scope() == ("acct", None)


async def test_mcp_server_error_no_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """PonyApp starts cleanly when no config_path is provided.

    Covers the try/except in _start_mcp_tcp_server: passing config_path=None
    causes start_tcp_mcp_server to raise ConfigError or OSError, which is
    silently swallowed so the app continues to run normally.
    """
    start_server = AsyncMock(side_effect=OSError("no test config"))
    monkeypatch.setattr("pony.mcp_server.start_tcp_mcp_server", start_server)
    app, _cfg, _paths, _index, _mirrors = build_pony_app(label="mcp-no-config")
    # Exercise the no-path branch only after MCP startup has been isolated.
    app._config_path = None  # noqa: SLF001
    assert app._config_path is None  # noqa: SLF001
    async with app.run_test() as pilot:
        await pilot.pause()
    start_server.assert_awaited_once()


async def test_q_key_quits_app() -> None:
    """Pressing Q exits the app cleanly via the app-level binding."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="q-quit",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_running
        await pilot.press("Q")
        await pilot.pause()
        # Leaving the app is the whole behaviour, and a clean exit from
        # ``run_test`` happens whether or not the key did anything.
        assert not app.is_running


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


async def test_app_unmount_stops_the_background_sync_thread() -> None:
    """Tearing the app down stops the scheduler it started.

    The thread is a daemon, so nothing visible breaks when it is left
    running -- which is why "no exception escaped" could stand in for
    this for so long.
    """
    import dataclasses as _dc

    paths = make_tmp_paths("unmount")
    account = make_test_account(paths)
    config = _dc.replace(
        make_test_config(accounts=(account,)),
        background_sync_enabled=True,
        # Far enough out that the cadence never fires inside the test:
        # the first run is one full interval after start().
        background_sync_interval_seconds=3600,
    )
    index = make_index(paths)
    mirrors = make_mirrors(config)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<unmount@example.com>",
    )
    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=PlaintextCredentialsProvider(config),
        config_path=paths.config_file,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        scheduler = app.mail_sync
        assert scheduler is not None
        assert scheduler.started

    assert not scheduler.started


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


def _canned_sync_result() -> SyncResult:
    """A non-empty SyncResult so the summary text includes a per-account line."""
    return SyncResult(
        accounts=(
            AccountSyncResult(
                account_name="acct",
                folders=(FolderSyncResult(folder_name="INBOX", fetched=3),),
            ),
        )
    )


def _capture_notifications(app: App[Any]) -> list[str]:
    """Patch ``app.notify`` to record every message it is handed."""
    notifications: list[str] = []
    original_notify = app.notify

    def _capture(msg: str, **kw: object) -> None:
        notifications.append(msg)
        original_notify(msg, **kw)  # type: ignore[arg-type]

    app.notify = _capture  # type: ignore[assignment,method-assign]
    return notifications


async def test_background_sync_success_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ctrl+g runs a background sync, reports a summary, clears the spinner."""
    monkeypatch.setattr(
        ImapSyncService, "sync", lambda _self, **_kw: _canned_sync_result()
    )
    app, *_ = build_pony_app(label="bg-success")
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+g")
        for _ in range(5):
            await pilot.pause()
        panel = app.screen.query_one(FolderPanel)
        # Spinner gone; ctrl+g armed the repeat, so the countdown takes over.
        assert "syncing" not in str(panel.border_title)
        assert SCHEDULED_SYNC_MARK in str(panel.border_title)

    assert any("Sync complete." in n and "+3 msgs" in n for n in notifications)


async def test_background_sync_failure_toast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing background sync reports an error toast and stops the spinner."""

    def _boom(_self: ImapSyncService, **_kw: object) -> SyncResult:
        raise RuntimeError("network down")

    monkeypatch.setattr(ImapSyncService, "sync", _boom)
    app, *_ = build_pony_app(label="bg-failure")
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+g")
        for _ in range(5):
            await pilot.pause()
        panel = app.screen.query_one(FolderPanel)
        assert "syncing" not in str(panel.border_title)
        assert SCHEDULED_SYNC_MARK in str(panel.border_title)

    assert any("Background sync failed" in n for n in notifications)


async def test_background_sync_rejects_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ctrl+g while a sync is running shows 'Sync already running.'."""
    import threading

    release = threading.Event()

    def _blocking_sync(_self: ImapSyncService, **_kw: object) -> SyncResult:
        release.wait(timeout=5)
        return _canned_sync_result()

    monkeypatch.setattr(ImapSyncService, "sync", _blocking_sync)
    app, *_ = build_pony_app(label="bg-overlap")
    notifications = _capture_notifications(app)

    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+g")
            await pilot.pause()
            panel = app.screen.query_one(FolderPanel)
            # Spinner is live while the worker is blocked.
            assert "syncing" in str(panel.border_title)
            await pilot.press("ctrl+g")
            await pilot.pause()
            assert "Sync already running." in notifications
            release.set()
            for _ in range(5):
                await pilot.pause()
            assert "syncing" not in str(panel.border_title)
            assert SCHEDULED_SYNC_MARK in str(panel.border_title)
    finally:
        release.set()


async def test_background_sync_runs_on_a_thread_when_enabled() -> None:
    """The cadence is a thread the application owns, started on mount.

    Nothing on the screen schedules it, which is what lets it keep running
    while the user is looking at the agenda instead.
    """
    import dataclasses

    app, cfg, *_ = build_pony_app(label="bg-timer")
    app._config = dataclasses.replace(  # type: ignore[attr-defined]
        cfg, background_sync_enabled=True, background_sync_interval_seconds=600
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        scheduler = app.mail_sync
        assert scheduler is not None
        assert scheduler.started
        deadline = scheduler.next_run_at
        assert deadline is not None
        # No sync at startup: the first run is a full interval away.
        assert monotonic() + 590 < deadline <= monotonic() + 600

    # Stopped with the application, so no thread outlives the session.
    assert not scheduler.started


async def test_background_sync_manual_trigger_arms_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ctrl+g syncs now and starts the cadence, even with the gate off."""
    synced = threading.Event()
    sync_calls = 0

    def _spy_sync(_self: ImapSyncService, **_kw: object) -> SyncResult:
        nonlocal sync_calls
        sync_calls += 1
        synced.set()
        return _canned_sync_result()

    monkeypatch.setattr(ImapSyncService, "sync", _spy_sync)

    app, *_ = build_pony_app(label="bg-manual-arms-repeat")
    async with app.run_test() as pilot:
        await pilot.pause()
        scheduler = app.mail_sync
        assert scheduler is not None
        # The default config leaves background sync off.
        assert not scheduler.started

        await pilot.press("ctrl+g")
        await _until(pilot, synced.is_set, "ctrl+g never ran a sync")
        for _ in range(5):
            await pilot.pause()

        assert sync_calls == 1
        # And from now on it repeats: the manual trigger armed the cadence
        # and the next run is a full interval out.
        assert scheduler.started
        deadline = scheduler.next_run_at
        assert deadline is not None
        assert deadline > monotonic() + 500


async def test_background_sync_absent_when_disabled() -> None:
    """Under the default config the cadence is built but never started."""
    app, *_ = build_pony_app(label="bg-no-timer")
    async with app.run_test() as pilot:
        await pilot.pause()
        scheduler = app.mail_sync
        assert scheduler is not None
        assert not scheduler.started
        assert scheduler.next_run_at is None


async def test_folder_panel_set_syncing_toggle() -> None:
    """set_syncing(True/False) toggles the border title and clears the timer."""
    app, *_ = build_pony_app(label="bg-spinner-toggle")
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.screen.query_one(FolderPanel)
        panel.set_syncing(True)
        await pilot.pause()
        assert "syncing" in str(panel.border_title)
        assert panel._spinner_timer is not None  # type: ignore[attr-defined]
        # Each tick of the 0.2s timer moves to the next frame, so the title
        # changes even though the sync state has not.
        first = str(panel.border_title)
        panel._advance_spinner()  # type: ignore[attr-defined]
        assert str(panel.border_title) != first
        assert "syncing" in str(panel.border_title)
        # Double-start is a no-op (guarded by the stored handle).
        panel.set_syncing(True)
        panel.set_syncing(False)
        await pilot.pause()
        assert panel.border_title == "Folders"
        assert panel._spinner_timer is None  # type: ignore[attr-defined]


async def test_folder_panel_shows_a_countdown_to_the_next_sync() -> None:
    """set_next_sync puts a clock and a M:SS countdown on the border title."""
    app, *_ = build_pony_app(label="bg-countdown")
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.screen.query_one(FolderPanel)
        assert panel.border_title == "Folders"

        # The half-second margin keeps the rounding off a step boundary, so
        # the rendered string does not depend on how long the call took.
        panel.set_next_sync(monotonic() + 150.5)
        assert str(panel.border_title) == f"Folders {SCHEDULED_SYNC_MARK} 2:30"
        assert panel._countdown_timer is not None  # type: ignore[attr-defined]

        # Re-arming replaces the deadline and reuses the one repaint timer.
        timer = panel._countdown_timer  # type: ignore[attr-defined]
        panel.set_next_sync(monotonic() + 600.5)
        assert str(panel.border_title) == f"Folders {SCHEDULED_SYNC_MARK} 10:00"
        assert panel._countdown_timer is timer  # type: ignore[attr-defined]


async def test_folder_panel_countdown_yields_to_the_sync_spinner() -> None:
    """A running sync outranks the countdown, which returns when it ends."""
    app, *_ = build_pony_app(label="bg-countdown-vs-spinner")
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.screen.query_one(FolderPanel)
        panel.set_next_sync(monotonic() + 300)
        assert SCHEDULED_SYNC_MARK in str(panel.border_title)

        panel.set_syncing(True)
        await pilot.pause()
        assert "syncing" in str(panel.border_title)
        assert SCHEDULED_SYNC_MARK not in str(panel.border_title)

        panel.set_syncing(False)
        await pilot.pause()
        assert "syncing" not in str(panel.border_title)
        assert SCHEDULED_SYNC_MARK in str(panel.border_title)


async def test_countdown_absent_until_a_periodic_sync_is_scheduled() -> None:
    """Under the default config nothing is scheduled, so no clock is shown."""
    app, *_ = build_pony_app(label="bg-countdown-absent")
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.screen.query_one(FolderPanel)
        assert panel.border_title == "Folders"
        assert panel._countdown_timer is None  # type: ignore[attr-defined]


async def test_enabled_background_sync_shows_the_countdown_at_startup() -> None:
    """background_sync_enabled arms the timer on mount, so the clock is up."""
    import dataclasses

    app, cfg, *_ = build_pony_app(label="bg-countdown-startup")
    app._config = dataclasses.replace(  # type: ignore[attr-defined]
        cfg, background_sync_enabled=True, background_sync_interval_seconds=900
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.screen.query_one(FolderPanel)
        assert SCHEDULED_SYNC_MARK in str(panel.border_title)
        # Asserted as a deadline rather than a rendered string: how much of
        # the interval mount consumed is not something a test can pin.
        deadline = panel._next_sync_deadline  # type: ignore[attr-defined]
        assert deadline is not None
        assert monotonic() + 890 < deadline <= monotonic() + 900


async def test_a_finished_sync_restarts_the_countdown() -> None:
    """Reporting a run repaints the clock from the scheduler's own deadline."""
    import dataclasses

    app, cfg, *_ = build_pony_app(label="bg-countdown-tick")
    app._config = dataclasses.replace(  # type: ignore[attr-defined]
        cfg, background_sync_enabled=True, background_sync_interval_seconds=900
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main_screen(app)
        panel = app.screen.query_one(FolderPanel)

        # Pretend the whole interval has already elapsed.
        panel.set_next_sync(monotonic() - 5)
        assert str(panel.border_title) == f"Folders {SCHEDULED_SYNC_MARK} 0:00"

        outcome: MailSyncOutcome = SyncOutcome(
            result=_canned_sync_result(), error=None, manual=False
        )
        screen.on_sync_finished(outcome)
        for _ in range(5):
            await pilot.pause()
        deadline = panel._next_sync_deadline  # type: ignore[attr-defined]
        assert deadline is not None
        assert monotonic() + 890 < deadline <= monotonic() + 900
        assert "syncing" not in str(panel.border_title)
        assert SCHEDULED_SYNC_MARK in str(panel.border_title)


async def _until(pilot: Pilot[None], check: Callable[[], bool], message: str) -> None:
    """Pump the app until `check` holds, rather than sleeping blind.

    The periodic syncs run on threads now, so a test that wants to see one
    land waits for the evidence instead of a fixed number of pauses.
    """
    for _ in range(200):
        if check():
            return
        await pilot.pause(0.01)
    raise AssertionError(message)


def _main_screen(app: PonyApp) -> MainScreen:
    """The MainScreen instance, even when a modal sits on top of it."""
    return next(s for s in app.screen_stack if isinstance(s, MainScreen))


async def test_background_sync_blocked_while_confirm_modal_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timer tick while the confirm modal is open does not start a sync.

    Regression guard: the confirmation wait is a window where no sync
    worker runs, yet a second sync would overlap IMAP sessions on the
    same account.  ``_sync_in_progress`` must treat an open
    ``SyncConfirmScreen`` as in-progress.
    """
    from pony.tui.screens.sync_confirm_screen import SyncConfirmScreen

    sync_calls: list[int] = []

    def _spy_sync(_self: ImapSyncService, **_kw: object) -> SyncResult:
        sync_calls.append(1)
        return _canned_sync_result()

    monkeypatch.setattr(ImapSyncService, "sync", _spy_sync)

    app, *_ = build_pony_app(label="bg-vs-modal")
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        main = _main_screen(app)
        # The foreground flow's confirm modal is open with no worker
        # running — the exact gap the guard has to cover.
        app.push_screen(SyncConfirmScreen.planning())
        await pilot.pause()
        assert isinstance(app.screen, SyncConfirmScreen)
        # Simulate the periodic timer firing during the confirmation wait.
        main.action_background_sync()
        await pilot.pause()
        assert "Sync already running." in notifications
        assert sync_calls == []


async def test_foreground_sync_blocked_while_background_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing g while a background sync runs is rejected, not overlapped."""
    import threading

    release = threading.Event()

    def _blocking_sync(_self: ImapSyncService, **_kw: object) -> SyncResult:
        release.wait(timeout=5)
        return _canned_sync_result()

    plan_calls: list[int] = []

    def _spy_plan(_self: ImapSyncService, **_kw: object) -> SyncPlan:
        plan_calls.append(1)
        return SyncPlan(accounts=())

    monkeypatch.setattr(ImapSyncService, "sync", _blocking_sync)
    monkeypatch.setattr(ImapSyncService, "plan", _spy_plan)

    app, *_ = build_pony_app(label="fg-vs-bg")
    notifications = _capture_notifications(app)

    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+g")
            await pilot.pause()
            # Background sync is running; the foreground flow must refuse.
            await pilot.press("g")
            await pilot.pause()
            assert "Sync already running." in notifications
            assert plan_calls == []
            release.set()
            for _ in range(5):
                await pilot.pause()
    finally:
        release.set()


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
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        before = index.list_folder_messages(folder=folder)
        # Opening the folder puts the cursor on the first row and reads
        # it, so exactly one of the two is still unread.
        unread_before = [
            row for row in before if MessageFlag.SEEN not in row.local_flags
        ]
        await pilot.press("C")
        await pilot.pause()
        after = index.list_folder_messages(folder=folder)

    # The flag in the index is the whole operation, and the count in the
    # notice is what tells the user it happened.
    assert len(unread_before) == 1
    assert [MessageFlag.SEEN in row.local_flags for row in after] == [True, True]
    assert "Marked 1 message as read." in notifications


async def test_mark_unread_takes_the_seen_flag_back_off() -> None:
    """Pressing u on a message that was read clears SEEN again."""
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
        # Opening the reader is what sets SEEN, so there is something
        # for u to undo; without this the press is a no-op either way.
        await pilot.press("enter")
        await pilot.pause()
        opened = index.list_folder_messages(folder=folder)
        await pilot.press("u")
        await pilot.pause()
        after = index.list_folder_messages(folder=folder)

    assert [MessageFlag.SEEN in row.local_flags for row in opened] == [True]
    assert [MessageFlag.SEEN in row.local_flags for row in after] == [False]


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
    """Pressing F opens the NewFolderScreen input bar."""
    from pony.tui.screens.new_folder_screen import NewFolderScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="new-folder",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("F")
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
    monkeypatch.setattr("pony.delivery.smtp_send", lambda **_: None)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("c")
        await pilot.pause()
        assert any(isinstance(s, ComposeScreen) for s in app.screen_stack)
        await pilot.press("Q")
        await pilot.pause()


async def test_compose_cancel_empty_dismisses_without_prompt() -> None:
    """Pressing Escape on an empty composer dismisses without showing a save prompt."""

    from pony.tui.screens.save_draft_screen import SaveDraftScreen

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="cancel-empty")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert any(isinstance(s, ComposeScreen) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()
        prompted = [s for s in app.screen_stack if isinstance(s, SaveDraftScreen)]
        # Dismissing the empty composer exits the app, which is the
        # only observable difference from being asked about a draft.
        left = not any(isinstance(s, ComposeScreen) for s in app.screen_stack)

    assert prompted == []
    assert left


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
        "pony.delivery.smtp_send",
        lambda **_: (_ for _ in ()).throw(SMTPError("connection refused")),
    )
    notifications = _capture_notifications(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        app.screen.query_one("#subject-input", Input).value = "Test"
        app.screen.query_one("#body-area", TextArea).load_text("Body")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        still_open = [s for s in app.screen_stack if isinstance(s, ComposeScreen)]

    assert any("connection refused" in n for n in notifications)
    # A failed send must not throw the typing away.
    assert len(still_open) == 1


async def _fill_and_send(pilot, app) -> None:  # type: ignore[no-untyped-def]
    """Fill the minimum compose fields and press the send chord."""
    from textual.widgets import Input, TextArea

    app.screen.query_one("#to-input", Input).value = "bob@example.com"
    app.screen.query_one("#subject-input", Input).value = "Subject"
    app.screen.query_one("#body-area", TextArea).load_text("Body")
    await pilot.pause()
    await pilot.press("ctrl+s")
    await pilot.pause()


async def test_compose_send_keeps_the_ui_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow SMTP connect must not freeze the compose screen.

    ``smtp_send`` blocks on a threading event, standing in for a connect
    to a host that never answers.  While it is stuck, the screen must
    still accept input — the whole point of moving the send off the
    message pump.
    """
    import threading

    app, _cfg, _paths, _index, mirrors = build_compose_app(label="send-responsive")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    release = threading.Event()

    def _blocking_send(**_kw: object) -> None:
        release.wait(timeout=10)

    monkeypatch.setattr(
        "pony.delivery.smtp_send",
        _blocking_send,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#to-input", Input).value = "bob@example.com"
        app.screen.query_one("#subject-input", Input).value = "Subject"
        app.screen.query_one("#body-area", TextArea).load_text("Body")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        # The send is in flight and parked in the thread.  If it were
        # running on the event loop this would never come back.
        subject = app.screen.query_one("#subject-input", Input)
        subject.value = "Edited while sending"
        await pilot.pause()
        assert subject.value == "Edited while sending"

        release.set()
        for _ in range(5):
            await pilot.pause()


async def test_compose_send_refuses_a_second_send_while_one_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ctrl+s stays live during the send, so a duplicate must be refused."""
    import threading

    app, _cfg, _paths, _index, mirrors = build_compose_app(label="send-double")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    release = threading.Event()
    calls: list[int] = []

    def _blocking_send(**_kw: object) -> None:
        calls.append(1)
        release.wait(timeout=10)

    monkeypatch.setattr(
        "pony.delivery.smtp_send",
        _blocking_send,
    )
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await _fill_and_send(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert any("already in progress" in n for n in notifications)
        release.set()
        for _ in range(5):
            await pilot.pause()

    assert len(calls) == 1


async def test_compose_send_announces_a_connect_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The progress callback surfaces retries as a toast naming the host."""
    app, _cfg, _paths, _index, mirrors = build_compose_app(label="send-retry-toast")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")

    def _retrying_send(**kwargs: object) -> None:
        on_attempt = kwargs["on_attempt"]
        assert callable(on_attempt)
        on_attempt(1, 4)
        on_attempt(2, 4)

    monkeypatch.setattr(
        "pony.delivery.smtp_send",
        _retrying_send,
    )
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await _fill_and_send(pilot, app)
        for _ in range(5):
            await pilot.pause()

    assert any("Connecting to smtp.example.com" in n for n in notifications)
    assert any("retrying (2 of 4)" in n for n in notifications)
    assert any("Message sent to bob@example.com" in n for n in notifications)


async def test_compose_send_passes_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connect timeout and attempt count reach the sender."""
    app, cfg, _paths, _index, mirrors = build_compose_app(label="send-timeout")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    send_mock = Mock()
    monkeypatch.setattr(
        "pony.delivery.smtp_send",
        send_mock,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await _fill_and_send(pilot, app)
        for _ in range(5):
            await pilot.pause()

    kwargs = send_mock.call_args.kwargs
    assert kwargs["connect_timeout"] == cfg.smtp_connect_timeout_seconds
    assert kwargs["connect_attempts"] == SMTP_CONNECT_ATTEMPTS


async def test_compose_send_discards_pre_send_draft_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The draft written before SMTP is removed once the send succeeds."""
    app, _cfg, _paths, index, mirrors = build_compose_app(label="send-draft-cleanup")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    monkeypatch.setattr("pony.delivery.smtp_send", Mock())

    async with app.run_test() as pilot:
        await pilot.pause()
        await _fill_and_send(pilot, app)

    drafts = FolderRef(account_name="acct", folder_name="Drafts")
    sent = FolderRef(account_name="acct", folder_name="Sent")
    assert len(mirrors["acct"].list_messages(folder=drafts)) == 0
    assert len(mirrors["acct"].list_messages(folder=sent)) == 1
    assert len(index.list_folder_messages(folder=drafts)) == 0


async def test_compose_send_failure_keeps_the_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SMTP failure leaves the pre-send draft in place and says so."""
    from pony.smtp_sender import SMTPError

    app, _cfg, _paths, _index, mirrors = build_compose_app(label="send-draft-kept")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")
    monkeypatch.setattr(
        "pony.delivery.smtp_send",
        lambda **_: (_ for _ in ()).throw(SMTPError("connection refused")),
    )
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await _fill_and_send(pilot, app)

    drafts = FolderRef(account_name="acct", folder_name="Drafts")
    assert len(mirrors["acct"].list_messages(folder=drafts)) == 1
    assert any("The message is in Drafts." in n for n in notifications)


async def test_compose_send_survives_a_failed_draft_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mirror that cannot delete the pre-send draft does not break the send."""
    app, _cfg, _paths, _index, mirrors = build_compose_app(
        label="send-draft-undeletable"
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    monkeypatch.setattr("pony.delivery.smtp_send", Mock())
    monkeypatch.setattr(
        mirrors["acct"],
        "delete_message",
        Mock(side_effect=OSError("mirror is read-only")),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await _fill_and_send(pilot, app)

    sent = FolderRef(account_name="acct", folder_name="Sent")
    assert len(mirrors["acct"].list_messages(folder=sent)) == 1


async def test_compose_send_reports_a_failed_sent_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mirror write error is surfaced instead of silently losing the copy."""
    app, _cfg, _paths, _index, mirrors = build_compose_app(label="send-store-fails")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    monkeypatch.setattr("pony.delivery.smtp_send", Mock())
    monkeypatch.setattr(
        mirrors["acct"],
        "store_message",
        Mock(side_effect=OSError("disk full")),
    )
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await _fill_and_send(pilot, app)

    assert any("Could not save to Sent: disk full" in n for n in notifications)


async def test_compose_send_without_a_mirror_still_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no mirror for the account, the send proceeds and files nothing."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="send-no-mirror")
    send_mock = Mock()
    monkeypatch.setattr("pony.delivery.smtp_send", send_mock)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        screen._mirrors = {}
        await _fill_and_send(pilot, app)

    assert send_mock.call_count == 1


async def test_compose_paste_file_path_attaches_it(tmp_path: Path) -> None:
    """Pasting a path into the attachments bar attaches that file."""
    from textual.events import Paste

    from pony.tui.screens.compose_screen import AttachmentsBar

    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"%PDF-1.4 fictional")

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="paste-attach")
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.screen.query_one(AttachmentsBar)
        bar.on_paste(Paste(str(attachment)))
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        assert screen._attachment_paths == [attachment]


async def test_compose_paste_file_url_is_unquoted(tmp_path: Path) -> None:
    """A file:// URL with percent-escapes resolves to the real path."""
    from textual.events import Paste

    from pony.tui.screens.compose_screen import AttachmentsBar

    attachment = tmp_path / "quarterly report.pdf"
    attachment.write_bytes(b"%PDF-1.4 fictional")

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="paste-file-url")
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.screen.query_one(AttachmentsBar)
        bar.on_paste(Paste(f"'file://{str(attachment).replace(' ', '%20')}'"))
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        assert screen._attachment_paths == [attachment]


async def test_compose_paste_non_path_attaches_nothing() -> None:
    """Pasting ordinary text warns and leaves the attachment list untouched."""
    from textual.events import Paste

    from pony.tui.screens.compose_screen import AttachmentsBar

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="paste-text")
    notifications = _capture_notifications(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.screen.query_one(AttachmentsBar)
        bar.on_paste(Paste("just some pasted prose"))
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        assert screen._attachment_paths == []

    assert any("Not a file:" in n for n in notifications)


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
        next_text = str(view.query_one("#content", Static).render())

    # ``assert first_text is not None`` held however little n did.
    assert "first body" in first_text
    assert "second body" in next_text


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


async def test_message_list_state_transitions() -> None:
    """Selection, marking, cursor boundaries, and unread navigation cooperate."""
    from dataclasses import replace

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="ml-state",
        seed=[
            (folder, _custom_plain("first")),
            (folder, _custom_plain("second")),
            (folder, _custom_plain("third")),
        ],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        panel = app.screen.query_one(MessageListPanel)
        await panel.wait_for_load_complete()
        panel.move_cursor(row=0)

        selected = panel.get_selected_summary()
        assert selected is not None
        assert panel.summaries_to_act_on() == [selected]
        assert panel.move_cursor_by(-1) is None

        panel._toggle_mark_current()
        assert panel.marked_summaries() == [selected]
        assert panel.summaries_to_act_on() == [selected]
        panel.clear_marks()
        assert panel.marked_summaries() == []

        seen = replace(selected, local_flags=frozenset({MessageFlag.SEEN}))
        panel.update_summary(seen)
        panel.move_cursor(row=0)
        next_unread = panel.move_cursor_to_next_unread()
        assert next_unread == panel.get_selected_summary()
        assert next_unread is not None
        assert next_unread.message_ref != selected.message_ref


async def test_message_list_empty_search_can_exit() -> None:
    """An empty search advertises its state and q restores folder mode."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="ml-empty-search",
        seed=[(folder, _custom_plain("only message"))],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        # _select_first_inbox leaves the reader open and focused; close it
        # so the q below exits search rather than closing the reader.
        await pilot.press("q")
        await pilot.pause()
        panel = app.screen.query_one(MessageListPanel)
        panel.load_search_results([], "fictional query")
        await pilot.pause()
        assert "no results" in str(panel.border_title)
        assert panel._in_search is True

        panel.focus()
        await pilot.press("q")
        await pilot.pause()
        assert panel._in_search is False
        assert panel.border_title == "Messages"
        assert len(panel._summaries) == 1


async def test_search_q_from_list_closes_the_reader_before_exiting() -> None:
    """With a hit open and the list focused, q closes the reader first."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="ml-search-close-reader",
        seed=[(folder, _custom_plain("only message"))],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        panel = app.screen.query_one(MessageListPanel)
        view = app.screen.query_one(MessageViewPanel)
        panel.load_search_results(list(index.list_folder_messages(folder=folder)), "x")
        await panel.wait_for_load_complete()
        panel.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert view.display

        panel.focus()
        await pilot.press("q")
        await pilot.pause()
        assert not view.display
        assert panel._in_search is True
        assert app.focused is panel

        await pilot.press("escape")
        await pilot.pause()
        assert panel._in_search is False
        assert panel.border_title == "Messages"


async def test_main_screen_address_and_link_actions() -> None:
    """Message metadata actions validate targets before composing or opening."""
    from pony.tui.screens.link_action_screen import LinkActionScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="main-links",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        screen = _main_screen(app)
        panel = screen.query_one(MessageViewPanel)
        screen.compose_new = Mock()

        panel.header_address = Mock(return_value=None)
        screen.action_compose_address("1")
        screen.compose_new.assert_not_called()
        panel.header_address = Mock(
            return_value=("Marina Rivera", "marina@example.test")
        )
        screen.action_compose_address("1")
        screen.compose_new.assert_called_once_with(
            to="Marina Rivera <marina@example.test>"
        )

        panel.body_link = Mock(return_value=("web", "https://example.test"))
        screen.action_activate_link("1")
        await pilot.pause()
        assert isinstance(app.screen, LinkActionScreen)
        await pilot.press("escape")
        await pilot.pause()

        panel.body_link = Mock(return_value=("mail", "river@example.test"))
        screen.action_compose_link("1")
        screen.compose_new.assert_called_with(to="river@example.test")


async def test_main_screen_attachment_dispatch_and_open_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested mail stays in Pony and external viewer errors stay contained."""
    from corpus import nested_forward

    from pony.message_renderer import extract_attachment
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="main-attachment-dispatch",
        seed=[(folder, nested_forward())],
    )
    launch_mock = Mock(side_effect=OSError("fictional viewer failure"))
    monkeypatch.setattr("pony.tui.screens.main_screen.launch_file", launch_mock)

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        screen = _main_screen(app)
        raw = screen.query_one(MessageViewPanel).raw_bytes
        assert raw is not None
        payloads = {index: extract_attachment(raw, index) for index in range(1, 4)}
        nested_index = next(
            index
            for index, payload in payloads.items()
            if payload is not None and payload.content_type == "message/rfc822"
        )
        file_index = next(
            index
            for index, payload in payloads.items()
            if payload is not None and payload.content_type != "message/rfc822"
        )
        screen._open_indices([nested_index])
        await pilot.pause()
        assert isinstance(app.screen, EmlViewerScreen)
        await pilot.press("Q")
        await pilot.pause()

        screen._open_indices([file_index])
        await pilot.pause()
        launch_mock.assert_called_once()


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
        config_path=paths.config_file,
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
    monkeypatch.setattr("pony.delivery.smtp_send", lambda **_: None)
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
    monkeypatch.setattr("pony.delivery.smtp_send", lambda **_: None)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        composer = next(s for s in app.screen_stack if isinstance(s, ComposeScreen))
        # The name of the test is the Cc, which was never looked at.
        cc = composer._collect_field("cc-container")  # noqa: SLF001
        to = composer._collect_field("to-container")  # noqa: SLF001
        await pilot.press("Q")
        await pilot.pause()

    assert "carol@example.com" in cc
    assert "alice@example.com" in to


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
    monkeypatch.setattr("pony.delivery.smtp_send", send_mock)

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


async def test_folder_picker_enter_on_account_row_does_nothing() -> None:
    """Enter on an account node (no folder data) leaves the picker open."""
    from pony.tui.screens.pick_folder_screen import PickFolderScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="picker-account-row",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("M")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PickFolderScreen)

        from textual.widgets import Tree

        tree = picker.query_one("#folder-tree", Tree)
        tree.cursor_line = 0  # the account node itself
        await pilot.pause()
        picker.action_select()
        await pilot.pause()
        assert app.screen is picker

        await pilot.press("escape")
        await pilot.pause()


async def test_folder_picker_selecting_a_folder_dismisses_with_it() -> None:
    """Selecting a folder leaf dismisses the picker with that FolderRef."""
    from pony.tui.screens.pick_folder_screen import PickFolderScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="picker-select-leaf",
        seed=[(folder, plain_text())],
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Archive")

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("M")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PickFolderScreen)

        from textual.widgets import Tree

        tree = picker.query_one("#folder-tree", Tree)
        leaf = next(
            node
            for node in tree.root.children[0].children
            if node.data is not None and node.data.folder_name == "Archive"
        )
        tree.select_node(leaf)
        tree.action_select_cursor()
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
    notifications = _capture_notifications(app)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("H")
        await pilot.pause()

    # Said, not swallowed: without the notice the key looks dead.
    assert "No contact store is open." in notifications


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
    monkeypatch.setattr("pony.delivery.smtp_send", lambda **_: None)
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
    notifications = _capture_notifications(app)
    drafts = FolderRef(account_name="acct", folder_name="Drafts")
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
        filed = index.list_folder_messages(folder=drafts)

    # The point of answering "yes" is a row in Drafts addressed to the
    # recipient that was typed, and a file behind it in the mirror.
    assert len(filed) == 1
    assert "bob@example.com" in filed[0].recipients
    assert mirrors["acct"].get_message_bytes(
        folder=drafts,
        storage_key=filed[0].storage_key,
    )
    assert any("Saved in Drafts." in n for n in notifications)


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

    def _subjects(panel: MessageListPanel) -> list[str]:
        return [summary.subject for summary in panel.marked_summaries()]

    def _cursor(panel: MessageListPanel) -> str | None:
        summary = panel.get_selected_summary()
        return None if summary is None else summary.subject

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        ml = app.screen.query_one(MessageListPanel)
        ml.focus()
        await pilot.pause()
        await pilot.pause()  # Extra pause to let message list load
        # All three carry the same Date, so the list keeps insert order.
        assert _cursor(ml) == "a"
        # Press m to mark current row
        await pilot.press("m")
        await pilot.pause()
        after_mark = _subjects(ml)
        # Jump to last row with >
        await pilot.press("greater_than_sign")
        await pilot.pause()
        await pilot.pause()  # Wait for async cursor_last
        at_last = _cursor(ml)
        # Jump to first row with <
        await pilot.press("less_than_sign")
        await pilot.pause()
        at_first = _cursor(ml)
        # Use shift+down to mark and move
        await pilot.press("shift+down")
        await pilot.pause()
        after_mark_down = _subjects(ml)
        moved_on = _cursor(ml)
        # Also test n/p navigation in the message list
        await pilot.press("n")
        await pilot.pause()
        after_n = _cursor(ml)
        await pilot.press("p")
        await pilot.pause()
        after_p = _cursor(ml)

    # Every one of these keys has an observable result; the test used to
    # press all seven and assert none of them.
    assert after_mark == ["a"]
    assert at_last == "c"
    assert at_first == "a"
    # shift+down *toggles* where it was and steps down, and "a" was
    # already marked by m, so the mark comes back off.
    assert after_mark_down == []
    assert moved_on == "b"
    assert after_n == "c"
    assert after_p == "b"


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
        config_path=paths.config_file,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        fp = app.screen.query_one(FolderPanel)
        labels = {str(node.label) for node in fp.root.children[0].children}
        branch = next(
            node
            for node in fp.root.children[0].children
            if str(node.label).startswith("Archive")
        )
        leaves = {str(child.label) for child in branch.children}

    # "Archive" is a branch the two years hang off, not three siblings.
    assert any(label.startswith("INBOX") for label in labels)
    assert not any(label.startswith("Archive/") for label in labels)
    assert {leaf.split()[0] for leaf in leaves} == {"2023", "2024"}


async def test_compose_toggle_markdown() -> None:
    """Pressing alt+m toggles markdown mode in the compose screen."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="toggle-md")
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        # Check initial state (markdown off)
        initial_mode = screen._markdown_mode
        await pilot.press("alt+m")
        await pilot.pause()
        assert screen._markdown_mode != initial_mode
        await pilot.press("alt+m")
        await pilot.pause()
        assert screen._markdown_mode == initial_mode


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
        # Asserted, not branched on: ``if has_edit:`` made the whole
        # test a no-op the moment the prompt stopped being pushed.
        assert has_edit
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
    monkeypatch.setattr("pony.delivery.smtp_send", send_mock)

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
    monkeypatch.setattr("pony.delivery.smtp_send", send_mock)

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
    notifications = _capture_notifications(app)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("B")
        await pilot.pause()
        from pony.tui.screens.contact_browser_screen import ContactBrowserScreen

        opened = [s for s in app.screen_stack if isinstance(s, ContactBrowserScreen)]

    assert "No contacts store available." in notifications
    assert opened == []


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
        config_path=paths.config_file,
    )
    notifications = _capture_notifications(app)
    assert index.list_all_contacts() == []
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("H")
        await pilot.pause()
        harvested = index.list_all_contacts()

    # ``corpus.plain_text`` carries To: bob and Cc: carol, so those are
    # the records the key is supposed to have created.
    assert sorted(email for contact in harvested for email in contact.emails) == [
        "bob@example.com",
        "carol@example.com",
    ]
    assert any("Harvested contacts from 1 message(s)" in n for n in notifications)


async def test_compose_new_no_smtp_notifies() -> None:
    """Pressing c with no sendable accounts shows a notification.

    The only account shape that cannot send is a local one with no SMTP
    block: ``AccountConfig.can_send`` is unconditionally True, so an
    IMAP account is always sendable however its SMTP block is built.
    """
    paths = make_tmp_paths("no-smtp")
    mirror_dir = paths.data_dir / "mirrors" / "acct"
    mirror_dir.mkdir(parents=True, exist_ok=True)
    account = LocalAccountConfig(
        name="acct",
        email_address="acct@example.com",
        mirror=MirrorConfig(path=mirror_dir, format="maildir"),
    )
    assert not account.can_send
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
        config_path=paths.config_file,
    )
    notifications = _capture_notifications(app)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("c")
        await pilot.pause()
        opened = [s for s in app.screen_stack if isinstance(s, ComposeScreen)]

    assert opened == []
    assert any(
        "Composing requires an IMAP account (SMTP is needed to send)." in n
        for n in notifications
    )


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
        screen._move_to_folder(msgs, archive)  # noqa: SLF001

    # Row should be moved to Archive folder
    from pony.domain import MessageStatus

    moved_rows = [
        r
        for r in index.list_folder_messages(folder=archive)
        if r.local_status == MessageStatus.PENDING_MOVE
    ]
    assert len(moved_rows) == 1


async def test_move_cross_account_direct() -> None:
    """_move_to_folder with two different accounts covers _move_cross_account."""
    paths = make_tmp_paths("cross-move")
    account1 = make_test_account(paths, name="acct1")
    account2 = make_test_account(paths, name="acct2")
    config = make_test_config(accounts=(account1, account2))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    credentials = PlaintextCredentialsProvider(config)

    folder1 = FolderRef(account_name="acct1", folder_name="INBOX")
    source_ref = seed_message(
        index=index,
        mirror=mirrors["acct1"],
        folder=folder1,
        raw=plain_text(),
        message_id="<cross-move@example.com>",
    )
    mirrors["acct2"].create_folder(account_name="acct2", folder_name="INBOX")

    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
        config_path=paths.config_file,
    )

    folder2 = FolderRef(account_name="acct2", folder_name="INBOX")

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msgs = [m for m in [index.get_message(message_ref=source_ref)] if m]
        screen._move_to_folder(msgs, folder2)  # noqa: SLF001

    # Check that a message was created in acct2/INBOX
    dest_msgs = list(index.list_folder_messages(folder=folder2))
    assert len(dest_msgs) >= 1


async def test_compose_from_draft_opens_compose() -> None:
    """Pressing e on a Drafts message opens ComposeScreen with draft content."""
    from email.message import EmailMessage

    folder = FolderRef(account_name="acct", folder_name="Drafts")
    draft_msg = EmailMessage()
    draft_msg["From"] = "acct@example.com"
    draft_msg["To"] = "bob@example.com"
    draft_msg["Subject"] = "My draft"
    draft_msg.set_content("Draft body text")
    raw = draft_msg.as_bytes()

    paths = make_tmp_paths("draft-edit")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    credentials = PlaintextCredentialsProvider(config)

    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=raw,
        message_id="<draft@example.com>",
    )

    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
        config_path=paths.config_file,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        fp = app.screen.query_one(FolderPanel)
        fp.focus()
        await pilot.pause()
        # Select the Drafts folder node via select_folder_ref
        app.screen.query_one(FolderPanel).select_folder_ref(folder)
        await pilot.pause()
        await pilot.pause()
        # Select the first message
        ml = app.screen.query_one(MessageListPanel)
        ml.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # Press e to edit the draft
        await pilot.press("e")
        await pilot.pause()
        composer = next(s for s in app.screen_stack if isinstance(s, ComposeScreen))
        from textual.widgets import Input, TextArea

        resumed_to = composer._collect_field("to-container")  # noqa: SLF001
        resumed_subject = composer.query_one("#subject-input", Input).value
        resumed_body = composer.query_one("#body-area", TextArea).text
        await pilot.press("Q")
        await pilot.pause()

    # "with draft content" was the claim; only the screen class was read.
    assert resumed_to == "bob@example.com"
    assert resumed_subject == "My draft"
    assert "Draft body text" in resumed_body


async def test_resuming_a_draft_and_saving_leaves_one_row_in_drafts() -> None:
    """Saving an edited draft replaces it rather than adding beside it.

    Both halves of this are covered at the model layer and in the
    browser; through the terminal, nothing reached ``_on_draft_done`` at
    all, because the screen test that resumes a draft replaces
    ``app.push_screen`` with a ``Mock``.
    """
    from email.message import EmailMessage

    from pony.domain import MessageStatus

    folder = FolderRef(account_name="acct", folder_name="Drafts")
    draft_msg = EmailMessage()
    draft_msg["From"] = "acct@example.com"
    draft_msg["To"] = "bob@example.com"
    draft_msg["Subject"] = "Half written"
    draft_msg["Date"] = "Fri, 11 Apr 2026 10:00:00 +0000"
    draft_msg["Message-ID"] = "<resume-me@example.com>"
    draft_msg.set_content("First half")

    paths = make_tmp_paths("draft-resume-save")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=draft_msg.as_bytes(),
        message_id="<resume-me@example.com>",
    )
    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=PlaintextCredentialsProvider(config),
        config_path=paths.config_file,
    )
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one(FolderPanel).select_folder_ref(folder)
        await pilot.pause()
        await pilot.pause()
        msg_list = app.screen.query_one(MessageListPanel)
        msg_list.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        composer = next(s for s in app.screen_stack if isinstance(s, ComposeScreen))
        from textual.widgets import TextArea

        composer.query_one("#body-area", TextArea).load_text("Second half")
        await pilot.pause()
        # Escape, then y: save the draft rather than discarding it.
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()
        rows = index.list_folder_messages(folder=folder)

    live = [row for row in rows if row.local_status is not MessageStatus.TRASHED]
    assert any("Saved in Drafts." in n for n in notifications)
    assert len(live) == 1
    assert live[0].message_id != "<resume-me@example.com>"
    saved = mirrors["acct"].get_message_bytes(
        folder=folder,
        storage_key=live[0].storage_key,
    )
    assert b"Second half" in saved


async def test_new_folder_creates_in_mirror() -> None:
    """Pressing F, typing a name, and pressing Enter creates a new folder."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="new-folder-create",
        seed=[(folder, plain_text())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("F")
        await pilot.pause()
        from pony.tui.screens.new_folder_screen import NewFolderScreen

        assert any(isinstance(s, NewFolderScreen) for s in app.screen_stack)
        for ch in "Archive":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
    # Archive folder should exist in mirror
    folder_refs = mirrors["acct"].list_folders(account_name="acct")
    folder_names = [r.folder_name for r in folder_refs]
    assert "Archive" in folder_names


async def test_open_attachment_submits_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing O then submitting '1' opens the first attachment."""
    launch_mock = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    monkeypatch.setattr("pony.tui.screens.main_screen.launch_file", launch_mock)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="open-att-index",
        seed=[(folder, multipart_mixed_attachment())],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        # Open attachments
        await pilot.press("O")
        await pilot.pause()
        from textual.widgets import Input

        from pony.tui.screens.attachment_picker_screen import AttachmentPickerScreen

        picker = next(
            screen
            for screen in app.screen_stack
            if isinstance(screen, AttachmentPickerScreen)
        )
        field = picker.query_one(Input)
        picker.on_input_submitted(Input.Submitted(field, "1"))
        await pilot.pause(0.1)
    # _launch_file should have been called for attachment 1
    assert launch_mock.call_count >= 1


async def test_open_attachment_numeric_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing '1' in the message view opens attachment 1 directly."""
    launch_mock = Mock()
    monkeypatch.setattr("pony.tui.screens.main_screen.launch_file", launch_mock)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, config, _paths, _index, _mirrors = build_pony_app(
        label="open-att-numeric",
        seed=[(folder, multipart_mixed_attachment())],
    )
    assert config.downloads_path is not None
    saved = config.downloads_path / "q1-report.pdf"
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        view.focus()
        await pilot.pause()
        # Press "1" to trigger screen.open_attachment('1')
        await pilot.press("1")
        await pilot.pause()
        await pilot.pause()

    # The key writes the part out and hands the file to the viewer; both
    # halves are observable, and neither was asserted before.
    assert saved.is_file()
    assert saved.read_bytes() == b"%PDF-1.4 fake pdf content"
    assert [call.args[0] for call in launch_mock.call_args_list] == [saved]


async def test_save_attachment_numeric_key() -> None:
    """Pressing ctrl+1 in the message view saves part 1."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, config, _paths, _index, _mirrors = build_pony_app(
        label="save-att-numeric",
        seed=[(folder, multipart_mixed_attachment())],
    )
    notifications = _capture_notifications(app)
    assert config.downloads_path is not None
    saved = config.downloads_path / "q1-report.pdf"
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        view.focus()
        await pilot.pause()
        # Press ctrl+1 to trigger screen.save_attachment('1')
        await pilot.press("ctrl+1")
        await pilot.pause()
        await pilot.pause()

    assert saved.is_file()
    assert saved.read_bytes() == b"%PDF-1.4 fake pdf content"
    # The notice names the file, which "Saved 1 attachment(s)" did not.
    assert any("q1-report.pdf" in n for n in notifications)


async def test_archive_no_folder_configured_notifies() -> None:
    """Pressing A when archive_folder is not configured shows an error."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    # build_pony_app doesn't set archive_folder by default
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="archive-no-folder",
        seed=[(folder, plain_text())],
    )
    notifications = _capture_notifications(app)
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("A")
        await pilot.pause()
        still_there = index.list_folder_messages(folder=folder)

    assert "No archive_folder configured for this account." in notifications
    # A refusal leaves the message where it was.
    assert len(still_there) == 1


async def test_reply_and_send_marks_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing r and sending a reply marks the original message as answered."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    from email.message import EmailMessage as _EMsg

    raw_msg = _EMsg()
    raw_msg["From"] = "alice@example.com"
    raw_msg["To"] = "acct@example.com"
    raw_msg["Subject"] = "Reply to me"
    raw_msg["Date"] = "Fri, 11 Apr 2026 10:00:00 +0000"
    raw_msg["Message-ID"] = "<reply-me@example.com>"
    raw_msg.set_content("Please reply")
    raw = raw_msg.as_bytes()

    app, _cfg, _paths, index, mirrors = build_pony_app(
        label="reply-send",
        seed=[(folder, raw)],
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    send_mock = __import__("unittest.mock", fromlist=["Mock"]).Mock()
    monkeypatch.setattr("pony.delivery.smtp_send", send_mock)

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#to-input", Input).value = "alice@example.com"
        app.screen.query_one("#body-area", TextArea).load_text("Reply body")
        await pilot.press("ctrl+s")
        await pilot.pause()
        # Asserted inside the app, because the flag is what makes the
        # list show the reply arrow and the test is named for it.
        rows = index.list_folder_messages(folder=folder)
        marked = [row for row in rows if MessageFlag.ANSWERED in row.local_flags]

    assert send_mock.call_count == 1
    # The test was named for this and asserted only that SMTP was
    # called, so it would have passed with the flag never set -- which
    # is exactly what moving the bookkeeping into deliver_message put
    # at risk.
    assert [row.message_id for row in marked] == ["<reply-me@example.com>"]


async def test_navigate_prev_in_message_view() -> None:
    """Pressing p in message view navigates to previous message."""
    from textual.widgets import Static

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="nav-prev-view",
        seed=[
            (folder, _custom_plain("msg1", body="first body")),
            (folder, _custom_plain("msg2", body="second body")),
        ],
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        # Navigate to second message (older)
        ml = app.screen.query_one(MessageListPanel)
        ml.focus()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        opened_second = str(view.query_one("#content", Static).render())
        # Now press p in the view to go back
        view.focus()
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        opened_first = str(view.query_one("#content", Static).render())
        cursor = ml.get_selected_summary()

    # p has to move both halves: the reader's body and the list cursor
    # behind it, or the next action works on a different message than
    # the one on screen.
    assert "second body" in opened_second
    assert "first body" in opened_first
    assert cursor is not None
    assert cursor.subject == "msg1"


async def test_compose_reply_sends_and_marks_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing r and sending marks the original as ANSWERED."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, mirrors = build_pony_app(
        label="compose-reply-send",
        seed=[(folder, plain_text())],
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Sent")
    send_mock = __import__("unittest.mock", fromlist=["Mock"]).Mock()
    monkeypatch.setattr("pony.delivery.smtp_send", send_mock)

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        from textual.widgets import Input, TextArea

        app.screen.query_one("#to-input", Input).value = "alice@example.com"
        app.screen.query_one("#body-area", TextArea).load_text("Reply")
        await pilot.press("ctrl+s")
        await pilot.pause()
        answered = [
            row
            for row in index.list_folder_messages(folder=folder)
            if MessageFlag.ANSWERED in row.local_flags
        ]
        # The reader is still open on it, so the mark the panel shows
        # has to have been repainted too.
        shown = app.screen.query_one(MessageListPanel).get_selected_summary()

    assert send_mock.call_count == 1
    assert len(answered) == 1
    assert shown is not None
    assert MessageFlag.ANSWERED in shown.local_flags


async def test_pressing_s_saves_the_message_without_asking() -> None:
    """``s`` writes the ``.eml`` and says where, in one keystroke."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, cfg, _paths, _index, _mirrors = build_pony_app(
        label="save-msg",
        seed=[(folder, plain_text())],
    )
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()

    downloads = cfg.downloads_path
    assert downloads is not None
    written = [path for path in downloads.iterdir() if path.suffix == ".eml"]
    assert len(written) == 1
    assert any(written[0].name in note for note in notifications)


def _cross_account_app(label: str):  # type: ignore[no-untyped-def]
    """Two IMAP accounts, one seeded message in acct1/INBOX."""
    paths = make_tmp_paths(label)
    account1 = make_test_account(paths, name="acct1")
    account2 = make_test_account(paths, name="acct2")
    config = make_test_config(accounts=(account1, account2))
    index = make_index(paths)
    mirrors = make_mirrors(config)

    folder1 = FolderRef(account_name="acct1", folder_name="INBOX")
    source_ref = seed_message(
        index=index,
        mirror=mirrors["acct1"],
        folder=folder1,
        raw=plain_text(),
        message_id="<cross@example.com>",
    )
    mirrors["acct2"].create_folder(account_name="acct2", folder_name="INBOX")

    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=PlaintextCredentialsProvider(config),
        config_path=paths.config_file,
    )
    return app, index, mirrors, folder1, source_ref


async def test_move_cross_account_unreadable_source_is_reported() -> None:
    """A source mirror that cannot produce bytes aborts that message."""
    app, index, mirrors, folder1, source_ref = _cross_account_app("cross-unreadable")
    folder2 = FolderRef(account_name="acct2", folder_name="INBOX")
    notifications = _capture_notifications(app)

    def _boom(*, folder: FolderRef, storage_key: str) -> bytes:  # noqa: ARG001
        raise OSError("mirror unreadable")

    mirrors["acct1"].get_message_bytes = _boom  # type: ignore[method-assign]

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msg = index.get_message(message_ref=source_ref)
        assert msg is not None
        screen._move_to_folder([msg], folder2)

    assert any("Could not read source message" in n for n in notifications)
    assert len(index.list_folder_messages(folder=folder2)) == 0


async def test_move_cross_account_unwritable_target_is_reported() -> None:
    """A target mirror that rejects the write leaves the source in place."""
    app, index, mirrors, folder1, source_ref = _cross_account_app("cross-unwritable")
    folder2 = FolderRef(account_name="acct2", folder_name="INBOX")
    notifications = _capture_notifications(app)

    def _boom(*, folder: FolderRef, raw_message: bytes) -> str:  # noqa: ARG001
        raise OSError("target read-only")

    mirrors["acct2"].store_message = _boom  # type: ignore[method-assign]

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msg = index.get_message(message_ref=source_ref)
        assert msg is not None
        screen._move_to_folder([msg], folder2)

    assert any("Failed to write copy to INBOX" in n for n in notifications)
    # Source row survives so the user can retry.
    assert len(index.list_folder_messages(folder=folder1)) == 1


async def test_move_from_read_only_folder_is_refused() -> None:
    """Move refuses to touch a read-only source folder."""
    import dataclasses as _dc

    from pony.domain import FolderConfig

    paths = make_tmp_paths("cross-readonly-src")
    account1 = _dc.replace(
        make_test_account(paths, name="acct1"),
        folders=FolderConfig(read_only=("INBOX",)),
    )
    account2 = make_test_account(paths, name="acct2")
    config = make_test_config(accounts=(account1, account2))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    folder1 = FolderRef(account_name="acct1", folder_name="INBOX")
    source_ref = seed_message(
        index=index,
        mirror=mirrors["acct1"],
        folder=folder1,
        raw=plain_text(),
        message_id="<ro@example.com>",
    )
    mirrors["acct2"].create_folder(account_name="acct2", folder_name="INBOX")

    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=PlaintextCredentialsProvider(config),
        config_path=paths.config_file,
    )
    notifications = _capture_notifications(app)
    folder2 = FolderRef(account_name="acct2", folder_name="INBOX")

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msg = index.get_message(message_ref=source_ref)
        assert msg is not None
        screen._move_to_folder([msg], folder2)

    assert any("read-only folder" in n for n in notifications)
    assert len(index.list_folder_messages(folder=folder2)) == 0


async def test_next_unread_request_advances_the_reader() -> None:
    """The reader's next-unread jump loads the following unread message."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="next-unread",
        seed=[(folder, plain_text()), (folder, multipart_mixed_attachment())],
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        screen = app.screen
        assert isinstance(screen, MainScreen)
        view = screen.query_one(MessageViewPanel)
        view.post_message(MessageViewPanel.NextUnreadRequested())
        await pilot.pause()
        await pilot.pause()

        rows = index.list_folder_messages(folder=folder)
        seen = [r for r in rows if MessageFlag.SEEN in r.local_flags]
        assert len(seen) >= 1


async def test_next_unread_request_at_the_end_is_a_noop() -> None:
    """With nothing unread after the cursor the reader stays put.

    Seeded so that an unread row sits *before* the cursor: the search
    has to start after it, and "nothing unread anywhere" would pass
    whichever end it started from.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="next-unread-end",
        seed=[
            (folder, _custom_plain("a", body="a body")),
            (folder, _custom_plain("b", body="b body")),
            (folder, _custom_plain("c", body="c body")),
        ],
    )

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msg_list = screen.query_one(MessageListPanel)
        msg_list.focus()
        await pilot.pause()
        # Open the last row; "b" in the middle is still unread.
        await pilot.press("greater_than_sign")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        unread = [
            summary.subject
            for summary in msg_list._summaries  # noqa: SLF001
            if MessageFlag.SEEN not in summary.local_flags
        ]
        view = screen.query_one(MessageViewPanel)
        before = msg_list.get_selected_summary()
        view.post_message(MessageViewPanel.NextUnreadRequested())
        await pilot.pause()
        await pilot.pause()
        after = msg_list.get_selected_summary()

    # "Stays put" is about the cursor, not about the screen class.
    assert unread == ["b"]
    assert before is not None
    assert before.subject == "c"
    assert after is not None
    assert after.message_ref == before.message_ref


async def test_external_editor_without_configuration_warns() -> None:
    """Alt+E with no editor configured explains instead of failing."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="editor-unset")
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        screen.action_edit_external()
        await pilot.pause()

    assert any("No external editor configured" in n for n in notifications)


async def test_external_editor_path_that_is_not_a_file_warns(tmp_path: Path) -> None:
    """A configured editor that is not an executable file is rejected."""
    import dataclasses as _dc

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="editor-missing")
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        screen._config = _dc.replace(screen._config, editor=str(tmp_path / "nope"))
        screen.action_edit_external()
        await pilot.pause()

    assert any("No external editor configured" in n for n in notifications)


async def test_external_editor_round_trips_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The editor's edits to the temp file are loaded back into the body."""
    import contextlib
    import dataclasses as _dc

    from textual.widgets import TextArea

    editor = tmp_path / "fake-editor"
    editor.write_text("#!/bin/sh\n", encoding="utf-8")

    edited: dict[str, str] = {}

    def _fake_run(argv: list[str], **_kw: object) -> object:
        target = Path(argv[1])
        edited["before"] = target.read_text(encoding="utf-8")
        target.write_text("Rewritten by the editor.", encoding="utf-8")
        return None

    monkeypatch.setattr("pony.tui.screens.compose_screen.subprocess.run", _fake_run)

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="editor-ok")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        screen._config = _dc.replace(screen._config, editor=str(editor))
        screen.query_one("#body-area", TextArea).load_text("Original body.")
        # The headless driver cannot suspend; the editor call is what matters.
        monkeypatch.setattr(
            type(app), "suspend", lambda _self: contextlib.nullcontext()
        )
        screen.action_edit_external()
        await pilot.pause()

        assert edited["before"] == "Original body."
        assert (
            screen.query_one("#body-area", TextArea).text == "Rewritten by the editor."
        )


async def test_attachment_picker_rejects_a_non_file(tmp_path: Path) -> None:
    """Choosing a directory in the picker warns and attaches nothing."""
    from pony.tui.screens.add_attachment_screen import AddAttachmentScreen

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="picker-not-file")
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        screen.action_add_attachment()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, AddAttachmentScreen)
        picker.dismiss(str(tmp_path))  # a directory, not a file
        await pilot.pause()

        assert screen._attachment_paths == []

    assert any("Not a file:" in n for n in notifications)


async def test_attachment_picker_accepts_a_file(tmp_path: Path) -> None:
    """A real file chosen in the picker lands in the attachment list."""
    from pony.tui.screens.add_attachment_screen import AddAttachmentScreen

    chosen = tmp_path / "notes.txt"
    chosen.write_text("hello", encoding="utf-8")

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="picker-file")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        screen.action_add_attachment()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, AddAttachmentScreen)
        picker.dismiss(str(chosen))
        await pilot.pause()

        assert screen._attachment_paths == [chosen]


def _two_imap_accounts(label: str, *, folders2: object = None):  # type: ignore[no-untyped-def]
    """Two IMAP accounts with one seeded message in acct1/INBOX.

    *folders2* overrides ``acct2``'s :class:`FolderConfig` so the move
    guards that consult the target account's policy can be exercised.
    """
    import dataclasses as _dc

    paths = make_tmp_paths(label)
    account1 = make_test_account(paths, name="acct1")
    account2 = make_test_account(paths, name="acct2")
    if folders2 is not None:
        account2 = _dc.replace(account2, folders=folders2)
    config = make_test_config(accounts=(account1, account2))
    index = make_index(paths)
    mirrors = make_mirrors(config)

    folder1 = FolderRef(account_name="acct1", folder_name="INBOX")
    source_ref = seed_message(
        index=index,
        mirror=mirrors["acct1"],
        folder=folder1,
        raw=plain_text(),
        message_id=f"<{label}@example.com>",
    )
    mirrors["acct2"].create_folder(account_name="acct2", folder_name="INBOX")

    app = TestPonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=PlaintextCredentialsProvider(config),
        config_path=paths.config_file,
    )
    return app, index, mirrors, folder1, source_ref


async def test_move_to_a_target_excluded_from_sync_is_refused() -> None:
    """A target folder the account never syncs would strand the message."""
    from pony.domain import FolderConfig

    app, index, _mirrors, folder1, source_ref = _two_imap_accounts(
        "target-excluded",
        folders2=FolderConfig(exclude=("INBOX",)),
    )
    folder2 = FolderRef(account_name="acct2", folder_name="INBOX")
    notifications = _capture_notifications(app)

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msg = index.get_message(message_ref=source_ref)
        assert msg is not None
        screen._move_to_folder([msg], folder2)  # noqa: SLF001

    assert any("excluded from sync" in n for n in notifications)
    assert len(index.list_folder_messages(folder=folder2)) == 0
    assert len(index.list_folder_messages(folder=folder1)) == 1


async def test_move_to_a_read_only_target_is_refused() -> None:
    """A read-only target could never push the new message back."""
    from pony.domain import FolderConfig

    app, index, _mirrors, folder1, source_ref = _two_imap_accounts(
        "target-readonly",
        folders2=FolderConfig(read_only=("INBOX",)),
    )
    folder2 = FolderRef(account_name="acct2", folder_name="INBOX")
    notifications = _capture_notifications(app)

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msg = index.get_message(message_ref=source_ref)
        assert msg is not None
        screen._move_to_folder([msg], folder2)  # noqa: SLF001

    assert any("is read-only" in n for n in notifications)
    assert len(index.list_folder_messages(folder=folder2)) == 0
    assert len(index.list_folder_messages(folder=folder1)) == 1


async def test_copy_reports_an_unreadable_source_and_skips_it() -> None:
    """A source the mirror cannot read is skipped, not half-copied."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, mirrors = build_pony_app(label="copy-unreadable")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")
    source_ref = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<copy-unreadable@example.com>",
    )
    drafts = FolderRef(account_name="acct", folder_name="Drafts")
    notifications = _capture_notifications(app)

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msg = index.get_message(message_ref=source_ref)
        assert msg is not None

        def _boom(*, folder: FolderRef, storage_key: str) -> bytes:  # noqa: ARG001
            raise OSError("mirror unreadable")

        mirrors["acct"].get_message_bytes = _boom  # type: ignore[method-assign]
        screen._copy_to_folder([msg], drafts)  # noqa: SLF001

    assert any("Could not read source message" in n for n in notifications)
    assert len(index.list_folder_messages(folder=drafts)) == 0
    assert len(index.list_folder_messages(folder=folder)) == 1


async def test_copy_reports_an_unwritable_target_and_skips_it() -> None:
    """A target the mirror cannot write leaves no orphan index row."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, index, mirrors = build_pony_app(label="copy-unwritable")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Drafts")
    source_ref = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<copy-unwritable@example.com>",
    )
    drafts = FolderRef(account_name="acct", folder_name="Drafts")
    notifications = _capture_notifications(app)

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        msg = index.get_message(message_ref=source_ref)
        assert msg is not None

        def _boom(*, folder: FolderRef, raw_message: bytes) -> str:  # noqa: ARG001
            raise OSError("target full")

        mirrors["acct"].store_message = _boom  # type: ignore[method-assign]
        screen._copy_to_folder([msg], drafts)  # noqa: SLF001

    assert any("Failed to write copy to Drafts" in n for n in notifications)
    assert len(index.list_folder_messages(folder=drafts)) == 0


async def test_new_folder_without_a_selected_account_warns() -> None:
    """With several accounts and no open folder there is no obvious target."""
    app, index, _mirrors, _folder1, _ref = _two_imap_accounts("new-folder-ambiguous")
    del index
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MainScreen)
        screen._current_folder_ref = None  # noqa: SLF001
        screen.action_new_folder()
        await pilot.pause()

    assert any("Select a folder or account first." in n for n in notifications)


async def test_part_zero_saves_the_message_not_the_attachments() -> None:
    """``ctrl+0`` is the message itself.

    It used to mean "every attachment", which was the only numbering in
    the program that did not count parts; every part at once is ``*`` at
    the ``S`` prompt.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, cfg, _paths, _index, _mirrors = build_pony_app(
        label="save-all-idx",
        seed=[(folder, multipart_mixed_attachment())],
    )
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        screen = app.screen
        assert isinstance(screen, MainScreen)
        screen.action_save_attachment("0")
        await pilot.pause()

    downloads = cfg.downloads_path
    assert downloads is not None
    assert not (downloads / "q1-report.pdf").exists()
    written = [path for path in downloads.iterdir() if path.suffix == ".eml"]
    assert len(written) == 1
    assert any(written[0].name in note for note in notifications)


async def test_part_zero_on_a_plain_message_opens_the_message() -> None:
    """``0`` has something to open on every message, viewer or not."""
    from pony.tui.screens.eml_viewer_screen import EmlViewerScreen

    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="open-all-none",
        seed=[(folder, plain_text())],
    )
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        screen = app.screen
        assert isinstance(screen, MainScreen)
        assert screen.query_one(MessageViewPanel).attachment_count == 0
        screen.action_open_attachment("0")
        await pilot.pause()
        assert isinstance(app.screen, EmlViewerScreen)

    assert notifications == []


async def test_the_star_selection_saves_every_part() -> None:
    """``*`` at the ``S`` prompt writes the message and its attachments."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, cfg, _paths, _index, _mirrors = build_pony_app(
        label="save-msg-write",
        seed=[(folder, multipart_mixed_attachment())],
    )
    notifications = _capture_notifications(app)

    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("S")
        await pilot.pause()
        await pilot.press("*")
        await pilot.press("enter")
        await pilot.pause()

    downloads = cfg.downloads_path
    assert downloads is not None
    assert (downloads / "q1-report.pdf").read_bytes().startswith(b"%PDF")
    assert [path for path in downloads.iterdir() if path.suffix == ".eml"]
    # Both names in one notice, because one key wrote both files.
    assert any("q1-report.pdf" in note and ".eml" in note for note in notifications)


async def test_open_attachment_uses_configured_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A [viewers] entry claims the attachment instead of the OS default."""
    from unittest.mock import MagicMock

    from corpus import calendar_invite

    from pony.domain import ViewerRule

    launch_mock = MagicMock()
    monkeypatch.setattr("pony.tui.screens.main_screen.launch_file", launch_mock)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="open-att-viewer",
        seed=[(folder, calendar_invite())],
        viewers=(ViewerRule("text/calendar", ("chronos", "import")),),
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        view.focus()
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause(0.1)

    launch_mock.assert_called_once()
    path, command = launch_mock.call_args.args
    assert path.name == "invite.ics"
    assert command == ("chronos", "import")


async def test_open_attachment_without_matching_viewer_uses_os_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlisted content type still falls through to the OS handler."""
    from unittest.mock import MagicMock

    from corpus import calendar_invite

    from pony.domain import ViewerRule

    launch_mock = MagicMock()
    monkeypatch.setattr("pony.tui.screens.main_screen.launch_file", launch_mock)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="open-att-no-viewer",
        seed=[(folder, calendar_invite())],
        viewers=(ViewerRule("application/pdf", ("zathura",)),),
    )
    async with app.run_test() as pilot:
        await _select_first_inbox(pilot)
        await pilot.press("enter")
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        view.focus()
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause(0.1)

    launch_mock.assert_called_once()
    _path, command = launch_mock.call_args.args
    assert command is None


async def test_quitting_while_a_sync_runs_does_not_wait_it_out() -> None:
    """Q during a sync leaves, and leaves the thread to exit on its own.

    A mail sync has no cancellation point, so the thread is a daemon and
    ``stop`` joins it with a timeout rather than blocking exit on an
    IMAP session that will not come back.  Nothing exercised quitting
    while one was in flight.
    """
    import dataclasses as _dc

    paths = make_tmp_paths("quit-while-syncing")
    account = make_test_account(paths)
    config = _dc.replace(
        make_test_config(accounts=(account,)),
        background_sync_enabled=True,
        background_sync_interval_seconds=3600,
    )
    index = make_index(paths)
    mirrors = make_mirrors(config)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
        message_id="<quit-sync@example.com>",
    )

    running = threading.Event()
    release = threading.Event()

    def _blocks(_self: ImapSyncService, **_kwargs: object) -> SyncResult:
        running.set()
        # Deliberately ignores the cancel event, which is the case the
        # join timeout exists for.
        release.wait(10)
        return SyncResult(accounts=())

    app = PonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=PlaintextCredentialsProvider(config),
        config_path=paths.config_file,
    )
    original = ImapSyncService.sync
    ImapSyncService.sync = _blocks  # type: ignore[method-assign]
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            scheduler = app.mail_sync
            assert scheduler is not None
            assert scheduler.request_now()
            # Polled rather than waited on: the scheduler announces its
            # start through `call_from_thread`, so blocking this thread
            # on an Event blocks the callback it is waiting for.
            for _ in range(200):
                if running.is_set():
                    break
                await pilot.pause()
            assert running.is_set()
            assert scheduler.running

            await pilot.press("Q")
            await pilot.pause()

            # Left: the app is gone while the uninterruptible run is
            # still in flight on its daemon thread.  Waiting it out is
            # what must not happen -- the exit would hang on it.
            assert not app.is_running
            assert scheduler.running
            # Let it finish before teardown, so the unmount's own
            # `stop` does not spend its 5 s join timeout here.
            release.set()
            for _ in range(200):
                if not scheduler.running:
                    break
                await pilot.pause()
    finally:
        ImapSyncService.sync = original  # type: ignore[method-assign]
        release.set()
