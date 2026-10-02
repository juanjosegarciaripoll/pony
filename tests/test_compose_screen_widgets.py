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
from textual.widgets.input import Selection
from tui_helpers import build_compose_app

from pony.tui.screens.compose_screen import (
    BCC_CONTAINER,
    CC_CONTAINER,
    TO_CONTAINER,
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
        assert rows[0].query_one(".addr-add-btn", Button).visible is False
        assert rows[-1].query_one(".addr-add-btn", Button).visible is True


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


# ---------------------------------------------------------------------------
# Quoted display names
#
# Regression: replying to a message whose recipients carry quoted display
# names ("Doe, John" <j@example.com>) produced one Cc row per comma
# rather than per address, so a single recipient arrived as two broken
# entries and the message went to the wrong (or no) address.
# ---------------------------------------------------------------------------


async def test_quoted_display_names_get_one_cc_row_each() -> None:
    cc = (
        '"Doe, John" <john@example.test>, '
        '"O\'Brien, Mary" <mary@example.test>, '
        "plain@example.test"
    )
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="quoted-cc-rows", cc=cc
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        rows = list(screen.query_one("#cc-container", Vertical).query(_AddrRow))

        values = [row.query_one(Input).value for row in rows]

    assert values == [
        '"Doe, John" <john@example.test>',
        '"O\'Brien, Mary" <mary@example.test>',
        "plain@example.test",
    ]


async def test_quoted_display_names_survive_the_round_trip() -> None:
    """Rows are collected back into a header, which must match the input."""
    cc = '"Lastname, Firstname" <fl@example.test>, plain@example.test'
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="quoted-cc-roundtrip", cc=cc
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)

        assert screen._collect_field("cc-container") == cc


async def test_a_sent_message_keeps_quoted_recipients_intact() -> None:
    """The address that reaches SMTP must be the one the reply started with."""
    from unittest.mock import Mock

    import pony.tui.screens.compose_screen as compose_module

    cc = '"Doe, John" <john@example.test>, plain@example.test'
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="quoted-cc-send",
        to="recipient@example.test",
        subject="Subject",
        body="Body",
        cc=cc,
    )
    sent: list[object] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        compose_module.smtp_send = Mock(  # type: ignore[attr-defined]
            side_effect=lambda **kwargs: sent.append(kwargs)
        )
        screen.action_send()
        await pilot.pause()

    assert sent, "the message was never handed to SMTP"
    message = sent[0]["msg"]  # type: ignore[index]
    addresses = [a.addr_spec for a in message["Cc"].addresses]
    names = [a.display_name for a in message["Cc"].addresses]
    assert addresses == ["john@example.test", "plain@example.test"]
    assert names[0] == "Doe, John"


async def test_a_non_plaintext_account_can_actually_send() -> None:
    """Every backend offered in the From dropdown must reach SMTP.

    ``action_send`` used to read ``account.password`` — the literal TOML
    field — so an account using the ``env``, ``command`` or ``encrypted``
    backend was listed as sendable and then refused at send time with a
    request for a password it was configured never to store.
    """
    from unittest.mock import Mock

    from tui_helpers import make_test_account, make_tmp_paths

    import pony.tui.screens.compose_screen as compose_module

    paths = make_tmp_paths("env-send")
    account = dataclasses.replace(
        make_test_account(paths),
        credentials_source="env",
        password=None,
    )
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="env-send",
        account=account,
        to="recipient@example.test",
        subject="Subject",
        body="Body",
    )
    sent: list[object] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        screen._credentials = Mock(  # type: ignore[attr-defined]
            get_password=Mock(return_value="resolved-from-env")
        )
        compose_module.smtp_send = Mock(  # type: ignore[attr-defined]
            side_effect=lambda **kwargs: sent.append(kwargs)
        )
        screen.action_send()
        await pilot.pause()

    assert sent, "an env-backed account was refused at send time"
    assert sent[0]["password"] == "resolved-from-env"  # type: ignore[index]


async def test_a_failing_credential_lookup_is_reported_not_crashed() -> None:
    """A backend that cannot produce a password must not take the app down."""
    from unittest.mock import Mock

    import pony.tui.screens.compose_screen as compose_module
    from pony.config import ConfigError

    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="cred-failure",
        to="recipient@example.test",
        subject="Subject",
        body="Body",
    )
    sent: list[object] = []
    notices: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        screen._credentials = Mock(  # type: ignore[attr-defined]
            get_password=Mock(side_effect=ConfigError("keyring locked"))
        )
        screen.notify = Mock(  # type: ignore[method-assign]
            side_effect=lambda message, **_kw: notices.append(str(message))
        )
        compose_module.smtp_send = Mock(  # type: ignore[attr-defined]
            side_effect=lambda **kwargs: sent.append(kwargs)
        )
        screen.action_send()
        await pilot.pause()

    assert not sent, "sent despite having no password"
    assert any("keyring locked" in n for n in notices), notices


async def test_without_a_provider_the_configured_password_is_used() -> None:
    """A composer built without a provider still sends from config."""
    from unittest.mock import Mock

    import pony.tui.screens.compose_screen as compose_module

    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="no-provider-send",
        to="recipient@example.test",
        subject="Subject",
        body="Body",
    )
    sent: list[object] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        assert screen._credentials is None
        compose_module.smtp_send = Mock(  # type: ignore[attr-defined]
            side_effect=lambda **kwargs: sent.append(kwargs)
        )
        screen.action_send()
        await pilot.pause()

    assert sent, "the configured password was ignored"


async def test_no_password_anywhere_is_refused_with_a_clear_notice() -> None:
    """Neither a provider nor a configured password: refuse, do not crash."""
    from unittest.mock import Mock

    from tui_helpers import make_test_account, make_tmp_paths

    import pony.tui.screens.compose_screen as compose_module

    paths = make_tmp_paths("no-password")
    account = dataclasses.replace(make_test_account(paths), password=None)
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="no-password",
        account=account,
        to="recipient@example.test",
        subject="Subject",
        body="Body",
    )
    sent: list[object] = []
    notices: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        screen.notify = Mock(  # type: ignore[method-assign]
            side_effect=lambda message, **_kw: notices.append(str(message))
        )
        compose_module.smtp_send = Mock(  # type: ignore[attr-defined]
            side_effect=lambda **kwargs: sent.append(kwargs)
        )
        screen.action_send()
        await pilot.pause()

    assert not sent
    assert any("No password available" in n for n in notices), notices


# ---------------------------------------------------------------------------
# Splitting an address list into one row per address
# ---------------------------------------------------------------------------


def _row_values(screen: ComposeScreen, container_id: str) -> list[str]:
    container = screen.query_one(f"#{container_id}", Vertical)
    return [row.address_input.value for row in container.query(_AddrRow)]


async def test_to_opens_with_one_row_per_initial_address() -> None:
    """To: is a row group like Cc/Bcc, so a draft's list arrives split."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="to-rows", to="alice@example.test, bob@example.test"
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)

        assert _row_values(screen, TO_CONTAINER) == [
            "alice@example.test",
            "bob@example.test",
        ]
        # Collecting the rows back reproduces the header it came from.
        assert screen._collect_field(TO_CONTAINER) == (
            "alice@example.test, bob@example.test"
        )


async def test_a_typed_address_list_splits_when_the_field_is_left() -> None:
    """Typing stays undisturbed; the split happens on blur."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="split-blur")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        field = screen.query_one("#to-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.value = "alice@example.test, bob@example.test"
        await pilot.pause()
        # Still one row — nothing moves while the caret is in the field.
        assert _row_values(screen, TO_CONTAINER) == [
            "alice@example.test, bob@example.test"
        ]

        screen.query_one("#subject-input", Input).focus()
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, TO_CONTAINER) == [
            "alice@example.test",
            "bob@example.test",
        ]


async def test_a_comma_inside_a_quoted_display_name_is_not_a_separator() -> None:
    """``"Doe, Jane"`` is one recipient, not two."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="split-quoted")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        field = screen.query_one("#cc-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.value = '"Doe, Jane" <jane@example.test>, bob@example.test'
        screen.query_one("#subject-input", Input).focus()
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, CC_CONTAINER) == [
            '"Doe, Jane" <jane@example.test>',
            "bob@example.test",
        ]


async def test_a_list_pasted_one_address_per_line_keeps_every_line() -> None:
    """Textual's Input pastes only the first line; recipients cannot be dropped."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="paste-lines")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        field = screen.query_one("#to-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.post_message(Paste("a@example.test\nb@example.test\nc@example.test"))
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, TO_CONTAINER) == [
            "a@example.test",
            "b@example.test",
            "c@example.test",
        ]


async def test_a_pasted_address_list_splits_without_waiting_for_a_blur() -> None:
    """A paste holds no half-typed address, so it can split immediately."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="paste-commas")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        field = screen.query_one("#bcc-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.post_message(Paste("a@example.test, b@example.test"))
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, BCC_CONTAINER) == [
            "a@example.test",
            "b@example.test",
        ]


async def test_pasting_one_address_leaves_the_row_alone() -> None:
    """The common paste must not gain a stray empty row."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="paste-single")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        field = screen.query_one("#to-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.post_message(Paste("solo@example.test"))
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, TO_CONTAINER) == ["solo@example.test"]


async def test_the_split_rows_land_next_to_the_row_they_came_from() -> None:
    """A middle row's list expands in place, not at the end of the group."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="split-middle", cc="first@example.test, last@example.test"
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        field = screen.query_one("#cc-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.value = "one@example.test, two@example.test"
        screen.query_one("#subject-input", Input).focus()
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, CC_CONTAINER) == [
            "one@example.test",
            "two@example.test",
            "last@example.test",
        ]


async def test_the_remove_button_sits_at_one_column_on_every_row() -> None:
    """The + is hidden, not removed, so × cannot shift between rows."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="button-columns", to="a@example.test, b@example.test"
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = _screen(app)
        rows = list(screen.query_one("#to-container", Vertical).query(_AddrRow))
        assert len(rows) == 2

        columns = {row.query_one(".addr-remove-btn", Button).region.x for row in rows}
        assert len(columns) == 1, f"× moved between rows: {columns}"

        # And the field stops short of the terminal edge, so the buttons
        # stay beside the address rather than a screen away from it.
        field = rows[0].address_input
        assert field.region.right < 120 - 20


async def test_a_paste_over_a_selection_replaces_it() -> None:
    """Pasting with text selected must not append alongside it."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="paste-selection", to="wrong@example.test"
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        field = screen.query_one("#to-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.selection = Selection(0, len(field.value))
        field.post_message(Paste("right@example.test, other@example.test"))
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, TO_CONTAINER) == [
            "right@example.test",
            "other@example.test",
        ]


async def test_an_empty_paste_changes_nothing() -> None:
    """A stray empty clipboard must not clear the field or add a row."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="paste-empty", to="keep@example.test"
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        field = screen.query_one("#to-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.post_message(Paste("   \n\n  "))
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, TO_CONTAINER) == ["keep@example.test"]


async def test_leaving_a_field_tidies_a_trailing_separator() -> None:
    """A dangling comma must not reach the header."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="tidy-comma")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _screen(app)
        field = screen.query_one("#to-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.value = "  alice@example.test ,  "
        screen.query_one("#subject-input", Input).focus()
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, TO_CONTAINER) == ["alice@example.test"]
        assert screen._collect_field(TO_CONTAINER) == "alice@example.test"


async def test_the_completion_list_stays_shut_when_a_row_is_tidied() -> None:
    """The tidy-up is not typing, so it must not reopen the dropdown."""
    from textual.widgets import OptionList

    from pony.domain import Contact

    app, _cfg, _paths, _index, _mirrors = build_compose_app(label="tidy-quiet")
    _index.upsert_contact(
        contact=Contact(
            id=None,
            first_name="Alice",
            last_name="Ansell",
            emails=("alice@example.test",),
        )
    )
    app._contacts = _index  # type: ignore[attr-defined]

    async with app.run_test() as pilot:
        await pilot.pause()
        # A contacts-backed composer with a nameless account also opens
        # the contact editor on top, so reach past it for the composer.
        screen = next(s for s in app.screen_stack if isinstance(s, ComposeScreen))
        field = screen.query_one("#to-container", Vertical).query(Input).first()
        field.focus()
        await pilot.pause()

        field.value = "alice@example.test, bob@example.test"
        screen.query_one("#subject-input", Input).focus()
        await pilot.pause()
        await pilot.pause()

        assert _row_values(screen, TO_CONTAINER) == [
            "alice@example.test",
            "bob@example.test",
        ]
        shown = [
            options
            for options in screen.query_one("#to-container", Vertical).query(OptionList)
            if options.display
        ]
        assert shown == [], "a completion list opened under an unfocused field"


async def test_the_attachment_buttons_share_the_recipient_columns() -> None:
    """× and + line up down the whole header, attachments included."""
    app, _cfg, _paths, _index, _mirrors = build_compose_app(
        label="button-alignment", to="a@example.test, b@example.test"
    )
    attachment = Path(_paths.data_dir) / "report.pdf"
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_bytes(b"%PDF-1.4\n")

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = _screen(app)
        screen._attachment_paths = [attachment]
        screen._refresh_attachments_bar()
        await pilot.pause()
        await pilot.pause()

        remove_columns = {
            button.region.x
            for selector in (".addr-remove-btn", ".attach-remove-btn")
            for button in screen.query(selector).results(Button)
        }
        add_columns = {
            button.region.x
            for selector in (".addr-add-btn", ".attach-add-btn")
            for button in screen.query(selector).results(Button)
            if button.visible
        }

        assert len(remove_columns) == 1, f"× columns disagree: {remove_columns}"
        assert len(add_columns) == 1, f"+ columns disagree: {add_columns}"
        # And + sits one cell to the right of ×, not on top of it.
        assert add_columns.pop() > remove_columns.pop()
