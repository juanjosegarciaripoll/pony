"""What an action gets to work with, and what it gives back.

``POST /action/<name>`` is the browser's one way of changing something,
and every action behind it has the same shape as the terminal's: it is
handed what the page says is selected, it calls one model operation, and
it returns an :class:`~chronos.outcome.Outcome` — a count and the
notices to show.  The page decides how to show them, exactly as
``MainScreen._report`` does.

The body is parsed here rather than through ``request.form()``, which
reaches for ``python-multipart``: the only bodies this endpoint receives
are short urlencoded ones, and a third dependency to read them would be
a poor trade.  Uploaded attachment bytes arrive on their own route
instead (see :mod:`pony.web.compose`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ..domain import FolderRef, MessageRef

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from chronos.domain import CalendarRef
    from chronos.outcome import Outcome

    from ..domain import IndexedMessage
    from .app import WebServices


@dataclass(frozen=True, slots=True)
class ActionContext:
    """One action's arguments: the services, and what the page sent."""

    services: WebServices
    fields: Mapping[str, list[str]]

    @staticmethod
    def of(services: WebServices, body: bytes) -> ActionContext:
        return ActionContext(
            services=services,
            fields=parse_qs(body.decode("utf-8", "replace")),
        )

    def first(self, name: str, default: str = "") -> str:
        values = self.fields.get(name, [])
        return values[0] if values else default

    def every(self, name: str) -> list[str]:
        return self.fields.get(name, [])

    def integer(self, name: str) -> int | None:
        value = self.first(name)
        return int(value) if value.isdigit() else None

    @property
    def folder(self) -> FolderRef:
        return FolderRef(
            account_name=self.first("account"),
            folder_name=self.first("folder"),
        )

    @property
    def ids(self) -> list[int]:
        """The row ids the page says are selected, bad ones dropped."""
        return [int(value) for value in self.every("id") if value.isdigit()]

    @property
    def refs(self) -> list[MessageRef]:
        folder = self.folder
        return [
            MessageRef(
                account_name=folder.account_name,
                folder_name=folder.folder_name,
                id=message_id,
            )
            for message_id in self.ids
        ]

    @property
    def messages(self) -> list[IndexedMessage]:
        from .. import mailbox_actions

        return mailbox_actions.resolve_messages(
            self.services.mailboxes,
            self.refs,
        )

    @property
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Confirmation:
    """A question the page must put to the user before acting.

    The model decides what is dangerous and what to ask -- this is only
    how the answer gets back: the page re-sends the same action with
    *fields* added once the user agrees.  It is the browser's spelling
    of the terminal's confirmation dialog.
    """

    question: str
    fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Reply:
    """What an action gives back when a bare outcome is not enough."""

    outcome: Outcome
    confirm: Confirmation | None = None


# What every action behind `POST /action/<name>` looks like.  Lazily
# evaluated, so the imports it names stay under TYPE_CHECKING.
type Action = Callable[[ActionContext], Outcome | Reply]


# Actions whose label says "marked".  They act on the marks and on
# nothing else, so the page must send the marks alone rather than
# falling back to the cursor -- which, since the cursor starts on the
# first row, meant that tapping "Delete marked" on a freshly loaded
# contacts list deleted the first contact.  `static/pony.js` keeps the
# same set, and `tests/test_web_dispatch.py` fails if the two differ.
MARKED_ONLY_ACTIONS = frozenset({"delete_marked", "merge_marked"})


def holds_form(outcome: Outcome) -> bool:
    """Whether a form must stay up with what the user typed still in it.

    One rule for all three forms, because they had three.  Compose and
    the event form tested :attr:`~chronos.outcome.Outcome.blocked`,
    which is ``failed or not acted`` -- so a save that legitimately
    changed nothing read as a failure and the form stayed up with
    nothing to explain it.  The contact form tested whether a record
    came back, which is a third thing again.

    What a form needs to know is whether anything is *wrong*.  A
    refusal is a warning rather than an error, deliberately, because
    throwing away what somebody typed over a warning loses their work.
    """
    return outcome.failed or any(
        notice.severity != "information" for notice in outcome.notices
    )


def held_for_confirmation(
    context: ActionContext,
    question: str,
) -> Reply | None:
    """*question* for the user, or ``None`` once they have answered it.

    The browser's spelling of the terminal's ``ConfirmScreen``, for an
    operation that cannot be taken back: nothing happens, the page is
    handed the question, and answering it re-sends the same action with
    ``confirm`` added.  One helper so that every irreversible operation
    asks the same way and none of them forgets to ask at all.
    """
    from chronos.outcome import Outcome

    if context.first("confirm"):
        return None
    return Reply(Outcome(), confirm=Confirmation(question, {"confirm": "1"}))


def folder_value(ref: FolderRef) -> str:
    """``account/folder``, as a prompt, a label and a refusal spell it.

    The inverse of :func:`parse_target`, beside it, so the two cannot
    drift: the prompt's options and what it accepts back are one
    spelling.
    """
    return f"{ref.account_name}/{ref.folder_name}"


def parse_target(value: str) -> FolderRef | None:
    """``account/folder`` as the move and copy prompts spell it.

    The folder name may itself contain slashes, so only the first one
    separates: ``work/INBOX/2024`` is the ``INBOX/2024`` folder of the
    ``work`` account.
    """
    account, _, folder = value.partition("/")
    if not account or not folder:
        return None
    return FolderRef(account_name=account, folder_name=folder)


def calendar_value(ref: CalendarRef) -> str:
    """``account/name``, as a chooser, a row and a form spell a calendar.

    One spelling, because there were eleven: every ``<option>``, every
    hidden field and every row that names a calendar went through its
    own f-string, and five more places took them apart again with a
    hand-written ``partition`` and a refusal message of their own.
    """
    return f"{ref.account_name}/{ref.calendar_name}"


def parse_calendar(value: str) -> CalendarRef | None:
    """The calendar *value* names, or ``None`` when it names none.

    The same rule as :func:`parse_target`: only the first slash
    separates, because a calendar name may contain one and an account
    name may not.
    """
    from chronos.domain import CalendarRef

    account, _, name = value.partition("/")
    if not account or not name:
        return None
    return CalendarRef(account, name)


__all__ = [
    "MARKED_ONLY_ACTIONS",
    "Action",
    "ActionContext",
    "Confirmation",
    "Reply",
    "calendar_value",
    "folder_value",
    "held_for_confirmation",
    "holds_form",
    "parse_calendar",
    "parse_target",
]
