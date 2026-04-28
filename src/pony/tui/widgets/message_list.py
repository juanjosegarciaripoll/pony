"""Message list panel — top-right pane showing messages in the selected folder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.text import Text
from textual.message import Message
from textual.widgets import DataTable
from textual.widgets._data_table import ColumnKey

from ...domain import (
    FolderMessageSummary,
    FolderRef,
    IndexedMessage,
    MessageFlag,
    MessageStatus,
)
from ...protocols import IndexRepository
from ..bindings import MARK_BINDINGS, MOTION_BINDINGS


class MessageListPanel(DataTable[Text]):
    """Tabular list of messages for the currently selected folder.

    Columns: status marker, date, from, subject.  Read messages are
    dimmed, trashed messages are struck through.  The marker column
    shows ``!`` for flagged, ``+`` for messages with attachments,
    ``*`` for marked, or blank.  Posts
    ``MessageListPanel.MessageSelected`` when a row is activated.

    The panel holds ``FolderMessageSummary`` rows — a narrow
    projection of ``IndexedMessage`` that skips the datetime and
    flag-set parsing the full object requires.  Callers that need
    the full ``IndexedMessage`` (e.g. to rewrite it via ``upsert_message``
    in a flag/status action, or to render a reply body) re-fetch it
    from the index using ``summary.message_ref``.
    """

    BORDER_TITLE = "Messages"

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

    def __init__(
        self, index: IndexRepository, **kwargs: object
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._index = index
        self._summaries: list[FolderMessageSummary] = []
        self._marked: set[str] = set()  # str(message_ref.id)
        self._in_search: bool = False
        self._icons_col_key: ColumnKey | None = None
        self._date_col_key: ColumnKey | None = None
        self._from_col_key: ColumnKey | None = None
        self._subject_col_key: ColumnKey | None = None

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._icons_col_key = self.add_column(" ", width=1, key="icons")
        self._date_col_key = self.add_column("Date")
        self._from_col_key = self.add_column("From")
        self._subject_col_key = self.add_column("Subject")

    def _from_max(self) -> int:
        """Max characters for the From column — capped at 25% of table width."""
        table_width = max(20, self.size.width)
        return max(10, min(40, table_width // 4))

    def _cells_for(
        self, summary: FolderMessageSummary,
    ) -> tuple[Text, Text, Text, Text]:
        style = _row_style(summary)
        icon = (
            "*" if str(summary.message_ref.id) in self._marked
            else _icon_column(summary)
        )
        return (
            Text(icon, style=style),
            Text(_format_date(summary.received_at), style=style),
            Text(_truncate(summary.sender, self._from_max()), style=style),
            Text(summary.subject or "(no subject)", style=style),
        )

    def _update_row(self, summary: FolderMessageSummary) -> None:
        assert self._icons_col_key is not None
        assert self._date_col_key is not None
        assert self._from_col_key is not None
        assert self._subject_col_key is not None
        key = str(summary.message_ref.id)
        icons, date, sender, subject = self._cells_for(summary)
        self.update_cell(
            row_key=key, column_key=self._icons_col_key, value=icons,
        )
        self.update_cell(
            row_key=key, column_key=self._date_col_key, value=date,
        )
        self.update_cell(
            row_key=key, column_key=self._from_col_key, value=sender,
            update_width=True,
        )
        self.update_cell(
            row_key=key, column_key=self._subject_col_key, value=subject,
        )

    def load_folder(self, folder_ref: FolderRef) -> None:
        """Replace the table contents with messages from *folder_ref*.

        The SQL path pre-filters to ``local_status='active'`` and
        pre-sorts by ``received_at DESC`` so no Python-side filter or
        sort is needed.
        """
        self._in_search = False
        self._marked.clear()
        self.border_title = "Messages"
        self.clear()
        summaries = list(self._index.list_folder_message_summaries(folder=folder_ref))
        self._summaries = summaries
        for summary in summaries:
            self.add_row(
                *self._cells_for(summary),
                key=str(summary.message_ref.id),
            )

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
        msgs = list(messages)
        msgs.sort(key=lambda m: m.received_at, reverse=True)
        summaries = [_summary_from_indexed(m) for m in msgs]
        self._summaries = summaries
        for summary in summaries:
            self.add_row(
                *self._cells_for(summary),
                key=str(summary.message_ref.id),
            )
        if not msgs:
            self.border_title = f"Search: {query_raw}  (no results)  [q=exit]"

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        event.stop()
        key = str(event.row_key.value) if event.row_key.value else ""
        summary = self._find_summary(key)
        if summary is not None:
            self.post_message(self.MessageSelected(summary=summary))

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        event.stop()
        key = str(event.row_key.value) if event.row_key.value else ""
        summary = self._find_summary(key)
        if summary is not None:
            self.post_message(self.MessageSelected(summary=summary))

    def get_selected_summary(self) -> FolderMessageSummary | None:
        """Return the summary for the highlighted row, if any."""
        if self.cursor_row < 0 or self.cursor_row >= len(self._summaries):
            return None
        return self._summaries[self.cursor_row]

    def action_cursor_first(self) -> None:
        if self._summaries:
            self.move_cursor(row=0)

    def action_cursor_last(self) -> None:
        if self._summaries:
            self.move_cursor(row=len(self._summaries) - 1)

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
        """Refresh all rows so the From column stays capped at 25% of width."""
        if self._from_col_key is None or not self._summaries:
            return
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
        return [
            s for s in self._summaries
            if str(s.message_ref.id) in self._marked
        ]

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
    """Single-character status marker: ``!`` flagged, ``+`` has-attachments."""
    if MessageFlag.FLAGGED in summary.local_flags:
        return "!"
    if summary.has_attachments:
        return "+"
    return " "


def _row_style(summary: FolderMessageSummary) -> str:
    """Rich style string applied to every cell in the row.

    Read messages are dimmed; trashed messages are struck through.
    """
    parts: list[str] = []
    if MessageFlag.SEEN in summary.local_flags:
        parts.append("dim")
    if summary.local_status == MessageStatus.TRASHED:
        parts.append("strike")
    return " ".join(parts)


def _format_date(dt: datetime) -> str:
    now = datetime.now(tz=dt.tzinfo)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%b %d")
    return dt.strftime("%Y-%m-%d")


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"
