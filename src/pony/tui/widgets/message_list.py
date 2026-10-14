"""Message list panel — top-right pane showing messages in the selected folder."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text
from textual import work
from textual.message import Message
from textual.widgets import DataTable
from textual.widgets._data_table import ColumnKey
from textual.worker import Worker

from ...domain import (
    FolderMessageSummary,
    FolderRef,
    IndexedMessage,
    MessageFlag,
)
from ...protocols import IndexRepository
from ...views import ROW_BATCH, correspondent_label, message_row, summary_of
from ..bindings import MARK_BINDINGS, MOTION_BINDINGS
from .edge_drag import DraggableEdgeMixin

_log = logging.getLogger(__name__)

# Width of the date cell. Widest format produced by ``views.format_received``
# is ``YYYY-MM-DD`` (10 chars).
_DATE_WIDTH = 10


@dataclass(frozen=True, slots=True)
class _Titles:
    """What the panel's border says at each stage of one load.

    Four, because a load has four outcomes a reader can tell apart:
    running, done, done with nothing in it, and failed.  The last one
    is why they travel together -- a load that raised used to leave the
    border saying it was still running, forever.
    """

    loading: str
    ready: str
    failed: str
    empty: str | None = None


class MessageListPanel(DraggableEdgeMixin, DataTable[Text | str]):
    """Single-column list of messages for the currently selected folder.

    The whole row is rendered as one pre-formatted line:
    ``<icon> <date> <who> <subject>`` with fixed-width icon, date, and
    correspondent fields.  ``<who>`` is the sender, except in a Sent
    folder, where every message is from the user and the recipient is
    the only thing that tells one row from another.  Seen rows are
    returned as plain ``str`` and inherit the widget's
    ``text-style: dim`` from CSS — the cheap path, since read messages
    dominate.  Unseen rows allocate one ``Text`` styled
    ``not dim`` so they render at full brightness.  Posts
    ``MessageListPanel.MessageSelected`` when a row is *activated*
    (Enter or click) — never when the cursor merely moves over it.
    Arrow keys and ``n``/``p`` navigate the list without opening
    anything; once a message is open, ``n``/``p`` in the reader step
    through messages and keep it open.

    The panel holds ``FolderMessageSummary`` rows — a narrow
    projection of ``IndexedMessage`` that skips the datetime and
    flag-set parsing the full object requires.  Callers that need
    the full ``IndexedMessage`` (e.g. to rewrite it via ``upsert_message``
    in a flag/status action, or to render a reply body) re-fetch it
    from the index using ``summary.message_ref``.
    """

    DEFAULT_CSS = """
    MessageListPanel {
        text-style: dim;
    }
    MessageListPanel > .datatable--header {
        text-style: not dim;
    }
    """

    BORDER_TITLE = "Messages"

    # The bottom border doubles as the resize handle for this pane.
    DRAG_EDGE = "bottom"

    # In the table's own order, so `tests/test_keymap.py` can compare the
    # whole list rather than a set and notice an extra entry.
    BINDINGS = [
        *MARK_BINDINGS,
        *MOTION_BINDINGS,
    ]

    @dataclass
    class MessageSelected(Message):
        """Posted when the user activates a message row."""

        summary: FolderMessageSummary

    @dataclass
    class SearchExitRequested(Message):
        """Posted when the user presses q or escape in search-results mode."""

    @dataclass
    class RowsReady(Message):
        """Posted once a load knows which rows the panel is showing.

        The rows are fetched off the event loop, so between asking for
        a folder and this there is nothing to select and nothing to act
        on.  Anything that has to read the panel back after reloading
        it -- the reader pane, which has to follow the cursor onto
        whatever took a trashed message's place -- waits for this
        instead of reading it on the next line.
        """

    _LOAD_BATCH = ROW_BATCH

    def __init__(self, index: IndexRepository, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._index = index
        self._summaries: list[FolderMessageSummary] = []
        self._marked: set[str] = set()  # str(message_ref.id)
        self._in_search: bool = False
        # True while showing a Sent folder, where the column holds the
        # recipients rather than the sender.
        self._show_recipients: bool = False
        self._row_col_key: ColumnKey | None = None
        self._who_width_cached: int = 0
        # Keys whose row has actually been added to the DataTable.
        # Populated incrementally by the streaming load worker.
        self._loaded_keys: set[str] = set()
        # The current streaming-load worker. Actions that must operate
        # on the *complete* row set (cursor-last, mark-all-read) await
        # ``wait_for_load_complete()`` before running.
        self._load_worker: Worker[None] | None = None
        # Row the cursor is heading for once the stream reaches it, set
        # by ``load_folder(restore_row=…)``. None once applied.
        self._pending_cursor: int | None = None
        # Clear while a load is still finding out what its rows are.
        # A panel with no rows knows that much, so it starts set.
        self._rows_known = asyncio.Event()
        self._rows_known.set()

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._who_width_cached = self._who_width()
        self._row_col_key = self.add_column(self._header_text(), key="row")

    def _who_width(self) -> int:
        """Fixed width for the From/To field — capped at 25% of table width."""
        return max(10, min(40, max(20, self.size.width) // 4))

    def _header_text(self) -> str:
        """The column headings, padded to this table's widths.

        What the correspondent column is *called* is
        `views.correspondent_label`: the browser labels the same column
        and used to decide the word for itself.
        """
        who_w = self._who_width_cached
        label = correspondent_label(shows_recipient=self._show_recipients)
        return f"  {'Date':<{_DATE_WIDTH}} {label:<{who_w}} Subject"

    def _cell_for(self, summary: FolderMessageSummary) -> Text:
        """Pad one projected row into this table's columns.

        The fields come from :func:`pony.views.message_row`; what is
        left here is the part that only means anything in a terminal —
        padding and truncating each field to a character width, and
        choosing between the widget's dim default and full brightness.
        """
        who_w = self._who_width_cached
        row = message_row(
            summary,
            show_recipients=self._show_recipients,
            marked=bool(self._marked) and str(summary.message_ref.id) in self._marked,
        )
        line = (
            f"{row.icon} "
            f"{row.date:<{_DATE_WIDTH}.{_DATE_WIDTH}} "
            f"{row.correspondent:<{who_w}.{who_w}} "
            f"{row.subject}"
        )
        return Text(line, style="not dim" if row.unread else "dim")

    def _update_row(self, summary: FolderMessageSummary) -> None:
        assert self._row_col_key is not None
        key = str(summary.message_ref.id)
        # The streaming loader may not have inserted this row yet; the
        # eventual insert will pick up the latest summary because
        # ``_summaries`` is the source of cell content via ``_cell_for``.
        if key not in self._loaded_keys:
            return
        self.update_cell(
            row_key=key,
            column_key=self._row_col_key,
            value=self._cell_for(summary),
        )

    def _set_correspondent_column(self, show_recipients: bool) -> None:
        """Switch the correspondent column between sender and recipients.

        The choice is baked into both the header label and every
        rendered row, so a change repaints what is already on screen.
        Callers that clear the table first pay nothing for the repaint —
        ``_update_row`` skips rows that have not been inserted yet.
        """
        if show_recipients == self._show_recipients:
            return
        self._show_recipients = show_recipients
        self._repaint_header()

    def _repaint_header(self) -> None:
        """Rewrite the column label and re-render every row under it.

        Each row is one pre-formatted string, so anything that changes
        the header — the field width, or which correspondent it names —
        has to re-render the rows to keep the columns lined up.  Both
        callers can fire before ``on_mount`` has added the column, and
        there is nothing to repaint until it exists.
        """
        if self._row_col_key is None:
            return
        self.columns[self._row_col_key].label = Text(self._header_text())
        for summary in self._summaries:
            self._update_row(summary)

    def load_folder(
        self,
        folder_ref: FolderRef,
        *,
        restore_row: int | None = None,
        restore_key: str | None = None,
        show_recipients: bool = False,
    ) -> None:
        """Replace the table contents with messages from *folder_ref*.

        The SQL path pre-filters to ``local_status='active'`` and
        pre-sorts by ``received_at DESC`` so no Python-side filter or
        sort is needed. Row insertion into the underlying ``DataTable``
        is streamed in batches by a worker so the UI thread isn't
        blocked on large folders.

        *restore_key* names the message the cursor should follow, and
        *restore_row* is the fallback for when that message is no longer
        in the folder.  Both matter: after a sync the message is still
        there but new mail may have pushed it down the list, so following
        the row number would land on a different message; after a trash
        or archive the message is gone, and the row number is exactly
        right because it identifies whatever took its place.

        ``clear()`` resets the cursor and the replacement rows arrive
        later from the worker, so neither can be done by moving the
        cursor once this returns — there is nothing to move it to yet.
        Nor is there a selection: the query runs in a thread, and until
        it answers the panel is :attr:`awaiting_rows` and says so in its
        border.  :class:`RowsReady` is posted the moment it answers.

        *show_recipients* asks for the To column instead of the From
        column; the screen sets it for a Sent folder, where the sender
        is the user on every row.
        """
        titles = _Titles(
            loading="Messages  (loading…)",
            ready="Messages",
            failed="Messages  (could not load)",
        )
        self._in_search = False
        self._begin_load(titles.loading)
        self._set_correspondent_column(show_recipients)
        self._load_worker = self._load_rows(
            lambda: list(self._index.list_folder_message_summaries(folder=folder_ref)),
            titles=titles,
            restore_row=restore_row,
            restore_key=restore_key,
        )

    def _restore_target(
        self,
        summaries: list[FolderMessageSummary],
        restore_row: int | None,
        restore_key: str | None,
    ) -> int | None:
        """Row the cursor should end on, or None to leave it at the top.

        Prefers the message named by *restore_key*; falls back to
        *restore_row*, clamped.  Resolved as soon as the query answers,
        which is before any row has been inserted -- so the cursor has
        a destination throughout the stream, and
        :attr:`effective_cursor_row` is that destination.
        """
        if not summaries:
            return None
        if restore_key is not None:
            for index, summary in enumerate(summaries):
                if str(summary.message_ref.id) == restore_key:
                    return index or None
        if restore_row is None or restore_row <= 0:
            return None
        return min(restore_row, len(summaries) - 1)

    @property
    def in_search(self) -> bool:
        """True while the rows on screen are search results, not a folder.

        The screen has to know: a list action that reloaded the folder
        would drop out of search, so triaging a result set meant running
        the search again for every message.
        """
        return self._in_search

    def load_search(
        self,
        query_raw: str,
        *,
        fetch: Callable[[], list[IndexedMessage]],
        restore_row: int | None = None,
        restore_key: str | None = None,
    ) -> None:
        """Replace the table contents with what *fetch* finds.

        The query belongs to the screen -- it owns the words and their
        scope -- and running it belongs here, in the one worker that
        already fetches a folder off the event loop.  It is the most
        expensive read the reader can ask for (``index_store`` records
        385 ms for it) and it used to run on the loop, so the terminal
        was frozen for the whole of it with the old folder still on
        screen and nothing to say why.  Now the panel empties, says it
        is searching, and fills when the hits arrive.

        Search returns full ``IndexedMessage`` objects; they are
        projected down to summaries in the worker, so the panel state
        is uniform and the projection is off the loop too.

        *restore_row* and *restore_key* mean what they do in
        :meth:`load_folder`: they are what lets the screen re-run a
        search after an action without throwing the cursor back to the
        top of the hit list.

        The rows are shown in the order they arrive.  This used to sort
        them by date -- a second definition of "newest first", beside
        the ``ORDER BY m.received_at DESC`` the search already runs, and
        one that would quietly undo any other order a caller asked for.
        """
        titles = _Titles(
            loading=f"Search: {query_raw}  (searching…)",
            ready=f"Search: {query_raw}  [q=exit]",
            failed=f"Search: {query_raw}  (search failed)  [q=exit]",
            empty=f"Search: {query_raw}  (no results)  [q=exit]",
        )
        self._in_search = True
        self._begin_load(titles.loading)
        # Results span folders, so there is no single answer to whether
        # the user is the sender; From is the column that always means
        # something.
        self._set_correspondent_column(False)
        self._load_worker = self._load_rows(
            lambda: [summary_of(message) for message in fetch()],
            titles=titles,
            restore_row=restore_row,
            restore_key=restore_key,
        )

    def _begin_load(self, awaiting_title: str) -> None:
        """Empty the panel and say that rows are on their way.

        One state for both loads: nothing selected, nothing to act on,
        and a border that says why.  A folder read with no sign of it is
        a keypress that looks ignored, which is what moving the query
        off the event loop would otherwise buy.
        """
        self._marked.clear()
        self.border_title = awaiting_title
        self.clear()
        self._loaded_keys.clear()
        self._summaries = []
        self._pending_cursor = None
        self._rows_known.clear()

    @property
    def awaiting_rows(self) -> bool:
        """True while a load has yet to find out what its rows are.

        There is no cursor row to read and nothing to act on while this
        holds: the screen says so rather than acting on the wrong
        message or appearing to drop the key.
        """
        return not self._rows_known.is_set()

    @work(exclusive=True, group="message-list-load")
    async def _load_rows(
        self,
        fetch: Callable[[], list[FolderMessageSummary]],
        *,
        titles: _Titles,
        restore_row: int | None,
        restore_key: str | None,
    ) -> None:
        """Find the rows off the event loop, then stream them in.

        The query used to run here on the loop -- a folder of ten
        thousand froze the terminal for as long as SQLite took, and
        `docs/architecture.md` claimed a worker that only the row
        insertion had.  ``asyncio.to_thread`` is what the rest of the
        screen's blocking work uses; the index opens one connection per
        thread for exactly this.

        A read that raises has to end the awaiting state too, or the
        panel waits for rows that are never coming: the border goes on
        saying it is loading and every action answers "the list is
        still loading" for the rest of the session.  A failed load is
        an empty list, a border that says so, and one notice.

        ``exclusive=True`` ensures a second ``load_folder`` call cancels
        the in-flight load -- query included -- before this one starts
        adding rows for the previous folder.  Cancellation is not a
        failure: asyncio raises it into this coroutine rather than
        resuming it, so nothing below runs and the load that cancelled
        it owns the panel.
        """
        try:
            summaries = await asyncio.to_thread(fetch)
        except Exception:
            _log.exception("loading the message list failed")
            self._finish_load([], titles=titles, failed=True)
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Could not load the message list.", severity="error"
            )
            return
        self._finish_load(
            summaries,
            titles=titles,
            restore_row=restore_row,
            restore_key=restore_key,
        )
        await self._stream_rows(summaries)

    def _finish_load(
        self,
        summaries: list[FolderMessageSummary],
        *,
        titles: _Titles,
        restore_row: int | None = None,
        restore_key: str | None = None,
        failed: bool = False,
    ) -> None:
        """Adopt what the read found and stop awaiting rows.

        Before the rows are inserted: what reads the panel back wants
        the selection, and the selection is decided by now.
        """
        self._summaries = summaries
        self._pending_cursor = self._restore_target(summaries, restore_row, restore_key)
        if failed:
            self.border_title = titles.failed
        elif not summaries and titles.empty is not None:
            self.border_title = titles.empty
        else:
            self.border_title = titles.ready
        self._rows_known.set()
        self.post_message(self.RowsReady())

    async def _stream_rows(self, summaries: list[FolderMessageSummary]) -> None:
        """Insert rows in batches, yielding to the event loop between them."""
        for i in range(0, len(summaries), self._LOAD_BATCH):
            chunk = summaries[i : i + self._LOAD_BATCH]
            for summary in chunk:
                key = str(summary.message_ref.id)
                self.add_row(self._cell_for(summary), key=key)
                self._loaded_keys.add(key)
            # Restore as soon as the target row exists rather than after
            # the whole folder has streamed in, so the cursor does not
            # visibly sit at the top of a large folder first.
            pending = self._pending_cursor
            if pending is not None and self.row_count > pending:
                self.move_cursor(row=pending)
                self._pending_cursor = None
            # Yield to the event loop so input, render, and the
            # message-selected auto-preview can interleave with loading.
            await asyncio.sleep(0)

    def _summary_for_row_key(self, row_key: object) -> FolderMessageSummary | None:
        from textual.widgets._data_table import RowKey

        if not isinstance(row_key, RowKey) or row_key.value is None:
            return None
        return self._find_summary(str(row_key.value))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Swallow the cursor-move event without opening the message.

        Moving the row cursor is navigation, not activation: it must not
        open the reader, must not steal focus from this panel, and must
        not mark the message seen.  Opening is Enter only — see
        :meth:`on_data_table_row_selected`.
        """
        event.stop()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        summary = self._summary_for_row_key(event.row_key)
        if summary is not None:
            self.post_message(self.MessageSelected(summary=summary))

    @property
    def effective_cursor_row(self) -> int:
        """The row the panel considers current.

        While a reload is still streaming, the real cursor is still at 0
        and the row it is heading for may not exist yet.  Every caller
        that asks "which message is selected" has to see the destination,
        or a trash immediately followed by another action would operate
        on the top of the folder instead of the message under the cursor.

        Before that -- while the query is still running -- there is no
        destination and no rows, and :attr:`awaiting_rows` is how a
        caller tells that apart from an empty folder.
        """
        if self._pending_cursor is not None:
            return self._pending_cursor
        return self.cursor_row

    def get_selected_summary(self) -> FolderMessageSummary | None:
        """Return the summary for the highlighted row, if any."""
        row = self.effective_cursor_row
        if row < 0 or row >= len(self._summaries):
            return None
        return self._summaries[row]

    def action_cursor_first(self) -> None:
        if self._summaries:
            self.move_cursor(row=0)

    async def action_cursor_last(self) -> None:
        # "Last" means the actual last message, not the last currently
        # streamed; wait for the load to finish before moving.
        await self.wait_for_load_complete()
        if self._summaries:
            self.move_cursor(row=len(self._summaries) - 1)

    async def wait_for_load_complete(self) -> None:
        """Await completion of the in-flight folder-load worker, if any."""
        worker = self._load_worker
        if worker is None or worker.is_finished:
            return
        await worker.wait()

    def move_cursor_to_next_unread(self) -> FolderMessageSummary | None:
        """Move to the first unread message after the current row.

        Returns the summary, or ``None`` if no unread message follows.

        From :attr:`effective_cursor_row`, not from `cursor_row`: while
        a reload is still streaming the real cursor is at 0 and the row
        it is heading for may not exist yet, and this is reached by
        ++space++ -- which is exactly what a reader presses while the
        rows after a trash are still arriving.
        """
        start = self.effective_cursor_row + 1
        for i in range(start, len(self._summaries)):
            summary = self._summaries[i]
            if MessageFlag.SEEN not in summary.local_flags:
                self.move_cursor(row=i)
                return summary
        return None

    def update_summary(self, updated: FolderMessageSummary) -> None:
        """Replace one row's summary and re-render it.

        Used by flag/status/seen actions after they've written the
        change back to the index via ``upsert_message``.
        """
        target = updated.message_ref.id
        for i, summary in enumerate(self._summaries):
            if summary.message_ref.id == target:
                self._summaries[i] = updated
                self._update_row(updated)
                break

    def update_from_indexed(self, updated: IndexedMessage) -> None:
        """Convenience — derive a summary from a full IndexedMessage."""
        self.update_summary(summary_of(updated))

    def on_key(self, event: object) -> None:
        """Ask the screen to back out of search when q or escape is pressed.

        The screen decides what backing out means: an open reader is
        closed first, and only a press with no reader open leaves search.
        """
        from textual.events import Key

        if not isinstance(event, Key):
            return
        if not self._in_search:
            return
        if event.key in ("q", "escape"):
            event.prevent_default()
            event.stop()
            self.post_message(self.SearchExitRequested())

    def exit_search(self) -> None:
        """Drop search-results mode; the caller reloads the folder."""
        self._in_search = False
        self._marked.clear()
        self.border_title = "Messages"

    def on_resize(self) -> None:
        """Re-format header and rows when the correspondent width changes."""
        new_width = self._who_width()
        if new_width == self._who_width_cached:
            return
        self._who_width_cached = new_width
        self._repaint_header()

    def move_cursor_by(self, delta: int) -> FolderMessageSummary | None:
        """Move the row cursor by *delta* (±1) and return the new summary.

        Relative to :attr:`effective_cursor_row`, for the same reason
        `move_cursor_to_next_unread` is: ++n++ and ++p++ during a
        reload moved from row 0 rather than from the row the reload is
        restoring to, so the message that opened was the second in the
        folder.
        """
        new_row = self.effective_cursor_row + delta
        if new_row < 0 or new_row >= len(self._summaries):
            return None
        self.move_cursor(row=new_row)
        return self._summaries[new_row]

    # ------------------------------------------------------------------
    # Mark / unmark
    # ------------------------------------------------------------------

    def _toggle_mark_current(self) -> None:
        summary = self.get_selected_summary()
        if summary is None:
            return
        mid = str(summary.message_ref.id)
        if mid in self._marked:
            self._marked.discard(mid)
        else:
            self._marked.add(mid)
        self._update_row(summary)

    def action_mark_down(self) -> None:
        self._toggle_mark_current()
        self.action_cursor_down()

    def action_mark_up(self) -> None:
        self._toggle_mark_current()
        self.action_cursor_up()

    def marked_summaries(self) -> list[FolderMessageSummary]:
        """Summaries currently marked, ordered as they appear in the list."""
        return [s for s in self._summaries if str(s.message_ref.id) in self._marked]

    def summaries_to_act_on(self) -> list[FolderMessageSummary]:
        """Marked summaries if any, otherwise the cursor row as a singleton."""
        if self._marked:
            return self.marked_summaries()
        summary = self.get_selected_summary()
        return [summary] if summary is not None else []

    def clear_marks(self) -> None:
        """Remove all marks and re-render the rows that were previously marked."""
        if not self._marked:
            return
        previously_marked = set(self._marked)
        self._marked.clear()
        for summary in self._summaries:
            if str(summary.message_ref.id) in previously_marked:
                self._update_row(summary)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_summary(self, key: str) -> FolderMessageSummary | None:
        for s in self._summaries:
            if str(s.message_ref.id) == key:
                return s
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
