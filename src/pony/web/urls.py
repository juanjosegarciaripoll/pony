"""Where each page lives, and how to address a folder safely.

Folder names are the reason this module exists.  A mail folder may be
called ``INBOX/2024`` or ``Archiv & Co`` or ``Correo enviado``, and a
path segment cannot hold the first of those: Starlette's ``str``
convertor stops at a slash, and percent-encoding does not help because
the ASGI scope carries a decoded path.  So account, folder and message
are query parameters throughout, which works for every name a server
can hand us.

Every link in the front end is built here, so a route and the link to it
cannot drift apart -- which it had twice over by the time anyone looked:
a ``SEARCH = "/search"`` naming a route that 404s, and a ``rows_url``
with no callers at all.  ``tests/test_web_dispatch.py`` now walks every
constant and every builder in here against the running application, so
neither a dead name nor a renamed query parameter can sit here quietly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlencode

if TYPE_CHECKING:
    from ..mail.domain import FolderRef, MessageRef
    from ..mail.message_export import PartKey

MAIL = "/mail"
ROWS = "/mail/rows"
PANES = "/mail/panes"
MESSAGE = "/message"
BODY = "/message/body"
RAW = "/message/raw"
ATTACHMENT = "/message/attachment"
PARTS = "/message/parts"
COMPOSE = "/compose"
CONTACTS = "/contacts"
CONTACT_FORM = "/contacts/edit"
CALENDAR = "/calendar"
EVENT_FORM = "/calendar/event"
#: What a sync in flight is doing.  Polled rather than pushed: the
#: ``POST`` that started it says nothing until it is over.
SYNC_STATUS = "/sync/status"


def query(**params: str | int | None) -> str:
    """``?a=1&b=2`` from the parameters that have a value."""
    pairs = [
        (name, str(value))
        for name, value in params.items()
        if value is not None and value != ""
    ]
    return f"?{urlencode(pairs)}" if pairs else ""


def mail_url(
    folder: FolderRef | None,
    *,
    q: str = "",
    query_text: str = "",
    offset: int = 0,
    selected: int | None = None,
) -> str:
    """The folder's page, optionally from *offset* rows down.

    *offset* is how a page with no script reaches the next batch of a
    long folder, and it is the one the script asks for too.

    *selected* is the row the cursor is on.  It travels because nothing
    else carries it: the cursor is the page's own state, so a folder URL
    without it opens the list at the top -- which is where every return
    to the list used to land, however far down the folder the reader
    had been.  Spelled ``id``, as it is everywhere else a message is
    named.
    """
    if folder is None:
        return MAIL
    return MAIL + query(
        account=folder.account_name,
        folder=folder.folder_name,
        q=(q or query_text) or None,
        offset=offset or None,
        id=selected,
    )


def message_url(ref: MessageRef, *, q: str = "") -> str:
    return MESSAGE + _message_query(ref, q=q)


def body_url(ref: MessageRef) -> str:
    return BODY + _message_query(ref)


def raw_url(ref: MessageRef) -> str:
    return RAW + _message_query(ref)


def part_url(ref: MessageRef, key: PartKey) -> str:
    """Where one part of a message is served: ``n=0``, ``n=T``, ``n=3``.

    The same names the reader's prompt takes, so a link and a keystroke
    mean the same thing.
    """
    return ATTACHMENT + _message_query(ref) + f"&n={key}"


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
    "PANES",
    "PARTS",
    "RAW",
    "ROWS",
    "SYNC_STATUS",
    "part_url",
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
]
