"""What the mail reader's lists are *of*, with no renderer attached.

A row in the message list is four fields and a state: a status marker,
when it arrived, who it is from (or to), what it is about, and whether it
has been read.  Padding those into one fixed-width line is a terminal's
answer to showing them in columns, and a browser would want the fields
and a stylesheet instead — so the fields are decided here and the
padding stays in the widget.

This is the mail half of what :mod:`chronos.views` is for the calendar,
and it sits beside the stores for the same reason: a projection that
lives under ``tui/`` can only ever serve one front end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .domain import FolderMessageSummary, MessageFlag

if TYPE_CHECKING:
    from .domain import IndexedMessage

# What each marker means, in precedence order: a flagged message is
# flagged whether or not it was answered or carries attachments.
FLAGGED_MARK = "!"
ANSWERED_MARK = "↩"
ATTACHMENT_MARK = "+"
MARKED_MARK = "*"
NO_MARK = " "


@dataclass(frozen=True, slots=True)
class MessageRow:
    """One line of the message list, before anything lays it out."""

    icon: str
    date: str
    correspondent: str
    subject: str
    # Read messages dominate a mailbox, so the interesting state is the
    # exception: a renderer emphasises an unread row rather than muting
    # every other one.
    unread: bool


def message_row(
    summary: FolderMessageSummary,
    *,
    show_recipients: bool,
    marked: bool = False,
    now: datetime | None = None,
) -> MessageRow:
    """Project *summary* into the fields a list row shows.

    *show_recipients* swaps the correspondent from the sender to the To
    address, which is what a Sent folder needs: there the sender is the
    user on every row and the recipient is the only thing that tells the
    rows apart.

    *marked* replaces the status marker, because a row the user has
    marked for an action has something more urgent to say than that it
    carries an attachment.
    """
    return MessageRow(
        icon=MARKED_MARK if marked else status_mark(summary),
        date=format_received(summary.received_at, now=now),
        correspondent=summary.recipients if show_recipients else summary.sender,
        subject=summary.subject or "(no subject)",
        unread=MessageFlag.SEEN not in summary.local_flags,
    )


def status_mark(summary: FolderMessageSummary) -> str:
    """Status marker: flagged, then answered, then has-attachments."""
    if MessageFlag.FLAGGED in summary.local_flags:
        return FLAGGED_MARK
    if MessageFlag.ANSWERED in summary.local_flags:
        return ANSWERED_MARK
    if summary.has_attachments:
        return ATTACHMENT_MARK
    return NO_MARK


def format_received(received_at: datetime, *, now: datetime | None = None) -> str:
    """When a message arrived, at the precision that is still useful.

    Today's mail is told apart by the time of day; this year's by day and
    month; anything older carries its year, because that is the only
    thing that distinguishes it.
    """
    moment = now or datetime.now(tz=received_at.tzinfo)
    if received_at.date() == moment.date():
        return received_at.strftime("%H:%M")
    if received_at.year == moment.year:
        return received_at.strftime("%b %d")
    return received_at.strftime("%Y-%m-%d")


def summary_of(message: IndexedMessage) -> FolderMessageSummary:
    """Narrow *message* to the fields a list row needs.

    The index hands these out directly for a whole folder; this is for
    the one row an action has just rewritten, so a repaint does not have
    to re-read what it already has.
    """
    return FolderMessageSummary(
        message_ref=message.message_ref,
        message_id=message.message_id,
        storage_key=message.storage_key,
        sender=message.sender,
        recipients=message.recipients,
        subject=message.subject,
        received_at=message.received_at,
        has_attachments=message.has_attachments,
        local_flags=message.local_flags,
        local_status=message.local_status,
    )


__all__ = [
    "ANSWERED_MARK",
    "ATTACHMENT_MARK",
    "FLAGGED_MARK",
    "MARKED_MARK",
    "NO_MARK",
    "MessageRow",
    "format_received",
    "message_row",
    "status_mark",
    "summary_of",
]
