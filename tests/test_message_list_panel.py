"""Behaviour tests for the ``MessageListPanel`` widget.

The panel is a ``DataTable`` subclass whose rows are pre-formatted
single-line cells, so most of its logic is about keeping the row cursor,
the mark set and the column widths in step with the folder contents.
Everything here drives the real widget inside a booted ``PonyApp`` via
``Pilot`` — the panel reads from a live SQLite index, so a stubbed
repository would not exercise the streaming loader.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from corpus import plain_text
from tui_helpers import build_pony_app, seed_message

from pony.mail.domain import FolderRef, MessageFlag, MessageStatus
from pony.tui.mail.screens.main_screen import MainScreen
from pony.tui.mail.widgets.message_list import MessageListPanel
from pony.tui.mail.widgets.message_view import MessageViewPanel

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


def _main(app: object) -> MainScreen:
    """The mail screen, typed, so screen-level actions can be called."""
    screen = app.screen  # type: ignore[attr-defined]
    assert isinstance(screen, MainScreen)
    return screen


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
        panel.load_search("subject", fetch=lambda: hits)
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
        panel.load_search("nothing", fetch=lambda: [])
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
        wide = panel._who_width_cached  # noqa: SLF001

        await pilot.resize_terminal(60, 30)
        await pilot.pause()

        narrow = panel._who_width_cached  # noqa: SLF001
        assert narrow < wide
        assert panel.row_count == 3


async def test_resize_before_mount_completes_is_a_noop() -> None:
    """``on_resize`` can fire before the column exists — it must not raise."""
    app, _cfg, _paths, _index, _mirrors = build_pony_app(label="mlist-resize-early")

    async with app.run_test() as pilot:
        await pilot.pause()
        panel = _panel(app)
        panel._row_col_key = None  # noqa: SLF001
        # A width change is what gets the repaint as far as the column.
        panel._who_width_cached = 0  # noqa: SLF001
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
# Awaiting rows
# ---------------------------------------------------------------------------


def _watch_thread(index: object, method: str) -> list[object]:
    """Record the thread each call to *method* runs on, and delegate."""
    import threading

    threads: list[object] = []
    real = getattr(index, method)

    def recording(**kwargs: object) -> object:
        threads.append(threading.current_thread())
        return real(**kwargs)

    setattr(index, method, recording)
    return threads


async def test_the_folder_query_runs_off_the_event_loop() -> None:
    """The read, not just the row insertion, is in the worker.

    ``list_folder_message_summaries`` ran here on the event loop, with
    no limit on it, while ``docs/architecture.md`` claimed a worker that
    only ``add_row`` had: opening a ten-thousand-row folder froze the
    terminal for as long as SQLite took. The index opens one connection
    per thread, which is what makes this safe.
    """
    import threading

    app, _cfg, _paths, index, _mirrors = _app_with_rows("mlist-off-loop", count=3)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        loop_thread = threading.current_thread()
        ran_on = _watch_thread(index, "list_folder_message_summaries")

        panel.load_folder(_INBOX)
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert ran_on, "the folder was never read"
        assert loop_thread not in ran_on
        assert panel.row_count == 3


async def test_a_list_still_loading_says_so_and_has_nothing_to_act_on() -> None:
    """Between asking for a folder and the rows: one honest state.

    Nothing is selected, nothing can be acted on, and the border says
    why -- a folder read with no sign of it is a keypress that looks
    ignored, which is what moving the read off the loop would otherwise
    buy.
    """
    app, *_ = _app_with_rows("mlist-awaiting", count=3)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)

        panel.load_folder(_INBOX)

        # Synchronously after the call: the worker cannot have run.
        assert panel.awaiting_rows is True
        assert panel.get_selected_summary() is None
        assert panel.summaries_to_act_on() == []
        assert panel.border_title == "Messages  (loading…)"

        await panel.wait_for_load_complete()
        await pilot.pause()

        assert panel.awaiting_rows is False
        assert panel.get_selected_summary() is not None
        assert panel.border_title == "Messages"


async def test_the_search_runs_off_the_event_loop_and_says_it_is_running() -> None:
    """The slowest read the reader can ask for, in the same worker.

    ``index_store`` records 385 ms for a search; it ran on the loop,
    with the folder it was leaving still on screen. The screen hands
    the panel the read rather than its result, so one worker runs both
    kinds of load and one await covers them.
    """
    import threading

    app, _cfg, _paths, index, _mirrors = _app_with_rows("mlist-search-loop", count=3)

    async with app.run_test() as pilot:
        await _boot(pilot)
        screen = _main(app)
        panel = _panel(app)
        loop_thread = threading.current_thread()
        ran_on = _watch_thread(index, "search")

        screen._run_search("subject")

        assert panel.awaiting_rows is True
        assert panel.border_title == "Search: subject  (searching…)"

        await panel.wait_for_load_complete()
        await pilot.pause()

        assert ran_on, "the search never ran"
        assert loop_thread not in ran_on
        assert panel.in_search is True
        # The title says the search is done rather than running. The
        # `[q=exit]` it also carries is console markup by the time it
        # comes back out, so it is not what this compares.
        assert "searching" not in str(panel.border_title)
        assert str(panel.border_title).startswith("Search: subject")
        assert panel.row_count == 3


async def test_the_reader_follows_the_cursor_once_the_rows_land() -> None:
    """Trashing the open message opens whatever took its place.

    The screen used to re-sync the reader on the line after asking for
    the reload, when the panel still held the old rows. It now waits
    for ``RowsReady``; without that wait the pane would be cleared and
    hidden on every action, because the panel has no rows yet.
    """
    app, *_ = _app_with_rows("mlist-reader-follows", count=4)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        view = app.screen.query_one(MessageViewPanel)

        await _press(pilot, panel, "down")
        panel.focus()
        await pilot.press("enter")
        await pilot.pause()
        opened = view._rendered
        assert opened is not None

        _main(app).trash_current_message()
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert view.display is True
        assert view._rendered is not None
        assert view._rendered.subject != opened.subject


async def test_a_read_that_fails_leaves_the_list_usable() -> None:
    """A load that raises still ends the awaiting state.

    It did not: ``_rows_known`` was set after the read, so a query that
    raised left the panel waiting for rows that were never coming --
    the border saying it was loading and every action answering "the
    list is still loading" for the rest of the session. A failure is
    now an empty list, a border that says so, one notice, and a panel
    the next load can use.
    """
    app, _cfg, _paths, index, _mirrors = _app_with_rows("mlist-read-fails", count=3)

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        notices: list[str] = []

        # Recorded rather than forwarded: a toast on screen is not what
        # this is about, and the real one outlives the test.
        def capture(message: str, **_kwargs: object) -> None:
            notices.append(message)

        app.notify = capture  # type: ignore[method-assign]
        real = index.list_folder_message_summaries

        def exploding(**_kwargs: object) -> list[object]:
            raise OSError("the index went away")

        index.list_folder_message_summaries = exploding  # type: ignore[method-assign]
        panel.load_folder(_INBOX)
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert panel.awaiting_rows is False
        assert panel.border_title == "Messages  (could not load)"
        assert panel.summaries_to_act_on() == []
        assert "Could not load the message list." in notices

        # And the panel recovers: the next load is an ordinary one.
        index.list_folder_message_summaries = real  # type: ignore[method-assign]
        panel.load_folder(_INBOX)
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert panel.awaiting_rows is False
        assert panel.border_title == "Messages"
        assert panel.row_count == 3


# ---------------------------------------------------------------------------
# Cursor restore across a reload
# ---------------------------------------------------------------------------


def _restore_target(
    ids: list[int], *, row: int | None = None, key: str | None = None
) -> int | None:
    """Run the target resolution against a synthetic summary list.

    Rows stream in from a worker, so the cursor cannot be moved after
    ``load_folder`` returns — there is nothing to move it to yet, and
    until the query answers there are no rows at all. The destination
    is resolved from the summary list the moment it arrives, before any
    row is inserted, and this is that resolution.
    """
    from unittest.mock import Mock

    from pony.mail.domain import FolderMessageSummary, MessageRef

    summaries = [
        FolderMessageSummary(
            message_ref=MessageRef(
                account_name="acct", folder_name="INBOX", id=message_id
            ),
            sender="a@example.com",
            recipients="me@example.com",
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

        _main(app).trash_current_message()
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
        _main(app).refresh_after_sync()
        await pilot.pause()
        await pilot.pause()

        after = panel.get_selected_summary()
        assert after is not None
        assert after.message_ref.id == before.message_ref.id
        # It is the same message, so the row only moved if the list did.
        assert panel.effective_cursor_row >= before_row


# ---------------------------------------------------------------------------
# Sent folders
#
# Every message in a Sent folder is from the user, so a From column there
# repeats one address down the whole list.  The recipient is what tells
# the rows apart, so that is what the column shows.
# ---------------------------------------------------------------------------

_SENT = FolderRef(account_name="acct", folder_name="Enviados")


def _outgoing_message(to: str, subject: str) -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "me@example.com"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
    msg["Message-ID"] = f"<{subject.replace(' ', '-')}@example.com>"
    msg.set_content(f"Body of {subject}.")
    return msg.as_bytes()


def _row_text(panel: MessageListPanel, row: int) -> str:
    from rich.text import Text

    cell = panel.get_row_at(row)[0]
    return cell.plain if isinstance(cell, Text) else str(cell)


def _header_text(panel: MessageListPanel) -> str:
    from rich.text import Text

    assert panel._row_col_key is not None  # noqa: SLF001
    label = panel.columns[panel._row_col_key].label  # noqa: SLF001
    return label.plain if isinstance(label, Text) else str(label)


async def test_selecting_a_sent_folder_lists_recipients() -> None:
    """A localised Sent folder shows To, and the rows follow the header."""
    from pony.tui.mail.widgets.folder_panel import FolderPanel

    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="mlist-sent",
        seed=[
            (_INBOX, _dated_message("inbound")),
            (_SENT, _outgoing_message("alice@example.com", "outbound")),
        ],
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await _boot(pilot)
        panel = _panel(app)
        assert "From" in _header_text(panel)
        assert "sender@example.com" in _row_text(panel, 0)

        _main(app).on_folder_panel_folder_selected(
            FolderPanel.FolderSelected(folder_ref=_SENT)
        )
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert "To" in _header_text(panel)
        assert "From" not in _header_text(panel)
        assert "alice@example.com" in _row_text(panel, 0)
        assert "me@example.com" not in _row_text(panel, 0)


async def test_leaving_a_sent_folder_restores_the_from_column() -> None:
    """The column follows the folder, both on the way in and on the way out."""
    from pony.tui.mail.widgets.folder_panel import FolderPanel

    app, _cfg, _paths, _index, _mirrors = build_pony_app(
        label="mlist-sent-back",
        seed=[
            (_INBOX, _dated_message("inbound")),
            (_SENT, _outgoing_message("alice@example.com", "outbound")),
        ],
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await _boot(pilot)
        screen = _main(app)
        panel = _panel(app)
        screen.on_folder_panel_folder_selected(
            FolderPanel.FolderSelected(folder_ref=_SENT)
        )
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert "To" in _header_text(panel)

        screen.on_folder_panel_folder_selected(
            FolderPanel.FolderSelected(folder_ref=_INBOX)
        )
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert "From" in _header_text(panel)
        assert "sender@example.com" in _row_text(panel, 0)


async def test_a_configured_sent_folder_overrides_the_name_match() -> None:
    """``sent_folder`` decides it when set, whatever the folder is called."""
    import dataclasses

    from pony.tui.mail.widgets.folder_panel import FolderPanel

    archive = FolderRef(account_name="acct", folder_name="Outgoing")
    app, cfg, _paths, _index, _mirrors = build_pony_app(
        label="mlist-sent-config",
        seed=[
            (_SENT, _outgoing_message("alice@example.com", "outbound")),
            (archive, _outgoing_message("bob@example.com", "archived")),
        ],
    )
    accounts = [dataclasses.replace(cfg.accounts[0], sent_folder="Outgoing")]
    app._config = dataclasses.replace(cfg, accounts=accounts)

    async with app.run_test(size=(120, 30)) as pilot:
        await _boot(pilot)
        screen = _main(app)
        screen._config = app._config
        panel = _panel(app)

        # The override names "Outgoing", so "Enviados" is an ordinary folder.
        screen.on_folder_panel_folder_selected(
            FolderPanel.FolderSelected(folder_ref=_SENT)
        )
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert "From" in _header_text(panel)

        screen.on_folder_panel_folder_selected(
            FolderPanel.FolderSelected(folder_ref=archive)
        )
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert "To" in _header_text(panel)
        assert "bob@example.com" in _row_text(panel, 0)


async def test_search_results_show_senders_even_from_a_sent_folder() -> None:
    """Results span folders, so From is the column that always means something."""
    from pony.tui.mail.widgets.folder_panel import FolderPanel

    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="mlist-sent-search",
        seed=[(_SENT, _outgoing_message("alice@example.com", "outbound"))],
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await _boot(pilot)
        panel = _panel(app)
        _main(app).on_folder_panel_folder_selected(
            FolderPanel.FolderSelected(folder_ref=_SENT)
        )
        await panel.wait_for_load_complete()
        await pilot.pause()
        assert "To" in _header_text(panel)

        results = list(index.list_folder_messages(folder=_SENT))
        panel.load_search("outbound", fetch=lambda: results)
        await panel.wait_for_load_complete()
        await pilot.pause()

        assert "From" in _header_text(panel)
        assert "me@example.com" in _row_text(panel, 0)


async def test_flagging_a_message_repaints_its_row() -> None:
    """The mark the user pressed for has to appear without a reload.

    ``FLAGGED_MARK`` and ``ANSWERED_MARK`` were asserted only in
    ``tests/test_views.py``, against the projection -- nothing checked
    that a flag change reaches the row on screen.  The archive case in
    ``test_trashing_leaves_the_cursor_where_it_was`` above is the
    template this follows.
    """
    from pony.mail.views import ANSWERED_MARK, FLAGGED_MARK

    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="mlist-flag-repaint",
        seed=[(_INBOX, _dated_message("flag me"))],
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await _boot(pilot)
        panel = _panel(app)
        assert FLAGGED_MARK not in _row_text(panel, 0)

        _main(app).toggle_flag(MessageFlag.FLAGGED)
        await pilot.pause()
        flagged_row = _row_text(panel, 0)

        _main(app).toggle_flag(MessageFlag.FLAGGED)
        await pilot.pause()
        unflagged_row = _row_text(panel, 0)

        _main(app).set_flag(MessageFlag.ANSWERED, present=True)
        await pilot.pause()
        answered_row = _row_text(panel, 0)

        stored = index.list_folder_messages(folder=_INBOX)

    assert FLAGGED_MARK in flagged_row
    assert FLAGGED_MARK not in unflagged_row
    assert ANSWERED_MARK in answered_row
    assert [MessageFlag.ANSWERED in row.local_flags for row in stored] == [True]


async def test_marking_a_message_unread_repaints_the_row_as_unread() -> None:
    """Unread is emphasis on the row, so the press has to repaint it."""
    app, *_ = build_pony_app(
        label="mlist-unread-repaint",
        seed=[(_INBOX, _dated_message("read me"))],
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await _boot(pilot)
        panel = _panel(app)
        panel.focus()
        await pilot.pause()
        # Opening the reader is what sets SEEN, and it takes the focus,
        # so this cannot go through the focus-checking `_press` helper.
        await pilot.press("enter")
        await pilot.pause()
        opened = panel.get_selected_summary()

        _main(app).set_flag(MessageFlag.SEEN, present=False)
        await pilot.pause()
        after = panel.get_selected_summary()

    assert opened is not None
    assert MessageFlag.SEEN in opened.local_flags
    # The summary the panel holds is what every row style reads from.
    assert after is not None
    assert MessageFlag.SEEN not in after.local_flags


async def test_archiving_leaves_the_cursor_where_it_was() -> None:
    """Archive takes the same path as trash, and had no test saying so."""
    from tui_helpers import make_test_account, make_tmp_paths

    paths = make_tmp_paths("cursor-after-archive")
    account = make_test_account(paths, archive_folder="Archive")
    app, _cfg, _paths, _index, mirrors = build_pony_app(
        label="cursor-after-archive-app",
        accounts=(account,),
        seed=[(_INBOX, _dated_message(f"subject {i}")) for i in range(6)],
    )
    mirrors["acct"].create_folder(account_name="acct", folder_name="Archive")

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        for _ in range(3):
            await _press(pilot, panel, "down")
        before_row = panel.effective_cursor_row
        before = panel.get_selected_summary()
        assert before is not None

        _main(app).archive_current_message()
        await pilot.pause()
        await pilot.pause()

        assert panel.effective_cursor_row == before_row
        after = panel.get_selected_summary()
        assert after is not None
        assert after.message_ref.id != before.message_ref.id


async def test_moving_leaves_the_cursor_where_it_was() -> None:
    """A move empties the row under the cursor just as archive does."""
    app, _cfg, _paths, index, mirrors = build_pony_app(
        label="cursor-after-move",
        seed=[(_INBOX, _dated_message(f"subject {i}")) for i in range(6)],
    )
    target = FolderRef(account_name="acct", folder_name="Later")
    mirrors["acct"].create_folder(account_name="acct", folder_name="Later")

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        for _ in range(2):
            await _press(pilot, panel, "down")
        before_row = panel.effective_cursor_row
        before = panel.get_selected_summary()
        assert before is not None
        moved = index.get_message(message_ref=before.message_ref)
        assert moved is not None

        _main(app)._move_to_folder([moved], target)
        await pilot.pause()
        await pilot.pause()

        assert panel.effective_cursor_row == before_row
        after = panel.get_selected_summary()
        assert after is not None
        assert after.message_ref.id != before.message_ref.id


async def test_n_and_space_move_from_where_the_cursor_is_going() -> None:
    """Mid-reload, `cursor_row` is 0 and the destination is pending.

    `effective_cursor_row` exists for exactly that window -- its
    docstring says every caller asking "which message is selected" has
    to see the destination -- and these two read `cursor_row` instead.
    They are reached by ++n++, ++p++ and ++space++, which is what a
    reader presses right after a trash, while the rows are still
    arriving.
    """
    app, *_ = build_pony_app(
        label="cursor-mid-reload",
        seed=[(_INBOX, _dated_message(f"subject {i}")) for i in range(6)],
    )

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        rows = [summary.message_ref.id for summary in panel._summaries]  # noqa: SLF001
        # The state `_load_rows` is in between two batches: the real
        # cursor has not moved yet and the row it is heading for is
        # recorded.
        panel._pending_cursor = 3  # noqa: SLF001
        assert panel.cursor_row == 0
        assert panel.effective_cursor_row == 3

        stepped = panel.move_cursor_by(1)
        assert stepped is not None
        assert stepped.message_ref.id == rows[4]

        panel._pending_cursor = 3  # noqa: SLF001
        # Everything after the pending row is read, so there is nothing
        # to jump to; from row 0 it would have found row 1.
        for index in (4, 5):
            panel._summaries[index] = dataclasses.replace(  # noqa: SLF001
                panel._summaries[index],  # noqa: SLF001
                local_flags=frozenset({MessageFlag.SEEN}),
            )
        assert panel.move_cursor_to_next_unread() is None


async def test_a_marked_multi_row_action_keeps_the_cursor_on_its_message() -> None:
    """Marks can take several rows out from above the cursor at once.

    The rows are gone, so the cursor cannot stay on the same *row*; it
    has to follow the message it was on, which moved up the list.  The
    one cursor test there was trashed the row the cursor sat on, which
    is the other half of ``_restore_target``.
    """
    app, *_ = build_pony_app(
        label="cursor-after-marked-trash",
        seed=[(_INBOX, _dated_message(f"subject {i}")) for i in range(6)],
    )

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        for _ in range(2):
            await _press(pilot, panel, "down")
        # shift+down marks the row and steps on, twice, so rows 2 and 3
        # are marked and the cursor has moved past them to row 4.
        await _press(pilot, panel, "shift+down")
        await _press(pilot, panel, "shift+down")
        marked = [summary.message_ref.id for summary in panel.marked_summaries()]
        before_row = panel.effective_cursor_row
        before = panel.get_selected_summary()
        assert len(marked) == 2
        assert before_row == 4
        assert before is not None
        assert before.message_ref.id not in marked

        _main(app).trash_current_message()
        await pilot.pause()
        await pilot.pause()

        assert panel.marked_summaries() == []
        after = panel.get_selected_summary()
        assert after is not None
        # Same message, two rows further up now that the marks are gone.
        assert after.message_ref.id == before.message_ref.id
        assert panel.effective_cursor_row == before_row - 2


async def test_search_results_keep_the_order_they_arrive_in() -> None:
    """The store's ``ORDER BY`` is the order, and the only one.

    The panel sorted the hits by date itself -- a second definition of
    "newest first", which would quietly undo any other order a caller
    asked for and which nothing but reading both could keep in step.
    """
    from email.message import EmailMessage

    def _on(day: int) -> bytes:
        msg = EmailMessage()
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Subject"] = f"subject {day}"
        msg["Date"] = f"Fri, {day} Apr 2026 12:00:00 +0000"
        msg["Message-ID"] = f"<day-{day}@example.com>"
        msg.set_content("body")
        return msg.as_bytes()

    app, _cfg, _paths, index, _mirrors = build_pony_app(
        label="mlist-search-order",
        seed=[(_INBOX, _on(day)) for day in (17, 18, 19)],
    )

    async with app.run_test() as pilot:
        await _boot(pilot)
        panel = _panel(app)
        hits = list(index.list_folder_messages(folder=_INBOX))
        # Deliberately not newest-first: whatever the caller hands over.
        reordered = sorted(hits, key=lambda m: m.received_at)
        panel.load_search("subject", fetch=lambda: reordered)
        await pilot.pause()

        assert [s.message_ref.id for s in panel._summaries] == [  # noqa: SLF001
            m.message_ref.id for m in reordered
        ]
