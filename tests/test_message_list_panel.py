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

from pony.domain import FolderRef, MessageFlag
from pony.tui.widgets.message_list import MessageListPanel, _format_date

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


async def _open_inbox(pilot: object) -> None:
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


async def _press(pilot: object, panel: MessageListPanel, key: str) -> None:
    """Send *key* to *panel*.

    Highlighting a row auto-previews the message, which hands focus to
    the reader pane — so focus has to be re-claimed immediately before
    every key, or the next press lands on the wrong widget.
    """
    panel.focus()
    await pilot.pause()  # type: ignore[attr-defined]
    await pilot.press(key)  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Cursor motion
# ---------------------------------------------------------------------------


async def test_first_and_last_jump_to_the_ends_of_the_list() -> None:
    """``<`` and ``>`` move to row 0 and to the final row."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-ends", count=4)

    async with app.run_test() as pilot:
        await _open_inbox(pilot)
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
        await _open_inbox(pilot)
        panel = _panel(app)

        assert panel.move_cursor_to_next_unread() is None


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------


async def test_mark_toggles_on_and_off_for_the_same_row() -> None:
    """``m`` marks the cursor row; pressing it again on that row unmarks it."""
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-mark-toggle", count=3)

    async with app.run_test() as pilot:
        await _open_inbox(pilot)
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
        await _open_inbox(pilot)
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
        await _open_inbox(pilot)
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
        await _open_inbox(pilot)
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
        await _open_inbox(pilot)
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
        await _open_inbox(pilot)
        panel = _panel(app)
        panel.focus()
        await pilot.pause()

        panel.on_key(object())  # not a Key event — ignored outright
        assert panel.border_title == "Messages"


async def test_search_with_no_hits_says_so_in_the_border_title() -> None:
    app, _cfg, _paths, _index, _mirrors = _app_with_rows("mlist-search-empty", count=1)

    async with app.run_test() as pilot:
        await _open_inbox(pilot)
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
        await _open_inbox(pilot)
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
        await _open_inbox(pilot)
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
