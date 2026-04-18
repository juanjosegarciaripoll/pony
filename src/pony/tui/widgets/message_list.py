"""Message list panel — top-right pane showing messages in the selected folder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import DataTable
from textual.widgets._data_table import ColumnKey

from ...domain import FolderRef, IndexedMessage, MessageFlag, MessageStatus
from ...protocols import IndexRepository


class MessageListPanel(DataTable[Text]):
    """Tabular list of messages for the currently selected folder.

    Columns: status marker, date, from, subject. Read messages are dimmed,
    trashed messages are struck through. The marker column shows ``!`` for
    flagged, ``+`` for messages with attachments, or blank.
    Posts ``MessageListPanel.MessageSelected`` when a row is activated.
    """

    BORDER_TITLE = "Messages"

    BINDINGS = [
        Binding("n", "cursor_down", "Next", show=False),
        Binding("p", "cursor_up", "Prev", show=False),
        Binding("<", "cursor_first", "First", show=False),
        Binding(">", "cursor_last", "Last", show=False),
    ]

    @dataclass
    class MessageSelected(Message):
        """Posted when the user activates a message row."""
        message: IndexedMessage

    @dataclass
    class SearchExited(Message):
        """Posted when the user exits search-results mode."""

    def __init__(
        self, index: IndexRepository, **kwargs: object
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._index = index
        self._messages: list[IndexedMessage] = []
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
        self, msg: IndexedMessage,
    ) -> tuple[Text, Text, Text, Text]:
        style = _row_style(msg)
        return (
            Text(_icon_column(msg), style=style),
            Text(_format_date(msg.received_at), style=style),
            Text(_truncate(msg.sender, self._from_max()), style=style),
            Text(msg.subject or "(no subject)", style=style),
        )

    def _update_row(self, msg: IndexedMessage) -> None:
        assert self._icons_col_key is not None
        assert self._date_col_key is not None
        assert self._from_col_key is not None
        assert self._subject_col_key is not None
        mid = msg.message_ref.message_id
        icons, date, sender, subject = self._cells_for(msg)
        self.update_cell(
            row_key=mid, column_key=self._icons_col_key, value=icons,
        )
        self.update_cell(
            row_key=mid, column_key=self._date_col_key, value=date,
        )
        self.update_cell(
            row_key=mid, column_key=self._from_col_key, value=sender,
            update_width=True,
        )
        self.update_cell(
            row_key=mid, column_key=self._subject_col_key, value=subject,
        )

    def load_folder(self, folder_ref: FolderRef) -> None:
        """Replace the table contents with messages from *folder_ref*."""
        self._in_search = False
        self.border_title = "Messages"
        self.clear()
        msgs = list(self._index.list_folder_messages(folder=folder_ref))
        # Active messages only; sort newest first.
        msgs = [
            m for m in msgs if m.local_status == MessageStatus.ACTIVE
        ]
        msgs.sort(key=lambda m: m.received_at, reverse=True)
        self._messages = msgs
        for msg in msgs:
            self.add_row(
                *self._cells_for(msg),
                key=msg.message_ref.message_id,
            )

    def load_search_results(
        self, messages: list[IndexedMessage], query_raw: str
    ) -> None:
        """Replace the table contents with search results."""
        self._in_search = True
        self.border_title = f"Search: {query_raw}  [q=exit]"
        self.clear()
        msgs = list(messages)
        msgs.sort(key=lambda m: m.received_at, reverse=True)
        self._messages = msgs
        for msg in msgs:
            self.add_row(
                *self._cells_for(msg),
                key=msg.message_ref.message_id,
            )
        if not msgs:
            self.border_title = f"Search: {query_raw}  (no results)  [q=exit]"

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        event.stop()
        # Find the IndexedMessage by its message_id key.
        key = str(event.row_key.value) if event.row_key.value else ""
        for msg in self._messages:
            if msg.message_ref.message_id == key:
                self.post_message(self.MessageSelected(message=msg))
                return

    def get_selected_message(self) -> IndexedMessage | None:
        """Return the currently highlighted message, if any."""
        if self.cursor_row < 0 or self.cursor_row >= len(self._messages):
            return None
        return self._messages[self.cursor_row]

    def action_cursor_first(self) -> None:
        if self._messages:
            self.move_cursor(row=0)

    def action_cursor_last(self) -> None:
        if self._messages:
            self.move_cursor(row=len(self._messages) - 1)

    def move_cursor_to_next_unread(self) -> IndexedMessage | None:
        """Move to the first unread message after the current row.

        Returns the message or None if no unread message follows.
        """
        start = self.cursor_row + 1
        for i in range(start, len(self._messages)):
            msg = self._messages[i]
            if MessageFlag.SEEN not in msg.local_flags:
                self.move_cursor(row=i)
                return msg
        return None

    def update_message(self, updated: IndexedMessage) -> None:
        """Replace one message in the internal list and restyle its row."""
        mid = updated.message_ref.message_id
        for i, msg in enumerate(self._messages):
            if msg.message_ref.message_id == mid:
                self._messages[i] = updated
                self._update_row(updated)
                break

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
        self.border_title = "Messages"
        self.post_message(self.SearchExited())

    def on_resize(self) -> None:
        """Refresh all rows so the From column stays capped at 25% of width."""
        if self._from_col_key is None or not self._messages:
            return
        for msg in self._messages:
            self._update_row(msg)

    def move_cursor_by(self, delta: int) -> IndexedMessage | None:
        """Move the row cursor by *delta* (±1) and return the new message.

        Returns None if the move would go out of bounds (cursor stays put).
        """
        new_row = self.cursor_row + delta
        if new_row < 0 or new_row >= len(self._messages):
            return None
        self.move_cursor(row=new_row)
        return self._messages[new_row]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _icon_column(msg: IndexedMessage) -> str:
    """Single-character status marker: ``!`` flagged, ``+`` has-attachments."""
    if MessageFlag.FLAGGED in msg.local_flags:
        return "!"
    if msg.has_attachments:
        return "+"
    return " "


def _row_style(msg: IndexedMessage) -> str:
    """Rich style string applied to every cell in the row.

    Read messages are dimmed; trashed messages are struck through.
    """
    parts: list[str] = []
    if MessageFlag.SEEN in msg.local_flags:
        parts.append("dim")
    if msg.local_status == MessageStatus.TRASHED:
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
