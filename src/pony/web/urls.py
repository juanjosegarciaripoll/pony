"""Where each page lives, and how to address a folder safely.

Folder names are the reason this module exists.  A mail folder may be
called ``INBOX/2024`` or ``Archiv & Co`` or ``Correo enviado``, and a
path segment cannot hold the first of those: Starlette's ``str``
convertor stops at a slash, and percent-encoding does not help because
the ASGI scope carries a decoded path.  So account, folder and message
are query parameters throughout, which works for every name a server
can hand us.

Every link in the front end is built here, so a route and the link to it
cannot drift apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlencode

if TYPE_CHECKING:
    from ..domain import FolderRef, MessageRef

MAIL = "/mail"
ROWS = "/mail/rows"
MESSAGE = "/message"
BODY = "/message/body"
RAW = "/message/raw"
ATTACHMENT = "/message/attachment"
COMPOSE = "/compose"
CONTACTS = "/contacts"
CONTACT_FORM = "/contacts/edit"
CALENDAR = "/calendar"
EVENT_FORM = "/calendar/event"
SEARCH = "/search"


def query(**params: str | int | None) -> str:
    """``?a=1&b=2`` from the parameters that have a value."""
    pairs = [
        (name, str(value))
        for name, value in params.items()
        if value is not None and value != ""
    ]
    return f"?{urlencode(pairs)}" if pairs else ""


def mail_url(folder: FolderRef | None, *, q: str = "") -> str:
    if folder is None:
        return MAIL
    return MAIL + query(
        account=folder.account_name,
        folder=folder.folder_name,
        q=q or None,
    )


def rows_url(folder: FolderRef, *, q: str = "") -> str:
    return ROWS + query(
        account=folder.account_name,
        folder=folder.folder_name,
        q=q or None,
    )


def message_url(ref: MessageRef, *, q: str = "") -> str:
    return MESSAGE + _message_query(ref, q=q)


def body_url(ref: MessageRef) -> str:
    return BODY + _message_query(ref)


def raw_url(ref: MessageRef) -> str:
    return RAW + _message_query(ref)


def attachment_url(ref: MessageRef, index: int) -> str:
    return ATTACHMENT + _message_query(ref) + f"&n={index}"


def compose_url(
    *,
    mode: str = "new",
    account: str | None = None,
    folder: str | None = None,
    message_id: int | None = None,
    to: str | None = None,
) -> str:
    return COMPOSE + query(
        mode=mode,
        account=account,
        folder=folder,
        id=message_id,
        to=to,
    )


def contacts_url(*, q: str = "") -> str:
    return CONTACTS + query(q=q or None)


def contact_form_url(contact_id: int | None = None) -> str:
    return CONTACT_FORM + query(id=contact_id)


def calendar_url(
    *,
    view: str = "agenda",
    date: str | None = None,
    span: int | None = None,
    window: str | None = None,
) -> str:
    return CALENDAR + query(view=view, date=date, span=span, window=window)


def event_form_url(
    *,
    account: str | None = None,
    calendar: str | None = None,
    uid: str | None = None,
    date: str | None = None,
) -> str:
    return EVENT_FORM + query(
        account=account,
        calendar=calendar,
        uid=uid,
        date=date,
    )


def _message_query(ref: MessageRef, *, q: str = "") -> str:
    return query(
        account=ref.account_name,
        folder=ref.folder_name,
        id=ref.id,
        q=q or None,
    )


__all__ = [
    "ATTACHMENT",
    "BODY",
    "CALENDAR",
    "COMPOSE",
    "CONTACTS",
    "CONTACT_FORM",
    "EVENT_FORM",
    "MAIL",
    "MESSAGE",
    "RAW",
    "ROWS",
    "SEARCH",
    "attachment_url",
    "body_url",
    "calendar_url",
    "compose_url",
    "contact_form_url",
    "contacts_url",
    "event_form_url",
    "mail_url",
    "message_url",
    "query",
    "raw_url",
    "rows_url",
]
