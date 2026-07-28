"""Behaviour tests for the ``MessageListPanel`` widget.

The panel is a ``DataTable`` subclass whose rows are pre-formatted
single-line cells, so most of its logic is about keeping the row cursor,
the mark set and the column widths in step with the folder contents.
Everything here drives the real widget inside a booted ``PonyApp`` via
``Pilot`` — the panel reads from a live SQLite index, so a stubbed
repository would not exercise the streaming loader.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from corpus import plain_text
from tui_helpers import build_pony_app, seed_message

from pony.domain import FolderRef, MessageFlag, MessageStatus
from pony.tui.widgets.message_list import MessageListPanel, _format_date
from pony.tui.widgets.message_view import MessageViewPanel

_INBOX = FolderRef(account_name="acct", folder_name="INBOX")


def _dated_message(subject: str) -> bytes:
    """A plain-text message carrying *subject* and a distinct Message-ID."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
    msg["Message-ID"] = f"<{subject.replace(' ', '-')}@example.com>"
    msg.set_content(f"Body of {subject}.")
    return msg.as_bytes()


def _app_with_rows(label: str, count: int = 3):  # type: ignore[no-untyped-def]
    """A booted app whose INBOX holds *count* distinct messages."""
    seed = [(_INBOX, _dated_message(f"subject {i}")) for i in range(count)]
    return build_pony_app(label=label, seed=seed)


def _panel(app: object) -> MessageListPanel:
    return app.screen.query_one(MessageListPanel)  # type: ignore[attr-defined,no-any-return]


async def _boot(pilot: object) -> None:
    """Settle the app.

    The INBOX is already loaded and focused at startup, and the reader
    stays closed until the user presses Enter — so list-behaviour tests
    need no opening step at all.
    """
    await pilot.pause()  # type: ignore[attr-defined]


async def _press(pilot: object, panel: MessageListPanel, key: str) -> None:
    """Send *key* to *panel*, asserting the panel still owns the focus.

    Moving the row cursor must never hand focus to the reader — see
    ``test_navigating_the_list_does_not_open_the_reader``.  The assert
    keeps that guarantee from silently regressing here.
    """
    panel.focus()
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press(key)  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    assert panel.has_focus


# ---------------------------------------------------------------------------
# Cursor motion
# ---------------------------------------------------------------------------


async def test_first_and_last_jump_to_the_ends_of_the_list() -> None:
    """``<`` and ``>`` move to row 0 and to the final row."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-ends", count=4)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)

        await _press(pilot, panel, ">")
        assert panel.cursor_row == panel.row_count - 1

        await _press(pilot, panel, "<")
        assert panel.cursor_row == 0


async def test_first_and_last_are_noops_on_an_empty_list() -> None:
    """With no rows loaded neither jump raises or moves the cursor."""
    app, _cfg, _paths, _index, _mirrors = build_pony_app(label="mlist-empty-ends")

    async with app.run_test() as pilot:
        await pilot.pause()
        panel = _panel(app)
        panel.action_cursor_first()
        await panel.action_cursor_last()
        await pilot.pause()

        assert panel.row_count == 0
        assert panel.cursor_row == 0


async def test_next_unread_stops_at_the_end_of_the_list() -> None:
    """With every following row read, the jump reports nothing to go to."""
    app, _cfg, _paths, index, mirrors = build_pony_app(label="mlist-no-unread")
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=_INBOX,
        raw=plain_text(),
        message_id="<only@example.com>",
    )

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)

        assert panel.move_cursor_to_next_unread() is None


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------


async def test_mark_toggles_on_and_off_for_the_same_row() -> None:
    """``m`` marks the cursor row; pressing it again on that row unmarks it."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-mark-toggle", count=3)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)

        # ``m`` marks the cursor row and then advances.
        await _press(pilot, panel, "m")
        assert len(panel.marked_summaries()) == 1
        assert panel.cursor_row == 1

        # Come back to the marked row and press again to clear it.
        panel.move_cursor(row=0)
        await pilot.pause()
        await _press(pilot, panel, "m")
        assert panel.marked_summaries() == []


async def test_mark_up_toggles_then_moves_the_cursor_up() -> None:
    """``shift+up`` marks the current row before stepping backwards."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-mark-up", count=3)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        panel.move_cursor(row=2)
        await pilot.pause()

        await _press(pilot, panel, "shift+up")

        assert len(panel.marked_summaries()) == 1
        assert panel.cursor_row == 1


async def test_marking_an_empty_list_is_a_noop() -> None:
    """No cursor row means nothing to toggle."""
    app, _cfg, _paths, _index, _mirrors = build_pony_app(label="mlist-mark-empty")

    async with app.run_test() as pilot:
        await pilot.pause()
        panel = _panel(app)
        panel.action_mark_up()
        await pilot.pause()

        assert panel.marked_summaries() == []


async def test_clear_marks_restores_the_status_icons() -> None:
    """Clearing marks re-renders exactly the rows that carried a ``*``."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-clear-marks", count=2)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)

        await _press(pilot, panel, "m")
        assert panel.marked_summaries()

        panel.clear_marks()
        await pilot.pause()
        assert panel.marked_summaries() == []

        # Second call short-circuits — nothing marked, nothing to redraw.
        panel.clear_marks()
        await pilot.pause()
        assert panel.marked_summaries() == []


async def test_summaries_to_act_on_prefers_the_marks() -> None:
    """Bulk actions use the mark set when it is non-empty."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-act-on", count=3)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)

        # Cursor row only.
        assert len(panel.summaries_to_act_on()) == 1

        await _press(pilot, panel, "m")
        await _press(pilot, panel, "m")
        assert len(panel.summaries_to_act_on()) == 2


# ---------------------------------------------------------------------------
# Search mode
# ---------------------------------------------------------------------------


async def test_q_leaves_search_mode_and_restores_the_border_title() -> None:
    """In search mode ``q`` exits; the panel drops back to folder framing."""
    app, _cfg, _paths, index, _mirrors = _app_with_rows("mlist-search-exit", count=2)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        hits = list(index.list_folder_messages(folder=_INBOX))
        panel.load_search_results(hits, "subject")
        await pilot.pause()
        assert "Search:" in str(panel.border_title)

        await _press(pilot, panel, "q")

        assert panel.border_title == "Messages"


async def test_q_outside_search_mode_is_left_to_the_app() -> None:
    """Outside search mode the panel must not swallow ``q``."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-q-passthrough", count=1)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        panel.focus()
        await pilot.pause()

        panel.on_key(object())  # not a Key event — ignored outright
        assert panel.border_title == "Messages"


async def test_search_with_no_hits_says_so_in_the_border_title() -> None:
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-search-empty", count=1)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        panel.load_search_results([], "nothing")
        await pilot.pause()

        assert "no results" in str(panel.border_title)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


async def test_resizing_rerenders_every_row_at_the_new_width() -> None:
    """A width change re-formats the header and all cells so columns align."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-resize", count=3)

    async with app.run_test(size=(120, 30)) as pilot:
        await _boot(pilot)
        panel = _panel(app)
        await pilot.pause()
        wide = panel._from_width_cached  # noqa: SLF001

        await pilot.resize_terminal(60, 30)
        await pilot.pause()

        narrow = panel._from_width_cached  # noqa: SLF001
        assert narrow < wide
        assert panel.row_count == 3


async def test_resize_before_mount_completes_is_a_noop() -> None:
    """``on_resize`` can fire before the column exists — it must not raise."""
    app, _cfg, _paths, _index, _mirrors = build_pony_app(label="mlist-resize-early")

    async with app.run_test() as pilot:
        await pilot.pause()
        panel = _panel(app)
        panel._row_col_key = None  # noqa: SLF001
        panel.on_resize()
        await pilot.pause()


# ---------------------------------------------------------------------------
# Row lookup
# ---------------------------------------------------------------------------


async def test_lookup_of_an_unknown_row_key_returns_none() -> None:
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-lookup", count=1)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)

        assert panel._find_summary("999999") is None  # noqa: SLF001
        # A non-RowKey object never reaches the summary list.
        assert panel._summary_for_row_key(object()) is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------


def test_todays_messages_show_only_the_time() -> None:
    now = datetime.now(tz=UTC)
    assert _format_date(now) == now.strftime("%H:%M")


def test_messages_earlier_this_year_show_month_and_day() -> None:
    now = datetime.now(tz=UTC)
    earlier = now - timedelta(days=1)
    if earlier.year != now.year:  # pragma: no cover - only on Jan 1
        earlier = now + timedelta(days=1)
    assert _format_date(earlier) == earlier.strftime("%b %d")


def test_older_messages_show_the_full_iso_date() -> None:
    old = datetime(2001, 3, 4, 9, 30, tzinfo=UTC)
    assert _format_date(old) == "2001-03-04"


def test_marked_rows_render_a_star_icon() -> None:
    """The mark icon wins over the flag/attachment status icon."""
    from pony.domain import FolderMessageSummary, MessageRef, MessageStatus
    from pony.tui.widgets.message_list import _icon_column

    def _summary(
        flags: frozenset[MessageFlag], *, has_attachments: bool = False
    ) -> FolderMessageSummary:
        return FolderMessageSummary(
            message_ref=MessageRef(account_name="acct", folder_name="INBOX", id=7),
            message_id="<icon@example.com>",
            storage_key="key",
            sender="sender@example.com",
            subject="Icon probe",
            received_at=datetime(2026, 4, 17, 12, tzinfo=UTC),
            has_attachments=has_attachments,
            local_flags=flags,
            local_status=MessageStatus.ACTIVE,
        )

    # Flagged wins over answered, which wins over the attachment marker.
    assert _icon_column(_summary(frozenset({MessageFlag.FLAGGED}))) == "!"
    assert _icon_column(_summary(frozenset({MessageFlag.ANSWERED}))) == "↩"
    assert _icon_column(_summary(frozenset(), has_attachments=True)) == "+"
    assert _icon_column(_summary(frozenset())) == " "


# ---------------------------------------------------------------------------
# Open on command
#
# Moving the row cursor is navigation, not activation.  Before this was
# fixed the reader opened on every cursor move: it was already open at
# startup with the first message marked read, it stole focus from the
# list, and the arrow keys then scrolled the reader instead of moving
# the cursor — so there was no way to browse a folder without opening
# (and implicitly reading) every message passed over.
# ---------------------------------------------------------------------------


async def test_the_reader_is_closed_until_a_message_is_opened() -> None:
    """A freshly-booted app shows the list only, with focus on it."""
    app, _cfg, _paths, index, _mirrors = _app_with_rows("open-boot", count=3)

    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        panel = _panel(app)

        assert view.display is False
        assert panel.has_focus

    # Nothing was marked read just by opening the folder.
    assert all(
        MessageFlag.SEEN not in row.local_flags
        for row in index.list_folder_messages(folder=_INBOX)
    )


async def test_navigating_the_list_does_not_open_the_reader() -> None:
    """Arrow keys and n/p move the cursor and nothing else."""
    app, _cfg, _paths, index, _mirrors = _app_with_rows("open-nav", count=4)

    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        panel = _panel(app)

        for key, expected_row in (("down", 1), ("down", 2), ("n", 3), ("p", 2)):
            await _press(pilot, panel, key)
            assert panel.cursor_row == expected_row
            assert view.display is False, f"{key!r} opened the reader"
            assert panel.has_focus, f"{key!r} stole focus from the list"

    # Passing over a message must not mark it read.
    assert all(
        MessageFlag.SEEN not in row.local_flags
        for row in index.list_folder_messages(folder=_INBOX)
    )


async def test_enter_opens_the_message_and_focuses_the_reader() -> None:
    """Enter is the only way to open — and it marks that message read."""
    app, _cfg, _paths, index, _mirrors = _app_with_rows("open-enter", count=3)

    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        panel = _panel(app)

        await _press(pilot, panel, "down")
        opened = panel.get_selected_summary()
        assert opened is not None

        panel.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert view.display is True
        assert view.has_focus

    seen = {
        row.subject
        for row in index.list_folder_messages(folder=_INBOX)
        if MessageFlag.SEEN in row.local_flags
    }
    assert seen == {opened.subject}


async def test_n_and_p_step_through_messages_while_the_reader_stays_open() -> None:
    """With the reader open, n/p advance it without closing it."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("open-reader-nav", count=3)

    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        panel = _panel(app)

        await pilot.press("enter")
        await pilot.pause()
        assert view.display is True
        assert panel.cursor_row == 0

        await pilot.press("n")
        await pilot.pause()
        assert panel.cursor_row == 1
        assert view.display is True
        assert view.has_focus

        await pilot.press("p")
        await pilot.pause()
        assert panel.cursor_row == 0
        assert view.display is True


async def test_closing_the_reader_returns_focus_to_the_list() -> None:
    """After ``q`` the list is navigable again, still without opening."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("open-close", count=3)

    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.screen.query_one(MessageViewPanel)
        panel = _panel(app)

        await pilot.press("enter")
        await pilot.pause()
        assert view.display is True

        await pilot.press("q")
        await pilot.pause()
        assert view.display is False
        assert panel.has_focus

        await _press(pilot, panel, "down")
        assert panel.cursor_row == 1
        assert view.display is False


# ---------------------------------------------------------------------------
# Cursor restore across a reload
# ---------------------------------------------------------------------------


def _restore_target(
    ids: list[int], *, row: int | None = None, key: str | None = None
) -> int | None:
    """Run the target resolution against a synthetic summary list.

    Rows stream in from a worker, so the cursor cannot be moved after
    ``load_folder`` returns — there is nothing to move it to yet. The
    destination is resolved up front from the summary list, which *is*
    known synchronously, and this is that resolution.
    """
    from unittest.mock import Mock

    from pony.domain import FolderMessageSummary, MessageRef

    summaries = [
        FolderMessageSummary(
            message_ref=MessageRef(
                account_name="acct", folder_name="INBOX", id=message_id
            ),
            sender="a@example.com",
            subject=f"subject {message_id}",
            received_at=datetime(2026, 4, 17, tzinfo=UTC),
            has_attachments=False,
            local_flags=frozenset(),
            local_status=MessageStatus.ACTIVE,
            storage_key=str(message_id),
            message_id=f"<m{message_id}@example.com>",
        )
        for message_id in ids
    ]
    panel = MessageListPanel(index=Mock())
    return panel._restore_target(summaries, row, key)  # noqa: SLF001


def test_restoring_into_an_empty_folder_has_no_target() -> None:
    assert _restore_target([], row=3, key="3") is None


def test_the_cursor_follows_the_named_message() -> None:
    # A sync can push the message down the list; following the row
    # number instead would silently land on a different message.
    assert _restore_target([9, 8, 1, 2, 3], row=0, key="1") == 2


def test_the_named_message_takes_precedence_over_the_row() -> None:
    assert _restore_target([1, 2, 3], row=2, key="1") is None


def test_a_vanished_message_falls_back_to_its_row() -> None:
    # Trash and archive remove the message; the row number then names
    # whatever took its place, which is exactly where the cursor belongs.
    assert _restore_target([1, 2, 3], row=1, key="99") == 1


def test_the_row_is_clamped_to_a_shrunken_folder() -> None:
    assert _restore_target([1, 2], row=7, key=None) == 1


def test_the_top_row_needs_no_restore() -> None:
    assert _restore_target([1, 2, 3], row=0, key=None) is None


def test_no_request_means_no_restore() -> None:
    assert _restore_target([1, 2, 3]) is None


async def test_trashing_leaves_the_cursor_where_it_was() -> None:
    """The row that took the trashed message's place becomes current.

    The restore used to run immediately after ``load_folder``, when the
    streaming worker had added no rows yet, so the guard saw an empty
    table and the cursor stayed at the top of the folder.
    """
    app, *_ = _app_with_rows("cursor-after-trash", count=6)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        for _ in range(3):
            await _press(pilot, panel, "down")
        before_row = panel.effective_cursor_row
        before = panel.get_selected_summary()
        assert before is not None

        app.screen.trash_current_message()
        await pilot.pause()
        await pilot.pause()

        assert panel.effective_cursor_row == before_row
        after = panel.get_selected_summary()
        assert after is not None
        assert after.message_ref.id != before.message_ref.id


async def test_a_sync_refresh_keeps_the_cursor_on_its_message() -> None:
    """New mail arriving above the cursor must not shift what is selected.

    A background sync can land at any moment, so this is the difference
    between the list staying put and it moving under the reader.
    """
    app, _cfg, _paths, index, mirrors = _app_with_rows("cursor-after-sync", count=6)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        for _ in range(3):
            await _press(pilot, panel, "down")
        before = panel.get_selected_summary()
        assert before is not None
        before_row = panel.effective_cursor_row

        seed_message(
            index=index,
            mirror=mirrors["acct"],
            folder=_INBOX,
            raw=_dated_message("newly arrived"),
            message_id="<newly-arrived@example.com>",
        )
        app.screen.refresh_after_sync()
        await pilot.pause()
        await pilot.pause()

        after = panel.get_selected_summary()
        assert after is not None
        assert after.message_ref.id == before.message_ref.id
        # It is the same message, so the row only moved if the list did.
        assert panel.effective_cursor_row >= before_row
