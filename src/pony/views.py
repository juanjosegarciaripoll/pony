"""What the mail reader's lists are *of*, with no renderer attached.

A row in the message list is four fields and a state: a status marker,
when it arrived, who it is from (or to), what it is about, and whether it
has been read.  Padding those into one fixed-width line is a terminal's
answer to showing them in columns, and a browser would want the fields
and a stylesheet instead — so the fields are decided here and the
padding stays in the widget.

The folder tree is the same story one level up.  Which folders nest
under which, where a level exists only because a name was hierarchical,
and how much unread sits beneath it are all answered here; drawing that
as a collapsible `Tree` or as a `<ul>` is the front end's business.

This is the mail half of what :mod:`chronos.views` is for the calendar,
and it sits beside the stores for the same reason: a projection that
lives under ``tui/`` can only ever serve one front end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from .domain import Contact, FolderMessageSummary, FolderRef, MessageFlag

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .domain import IndexedMessage

# What each marker means, in precedence order: a flagged message is
# flagged whether or not it was answered or carries attachments.
FLAGGED_MARK = "!"
ANSWERED_MARK = "↩"
ATTACHMENT_MARK = "+"
MARKED_MARK = "*"
NO_MARK = " "


@dataclass(frozen=True, slots=True)
class FolderTreeNode:
    """One node in the nested folder display tree.

    ``folder_ref`` is ``None`` for *synthetic* parents — tree levels
    that exist only because a child folder name was hierarchical
    (``Archives.2026`` with no ``Archives`` folder on the server).
    The stored folder_name on the server is always the *full* path
    (``Archives.2026``); nesting is a display concern only.
    """

    label: str  # last path segment
    folder_ref: FolderRef | None  # None for synthetic parents
    own_unread: int  # 0 for synthetic parents
    children: tuple[FolderTreeNode, ...] = field(default_factory=tuple)

    def descendant_unread(self) -> int:
        """Total unread in this node and all descendants.

        Used to decide whether a synthetic parent is dim (all quiet) or
        bright (at least one descendant has unread).
        """
        return self.own_unread + sum(c.descendant_unread() for c in self.children)


def _split_folder_name(name: str) -> tuple[str, ...]:
    """Split *name* on the first ``.`` or ``/`` delimiter it contains.

    Per-folder detection (rather than a single account-wide delimiter)
    handles the rare case where different folders on the same server
    use different conventions.  ``INBOX`` is never split.  Empty
    segments (leading / trailing / doubled delimiters) are dropped.
    """
    if name == "INBOX":
        return (name,)
    dot = name.find(".")
    slash = name.find("/")
    candidates = [pos for pos in (dot, slash) if pos != -1]
    if not candidates:
        return (name,)
    first = min(candidates)
    delim = name[first]
    segments = tuple(seg for seg in name.split(delim) if seg)
    return segments or (name,)


def build_folder_tree(
    *,
    folder_names: Sequence[str],
    unread_counts: Mapping[str, int],
    account_name: str,
) -> tuple[FolderTreeNode, ...]:
    """Build a nested display tree from flat folder names.

    The returned nodes are roots under the account.  ``INBOX`` (if
    present) is pinned first; remaining siblings are sorted
    alphabetically at every level.
    """
    # Collect every distinct path prefix: ancestors are created on
    # demand, a real folder's full segmentation is the exact path.
    real_by_path: dict[tuple[str, ...], str] = {}
    all_paths: set[tuple[str, ...]] = set()
    for name in folder_names:
        segments = _split_folder_name(name)
        real_by_path[segments] = name
        for i in range(1, len(segments) + 1):
            all_paths.add(segments[:i])

    def _build_at(prefix: tuple[str, ...]) -> tuple[FolderTreeNode, ...]:
        direct = sorted(
            {
                p[len(prefix)]
                for p in all_paths
                if len(p) == len(prefix) + 1 and p[: len(prefix)] == prefix
            }
        )
        nodes: list[FolderTreeNode] = []
        for seg in direct:
            child_path = (*prefix, seg)
            real_name = real_by_path.get(child_path)
            folder_ref = (
                FolderRef(account_name=account_name, folder_name=real_name)
                if real_name is not None
                else None
            )
            own_unread = unread_counts.get(real_name, 0) if real_name else 0
            nodes.append(
                FolderTreeNode(
                    label=seg,
                    folder_ref=folder_ref,
                    own_unread=own_unread,
                    children=_build_at(child_path),
                )
            )
        return tuple(nodes)

    roots = list(_build_at(()))
    inbox = [n for n in roots if n.label == "INBOX"]
    others = [n for n in roots if n.label != "INBOX"]
    return tuple(inbox + others)


def has_inbox_mail(counts: Mapping[str, int]) -> bool:
    """True when any INBOX-cased folder has one or more unread messages."""
    return any(
        name.casefold() == "inbox" and count > 0 for name, count in counts.items()
    )


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

    In the reader's own zone, as every calendar view renders a moment
    (`chronos.views` starts each of its three with
    ``start.astimezone()``).  Stored times are UTC, so at UTC+2 mail
    that arrived at 00:30 this morning was rendered ``Jan 05`` -- a
    date, for mail that came in a few hours ago -- because its UTC day
    was yesterday.

    The zone is *now*'s, which is the system's when nobody says
    otherwise.  That is what makes the rule testable without setting
    ``TZ`` for the whole process: a caller that passes a moment in
    another zone gets the row that reader would see.
    """
    moment = now if now is not None else datetime.now().astimezone()
    local = received_at.astimezone(moment.tzinfo)
    if local.date() == moment.date():
        return local.strftime("%H:%M")
    if local.year == moment.year:
        return local.strftime("%b %d")
    return local.strftime("%Y-%m-%d")


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


@dataclass(frozen=True, kw_only=True)
class ContactRow:
    """One line of the contacts list.

    The addresses are summarised rather than listed in full — a contact
    with a dozen of them would push the organisation off the end of any
    column — and the count of what is hidden travels as part of the
    text, as it does for the message list's marks.
    """

    mark: str
    last_name: str
    first_name: str
    emails: str
    organization: str
    marked: bool = False


def contact_row(
    contact: Contact,
    *,
    marked: bool = False,
    max_emails: int = 3,
) -> ContactRow:
    """Project *contact* into the fields both contact lists show."""
    emails = ", ".join(contact.emails[:max_emails])
    hidden = len(contact.emails) - max_emails
    if hidden > 0:
        emails += f" (+{hidden})"
    return ContactRow(
        mark=MARKED_MARK if marked else NO_MARK,
        last_name=contact.last_name,
        first_name=contact.first_name,
        emails=emails,
        organization=contact.organization,
        marked=marked,
    )


__all__ = [
    "ANSWERED_MARK",
    "ContactRow",
    "FolderTreeNode",
    "ATTACHMENT_MARK",
    "FLAGGED_MARK",
    "MARKED_MARK",
    "NO_MARK",
    "MessageRow",
    "build_folder_tree",
    "contact_row",
    "format_received",
    "has_inbox_mail",
    "message_row",
    "status_mark",
    "summary_of",
]
