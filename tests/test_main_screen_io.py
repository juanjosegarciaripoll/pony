"""Focused export and attachment boundary tests for ``MainScreen``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from corpus import multipart_mixed_attachment, plain_text
from tui_helpers import build_pony_app

from pony.domain import FolderRef
from pony.tui.screens.main_screen import MainScreen
from pony.tui.screens.save_message_screen import SaveItem


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


async def test_export_actions_ignore_empty_selection_and_attachments() -> None:
    app, *_ = build_pony_app(label="main-io-empty")
    messages = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _main(app)
        screen.action_print_pdf()
        screen.action_save_message()
        screen.action_attachments_open()
        screen.action_attachments_save()

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


async def test_save_indices_reports_saved_missing_and_errors(tmp_path: Path) -> None:
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
        screen.save_attachment = Mock(
            side_effect=["fictional.pdf", None, OSError("fictional save failure")]
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
        screen.save_attachment = Mock(
            side_effect=[None, OSError("fictional save failure"), "opened.txt"]
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
