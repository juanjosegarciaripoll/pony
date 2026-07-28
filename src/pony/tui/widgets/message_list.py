"""Message list panel — top-right pane showing messages in the selected folder."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

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
from ..bindings import MARK_BINDINGS, MOTION_BINDINGS
from .edge_drag import DraggableEdgeMixin

# Width of the date cell. Widest format produced by ``_format_date`` is
# ``YYYY-MM-DD`` (10 chars).
_DATE_WIDTH = 10


class MessageListPanel(DraggableEdgeMixin, DataTable[Text | str]):
    """Single-column list of messages for the currently selected folder.

    The whole row is rendered as one pre-formatted line:
    ``<icon> <date> <from> <subject>`` with fixed-width icon, date, and
    from fields.  Seen rows are returned as plain ``str`` and inherit
    the widget's ``text-style: dim`` from CSS — the cheap path, since
    read messages dominate.  Unseen rows allocate one ``Text`` styled
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

    BINDINGS = [
        *MOTION_BINDINGS,
        *MARK_BINDINGS,
    ]

    @dataclass
    class MessageSelected(Message):
        """Posted when the user activates a message row."""

        summary: FolderMessageSummary

    @dataclass
    class SearchExited(Message):
        """Posted when the user exits search-results mode."""

    _LOAD_BATCH = 200

    def __init__(self, index: IndexRepository, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._index = index
        self._summaries: list[FolderMessageSummary] = []
        self._marked: set[str] = set()  # str(message_ref.id)
        self._in_search: bool = False
        self._row_col_key: ColumnKey | None = None
        self._from_width_cached: int = 0
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

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._from_width_cached = self._from_width()
        self._row_col_key = self.add_column(self._header_text(), key="row")

    def _from_width(self) -> int:
        """Fixed width for the From field — capped at 25% of table width."""
        return max(10, min(40, max(20, self.size.width) // 4))

    def _header_text(self) -> str:
        from_w = self._from_width_cached
        return f"  {'Date':<{_DATE_WIDTH}} {'From':<{from_w}} Subject"

    def _icon_for(self, summary: FolderMessageSummary) -> str:
        if self._marked and str(summary.message_ref.id) in self._marked:
            return "*"
        return _icon_column(summary)

    def _cell_for(self, summary: FolderMessageSummary) -> Text:
        from_w = self._from_width_cached
        icon = self._icon_for(summary)
        date = _format_date(summary.received_at)
        sender = summary.sender
        subject = summary.subject or "(no subject)"
        line = (
            f"{icon} "
            f"{date:<{_DATE_WIDTH}.{_DATE_WIDTH}} "
            f"{sender:<{from_w}.{from_w}} "
            f"{subject}"
        )
        if MessageFlag.SEEN in summary.local_flags:
            return Text(line, style="dim")
        return Text(line, style="not dim")

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

    def load_folder(
        self,
        folder_ref: FolderRef,
        *,
        restore_row: int | None = None,
        restore_key: str | None = None,
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
        """
        self._in_search = False
        self._marked.clear()
        self.border_title = "Messages"
        self.clear()
        self._loaded_keys.clear()
        summaries = list(self._index.list_folder_message_summaries(folder=folder_ref))
        self._summaries = summaries
        self._pending_cursor = self._restore_target(summaries, restore_row, restore_key)
        self._load_worker = self._stream_rows(summaries)

    def _restore_target(
        self,
        summaries: list[FolderMessageSummary],
        restore_row: int | None,
        restore_key: str | None,
    ) -> int | None:
        """Row the cursor should end on, or None to leave it at the top.

        Prefers the message named by *restore_key*; falls back to
        *restore_row*, clamped.  The full row list is known synchronously
        even though the rows stream in, so both resolve immediately.
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

    def load_search_results(
        self, messages: list[IndexedMessage], query_raw: str
    ) -> None:
        """Replace the table contents with search results.

        Search still returns full ``IndexedMessage`` objects; project
        them down to summaries here so the panel state is uniform.
        """
        self._in_search = True
        self._marked.clear()
        self.border_title = f"Search: {query_raw}  [q=exit]"
        self.clear()
        self._loaded_keys.clear()
        msgs = list(messages)
        msgs.sort(key=lambda m: m.received_at, reverse=True)
        summaries = [_summary_from_indexed(m) for m in msgs]
        self._summaries = summaries
        self._load_worker = self._stream_rows(summaries)
        if not msgs:
            self.border_title = f"Search: {query_raw}  (no results)  [q=exit]"

    @work(exclusive=True, group="message-list-load")
    async def _stream_rows(self, summaries: list[FolderMessageSummary]) -> None:
        """Insert rows in batches, yielding to the event loop between them.

        ``exclusive=True`` ensures a second ``load_folder`` call cancels
        the in-flight load before this one starts adding rows for the
        previous folder.
        """
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
        """
        start = self.cursor_row + 1
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
        self.update_summary(_summary_from_indexed(updated))

    def on_key(self, event: object) -> None:
        """Exit search mode when q or escape is pressed."""
        from textual.events import Key

        if not isinstance(event, Key):
            return
        if not self._in_search:
            return
        if event.key in ("q", "escape"):
            event.prevent_default()
            event.stop()
            self.action_exit_search()

    def action_exit_search(self) -> None:
        """Post SearchExited to let the screen reload the current folder."""
        self._in_search = False
        self._marked.clear()
        self.border_title = "Messages"
        self.post_message(self.SearchExited())

    def on_resize(self) -> None:
        """Re-format header and rows when the From-field width changes.

        Each row is a single pre-formatted string, so a width change
        means re-rendering every cell to keep columns aligned.
        """
        if self._row_col_key is None:
            return
        new_width = self._from_width()
        if new_width == self._from_width_cached:
            return
        self._from_width_cached = new_width
        col = self.columns[self._row_col_key]
        col.label = Text(self._header_text())
        for summary in self._summaries:
            self._update_row(summary)

    def move_cursor_by(self, delta: int) -> FolderMessageSummary | None:
        """Move the row cursor by *delta* (±1) and return the new summary."""
        new_row = self.cursor_row + delta
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
# ---------------------------------------------------------------------------


def _summary_from_indexed(msg: IndexedMessage) -> FolderMessageSummary:
    """Project an IndexedMessage down to the fields the list needs."""
    return FolderMessageSummary(
        message_ref=msg.message_ref,
        message_id=msg.message_id,
        storage_key=msg.storage_key,
        sender=msg.sender,
        subject=msg.subject,
        received_at=msg.received_at,
        has_attachments=msg.has_attachments,
        local_flags=msg.local_flags,
        local_status=msg.local_status,
    )


def _icon_column(summary: FolderMessageSummary) -> str:
    """Status marker: ``!`` flagged, ``↩`` answered, ``+`` has-attachments."""
    if MessageFlag.FLAGGED in summary.local_flags:
        return "!"
    if MessageFlag.ANSWERED in summary.local_flags:
        return "↩"
    if summary.has_attachments:
        return "+"
    return " "


def _format_date(dt: datetime) -> str:
    now = datetime.now(tz=dt.tzinfo)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%b %d")
    return dt.strftime("%Y-%m-%d")
