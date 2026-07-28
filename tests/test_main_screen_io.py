"""Focused export and attachment boundary tests for ``MainScreen``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from corpus import multipart_mixed_attachment, plain_text
from tui_helpers import build_pony_app

from pony.domain import FolderRef
from pony.tui.screens.main_screen import MainScreen
from pony.tui.screens.save_message_screen import SaveItem
from pony.tui.widgets.message_view import MessageViewPanel


def _main(app: object) -> MainScreen:
    return next(
        screen
        for screen in app.screen_stack  # type: ignore[attr-defined]
        if isinstance(screen, MainScreen)
    )


def _notifications(app: object) -> list[str]:
    messages: list[str] = []
    original = app.notify  # type: ignore[attr-defined]

    def capture(message: str, **kwargs: object) -> None:
        messages.append(message)
        original(message, **kwargs)

    app.notify = capture  # type: ignore[attr-defined]
    return messages


async def _open_message(pilot: object) -> None:
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


async def test_export_actions_are_silent_with_nothing_selected() -> None:
    """No selection means no target; these no-op like the other actions."""
    app, *_ = build_pony_app(label="main-io-empty")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen.action_print_pdf()
        screen.action_save_message()
        screen.action_attachments_open()
        screen.action_attachments_save()

    assert messages == []


async def test_attachment_actions_say_so_when_there_are_none() -> None:
    """A selected message with no attachments still explains itself."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-io-no-attachments",
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen.action_attachments_open()
        screen.action_attachments_save()
        await pilot.pause()

    assert messages == [
        "No attachments on this message.",
        "No attachments on this message.",
    ]


async def test_pdf_and_message_export_report_mirror_read_failure() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="main-io-read-failure",
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _open_message(pilot)
        screen = _main(app)
        mirrors["acct"].get_message_bytes = Mock(
            side_effect=OSError("fictional read failure")
        )
        screen.action_print_pdf()
        screen.action_save_message()

    assert messages == [
        "Could not read message from mirror.",
        "Could not read message from mirror.",
    ]


async def test_attachment_picker_cancel_and_selection_callbacks() -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-io-picker",
        seed=[(folder, multipart_mixed_attachment())],
    )

    async with app.run_test() as pilot:
        await _open_message(pilot)
        screen = _main(app)
        chosen = Mock()
        app.push_screen = Mock()
        screen._prompt_attachments(action_label="Save", then=chosen)
        callback = app.push_screen.call_args.args[1]
        callback(None)
        chosen.assert_not_called()
        callback([1])
        chosen.assert_called_once_with([1])


async def test_save_indices_reports_saved_missing_and_errors(
    monkeypatch, tmp_path: Path
) -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-io-save-indices",
        seed=[(folder, multipart_mixed_attachment())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _open_message(pilot)
        screen = _main(app)
        screen._config = screen._config.__class__(
            accounts=screen._config.accounts,
            downloads_path=tmp_path,
        )
        monkeypatch.setattr(
            "pony.tui.screens.main_screen.save_one_attachment",
            Mock(
                side_effect=["fictional.pdf", None, OSError("fictional save failure")]
            ),
        )
        screen._save_indices([1, 2, 3])

    assert any("Could not save attachment 3" in message for message in messages)
    assert any("Saved 1 attachment(s)" in message for message in messages)
    assert any("Attachment(s) not found: 2" in message for message in messages)


async def test_open_indices_reports_missing_save_and_launch_errors(
    monkeypatch, tmp_path: Path
) -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-io-open-indices",
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)
    monkeypatch.setattr(
        "pony.tui.screens.main_screen.launch_file",
        Mock(side_effect=OSError("fictional launch failure")),
    )

    async with app.run_test() as pilot:
        await _open_message(pilot)
        screen = _main(app)
        screen._config = screen._config.__class__(
            accounts=screen._config.accounts,
            downloads_path=tmp_path,
        )
        monkeypatch.setattr(
            "pony.tui.screens.main_screen.save_one_attachment",
            Mock(side_effect=[None, OSError("fictional save failure"), "opened.txt"]),
        )
        screen._open_indices([1, 2, 3])

    assert any("Attachment(s) not found: 1" in message for message in messages)
    assert any("Could not save attachment 2" in message for message in messages)
    assert any("Could not open opened.txt" in message for message in messages)


async def test_save_message_callbacks_handle_cancel_success_and_traversal(
    tmp_path: Path,
) -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-io-save-message",
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await _open_message(pilot)
        screen = _main(app)
        app.push_screen = Mock()
        screen.action_save_message()

        on_items = app.push_screen.call_args.args[1]
        on_items(None)
        assert app.push_screen.call_count == 1
        on_items(
            [
                SaveItem(kind="body", filename="message.md"),
                SaveItem(kind="body", filename="../outside.md"),
            ]
        )
        assert app.push_screen.call_count == 2
        on_folder = app.push_screen.call_args.args[1]
        on_folder(None)
        assert not (tmp_path / "message.md").exists()
        on_folder(tmp_path)

    assert (tmp_path / "message.md").is_file()
    assert not (tmp_path.parent / "outside.md").exists()
    assert messages[-1].endswith("(1 failed)")


async def test_the_print_pdf_folder_callback_guards_the_destination(
    tmp_path: Path,
) -> None:
    """Cancelling writes nothing; a filename escaping the folder is refused.

    The proposed filename comes from the message subject, so a crafted
    subject must not be able to steer the write outside the folder the
    user picked.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-pdf-callback",
        seed=[(folder, plain_text())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        screen = _main(app)

        captured: list[object] = []

        def _capture(_screen: object, callback: object = None, **_kw: object) -> None:
            captured.append(callback)

        app.push_screen = Mock(side_effect=_capture)
        screen.run_worker = Mock()  # type: ignore[method-assign]
        screen.action_print_pdf()
        await pilot.pause()

        on_folder = captured[0]
        assert callable(on_folder)

        # Cancelled — no export worker started.
        on_folder(None)
        screen.run_worker.assert_not_called()

        # A destination the resolved path escapes.
        escaping = tmp_path / "sub"
        escaping.mkdir()
        real_resolve = Path.resolve

        def _fake_resolve(self: Path, *args: object, **kwargs: object) -> Path:
            if self.name.endswith(".pdf"):
                return tmp_path / "outside.pdf"
            return real_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

        Path.resolve = _fake_resolve  # type: ignore[method-assign]
        try:
            on_folder(escaping)
        finally:
            Path.resolve = real_resolve  # type: ignore[method-assign]

        screen.run_worker.assert_not_called()
        await pilot.pause()

    assert any("Invalid destination." in m for m in messages)


async def test_save_all_attachments_writes_every_part(tmp_path: Path) -> None:
    """The screen-level helper delegates to the reader panel."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-save-all-helper",
        seed=[(folder, multipart_mixed_attachment())],
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        screen = _main(app)

        saved = screen.save_all_attachments(tmp_path)

        assert saved == ["q1-report.pdf"]
        assert (tmp_path / "q1-report.pdf").is_file()


async def test_opening_an_index_past_the_end_reports_it(tmp_path: Path) -> None:
    """The index comes from the user, so it can name an attachment that is not there."""
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="main-open-out-of-range",
        seed=[(folder, multipart_mixed_attachment())],
    )
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._downloads_dir = Mock(return_value=tmp_path)

        screen._open_indices([99])
        await pilot.pause()

    assert any("Attachment(s) not found: 99" in message for message in messages)


def _attachment_message(subject: str, attachment: str) -> bytes:
    """A message whose attachment name identifies which message it came from."""
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("mixed")
    msg["From"] = "sender@example.com"
    msg["To"] = "acct@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
    msg["Message-ID"] = f"<{subject}@example.com>"
    msg.attach(MIMEText(f"body of {subject}\n"))
    part = MIMEApplication(subject.encode(), Name=attachment)
    part["Content-Disposition"] = f'attachment; filename="{attachment}"'
    msg.attach(part)
    return msg.as_bytes()


async def _open_first_then_move_cursor(pilot: object, app: object):  # type: ignore[no-untyped-def]
    """Open the first message, then move the cursor to the second."""
    from pony.tui.widgets.message_list import MessageListPanel

    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    screen = _main(app)
    panel = screen.query_one(MessageListPanel)
    panel.focus()
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press("down")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    return screen


async def test_attachment_actions_follow_the_cursor_not_the_reader(
    tmp_path: Path,
) -> None:
    """The reader holds the last message opened; actions act on the selection.

    Regression: `w`, `O`, `S` and the number accelerators read their bytes
    from the reader pane while `s` and `ctrl+p` used the cursor row.  With
    the reader on one message and the cursor on another they operated on
    different mail, silently.  Reachable since cursor movement stopped
    loading the reader.
    """
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="io-cursor-not-reader",
        seed=[
            (folder, _attachment_message("AAA", "aaa.bin")),
            (folder, _attachment_message("BBB", "bbb.bin")),
        ],
    )

    async with app.run_test() as pilot:
        screen = await _open_first_then_move_cursor(pilot, app)
        screen._downloads_dir = Mock(return_value=tmp_path)

        opened = screen.query_one(MessageViewPanel)
        assert opened.raw_bytes is not None
        assert b"AAA" in opened.raw_bytes, "reader still shows the opened message"

        current = screen.get_current_message()
        assert current is not None
        assert current.subject == "BBB", "cursor moved to the second message"

        saved = screen.save_all_attachments(tmp_path)

    assert saved == ["bbb.bin"], "save must act on the highlighted message"
    assert (tmp_path / "bbb.bin").is_file()
    assert not (tmp_path / "aaa.bin").exists()


async def test_the_numeric_accelerator_also_follows_the_cursor(
    tmp_path: Path,
) -> None:
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    app, *_ = build_pony_app(
        label="io-accel-cursor",
        seed=[
            (folder, _attachment_message("AAA", "aaa.bin")),
            (folder, _attachment_message("BBB", "bbb.bin")),
        ],
    )

    async with app.run_test() as pilot:
        screen = await _open_first_then_move_cursor(pilot, app)
        screen._downloads_dir = Mock(return_value=tmp_path)

        screen.action_save_attachment("1")
        await pilot.pause()

    assert (tmp_path / "bbb.bin").is_file()
    assert not (tmp_path / "aaa.bin").exists()


async def test_saving_with_no_message_selected_is_a_noop(tmp_path: Path) -> None:
    app, *_ = build_pony_app(label="io-cursor-empty")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen._downloads_dir = Mock(return_value=tmp_path)

        assert screen.save_all_attachments(tmp_path) == []
        assert screen.save_attachment(1, tmp_path) is None
        screen.action_save_attachment("0")
        await pilot.pause()

    assert list(tmp_path.iterdir()) == []
