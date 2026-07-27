"""Widget-level behaviour of ``ComposeScreen``.

Covers the parts driven by clicks and drops rather than by the send
path: the attachment bar's add/remove buttons, the dynamic address-row
buttons and their defensive guards, drag-and-drop paste handling, and
the From-select → account resolution that everything else depends on.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from uuid import uuid4

from textual.containers import Horizontal, Vertical
from textual.events import Paste
from textual.widgets import Button, Input
from tui_helpers import build_compose_app

from pony.tui.screens.compose_screen import (
    AttachmentsBar,
    ComposeScreen,
    _AddrRow,
    _AttachRow,
)


def _screen(app: object) -> ComposeScreen:
    screen = app.screen  # type: ignore[attr-defined]
    assert isinstance(screen, ComposeScreen)
    return screen


def _notifications(app: object) -> list[str]:
    messages: list[str] = []
    original = app.notify  # type: ignore[attr-defined]

    def capture(message: str, **kwargs: object) -> None:
        messages.append(message)
        original(message, **kwargs)

    app.notify = capture  # type: ignore[attr-defined]
    return messages


# ---------------------------------------------------------------------------
# Attachment bar buttons
# ---------------------------------------------------------------------------


async def test_the_attachment_plus_button_opens_the_picker() -> None:
    from pony.tui.screens.add_attachment_screen import AddAttachmentScreen

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="attach-plus")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        add_button = screen.query_one(".attach-add-btn", Button)

        screen.on_button_pressed(Button.Pressed(add_button))
        await pilot.pause()

        assert isinstance(app.screen, AddAttachmentScreen)
        app.screen.dismiss(None)
        await pilot.pause()


async def test_the_attachment_remove_button_drops_that_file(tmp_path: Path) -> None:
    """Removing one row leaves the other attachments in place."""
    keep = tmp_path / "keep.txt"
    drop = tmp_path / "drop.txt"
    keep.write_text("keep", encoding="utf-8")
    drop.write_text("drop", encoding="utf-8")

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="attach-remove")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        screen._attachment_paths.extend([keep, drop])
        screen._refresh_attachments_bar()
        await pilot.pause()

        rows = list(screen.query(_AttachRow))
        assert len(rows) == 2

        target = next(r for r in rows if r.attachment_path == drop)
        remove_button = target.query_one(".attach-remove-btn", Button)
        screen.on_button_pressed(Button.Pressed(remove_button))
        await pilot.pause()

        assert screen._attachment_paths == [keep]


async def test_removing_an_attachment_twice_is_harmless(tmp_path: Path) -> None:
    """A stale row must not raise when its path is already gone."""
    only = tmp_path / "only.txt"
    only.write_text("only", encoding="utf-8")

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="attach-twice")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        screen._attachment_paths.append(only)
        screen._refresh_attachments_bar()
        await pilot.pause()

        row = screen.query_one(_AttachRow)
        remove_button = row.query_one(".attach-remove-btn", Button)
        screen.on_button_pressed(Button.Pressed(remove_button))
        await pilot.pause()
        # Second press against the now-detached row.
        screen.on_button_pressed(Button.Pressed(remove_button))
        await pilot.pause()

        assert screen._attachment_paths == []


# ---------------------------------------------------------------------------
# Address-row guards
# ---------------------------------------------------------------------------


async def test_row_buttons_outside_an_address_row_are_ignored() -> None:
    """The handlers key off button classes, so a stray button must no-op."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="addr-stray")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        cc_container = screen.query_one("#cc-container", Vertical)
        before = len(list(cc_container.query(_AddrRow)))

        # Buttons carrying the row classes but mounted outside an _AddrRow.
        orphan_add = Button("+", classes="addr-add-btn")
        orphan_remove = Button("×", classes="addr-remove-btn")
        await screen.mount(Horizontal(orphan_add, orphan_remove))
        await pilot.pause()

        screen.on_button_pressed(Button.Pressed(orphan_add))
        screen.on_button_pressed(Button.Pressed(orphan_remove))
        await pilot.pause()

        assert len(list(cc_container.query(_AddrRow))) == before


async def test_an_address_row_outside_a_container_is_ignored() -> None:
    """A row whose parent is not the vertical container cannot be resolved."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="addr-nocontainer")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        loose_row = _AddrRow("")
        await screen.mount(Horizontal(loose_row))
        await pilot.pause()

        add_button = loose_row.query_one(".addr-add-btn", Button)
        remove_button = loose_row.query_one(".addr-remove-btn", Button)
        screen.on_button_pressed(Button.Pressed(add_button))
        screen.on_button_pressed(Button.Pressed(remove_button))
        await pilot.pause()

        # Nothing raised, and the row survives untouched.
        assert loose_row.is_mounted


async def test_removing_a_middle_row_keeps_the_plus_on_the_last_one() -> None:
    """Only the final row offers ``+``, however rows are removed."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="addr-middle")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        container = screen.query_one("#cc-container", Vertical)

        for _ in range(2):
            rows = list(container.query(_AddrRow))
            screen.on_button_pressed(
                Button.Pressed(rows[-1].query_one(".addr-add-btn", Button))
            )
            await pilot.pause()

        rows = list(container.query(_AddrRow))
        assert len(rows) == 3

        # Remove the middle row.
        screen.on_button_pressed(
            Button.Pressed(rows[1].query_one(".addr-remove-btn", Button))
        )
        await pilot.pause()

        rows = list(container.query(_AddrRow))
        assert len(rows) == 2
        assert rows[0].query_one(".addr-add-btn", Button).display is False
        assert rows[-1].query_one(".addr-add-btn", Button).display is True


# ---------------------------------------------------------------------------
# Paste / drag-and-drop
# ---------------------------------------------------------------------------


async def test_pasting_paths_ignores_blank_lines(tmp_path: Path) -> None:
    """Terminals pad drops with blank lines; they are not bad paths."""
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="paste-blank")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        bar = screen.query_one(AttachmentsBar)

        bar.on_paste(Paste(f"\n{first}\n\n'{second}'\n"))  # type: ignore[attr-defined]
        await pilot.pause()

        assert screen._attachment_paths == [first, second]


async def test_pasting_a_file_url_is_unquoted(tmp_path: Path) -> None:
    """GNOME and KDE drop ``file://`` URLs with percent-escapes."""
    target = tmp_path / "a file.txt"
    target.write_text("x", encoding="utf-8")

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="paste-url")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        bar = screen.query_one(AttachmentsBar)

        bar.on_paste(Paste(f"file://{target.as_posix().replace(' ', '%20')}"))  # type: ignore[attr-defined]
        await pilot.pause()

        assert screen._attachment_paths == [target]


async def test_pasting_ordinary_text_attaches_nothing() -> None:
    """A paste that is not a path warns once and adds no attachment."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="paste-text")
    notifications = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        bar = screen.query_one(AttachmentsBar)

        bar.on_paste(Paste("just some prose the user copied"))  # type: ignore[attr-defined]
        await pilot.pause()

        assert screen._attachment_paths == []

    assert any("Not a file:" in n for n in notifications)


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------


async def test_sending_without_a_resolvable_account_explains_itself() -> None:
    """Send must not proceed when the From-select names no known account.

    ``#from-select`` is built with ``allow_blank=False``, so the truly
    blank case cannot be reached through the UI; the reachable failure
    is a selection that matches none of the screen's accounts.
    """
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="account-blank")
    notifications = _notifications(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        screen._accounts = []

        screen.query_one("#to-input", Input).value = "someone@example.test"
        screen.action_send()
        await pilot.pause()

    assert any("Could not determine sending account." in n for n in notifications)


async def test_an_unknown_selected_account_resolves_to_none() -> None:
    """A selection naming no configured account is not silently accepted."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="account-unknown")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        screen._accounts = []

        assert screen._get_account() is None


async def test_the_body_title_names_the_configured_editor(tmp_path: Path) -> None:
    """The Alt+E hint only appears when the editor actually exists."""
    editor = tmp_path / "my-editor"
    editor.write_text("#!/bin/sh\n", encoding="utf-8")

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="body-title")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)

        screen._config = dataclasses.replace(screen._config, editor=str(editor))
        screen._refresh_body_title()
        await pilot.pause()
        assert "my-editor" in str(screen.query_one("#body-area").border_title)

        screen._config = dataclasses.replace(
            screen._config, editor=str(tmp_path / f"gone-{uuid4().hex}")
        )
        screen._refresh_body_title()
        await pilot.pause()
        assert "Alt+E" not in str(screen.query_one("#body-area").border_title)
