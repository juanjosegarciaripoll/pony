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


# What every action behind `POST /action/<name>` looks like.  Lazily
# evaluated, so the imports it names stay under TYPE_CHECKING.
type Action = Callable[[ActionContext], Outcome]


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


__all__ = ["Action", "ActionContext", "parse_target"]
