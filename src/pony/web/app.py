"""The browser front end: routes over the same actions the TUI calls.

This is a second front end, not a second program.  Every operation it
offers is a call into :mod:`pony.mailbox_actions` or
:mod:`chronos.event_actions`, every row it renders comes from
:mod:`pony.views` or :mod:`chronos.views`, and every key it answers to
comes from :mod:`pony.keymap`.  What is new here is HTTP and HTML.

Three deliberate restrictions in this first version:

* **It does not sync.** The mail half has no cross-process sync lock
  (the calendar's ``chronos.locking`` is not used for it), so a web
  server syncing while a terminal is open would be two syncs against one
  account — the thing the architecture says must not happen.  What the
  browser does instead is make *local* mutations: archive, trash and
  flag changes rewrite the index row, and the next ``pony sync`` from
  anywhere pushes them.  That is the ``uid IS NULL`` design doing
  exactly what it was built for.
* **It does not send.** Composing needs SMTP credentials and a form; the
  pieces are in :mod:`pony.delivery` already, and this version does not
  wire them up.
* **It renders a message body in a sandboxed frame.** Mail is hostile
  input.  ``build_browser_html`` already inlines ``cid:`` images as data
  URIs, so the frame needs no network access and is served with
  everything switched off.

Keys the browser cannot do yet still appear in ``/keymap.json``, marked
unavailable, so the help overlay tells the truth instead of the page
silently ignoring a press.
"""

from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from chronos.outcome import Outcome

from .. import mailbox_actions
from ..domain import FolderRef, MessageFlag, MessageRef
from ..keymap import MAIL_KEYMAP, web_key
from ..mailbox_actions import Mailboxes
from ..message_renderer import build_browser_html
from ..views import build_folder_tree, message_row
from .html import Raw, document, element, join

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from starlette.requests import Request

    from ..calendar import CalendarRuntime
    from ..domain import AppConfig, FolderMessageSummary, IndexedMessage
    from ..protocols import ContactRepository, IndexRepository, MirrorRepository
    from ..views import FolderTreeNode


def _static_dir() -> Path:
    """Where the stylesheet and the keymap dispatcher are.

    Beside this module normally; under the unpacked bundle when frozen,
    because PyInstaller puts data files there rather than inside the
    package (see ``pony.spec``).
    """
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen is not None:
        return Path(frozen) / "pony" / "web" / "static"
    return Path(__file__).parent / "static"


STATIC_DIR = _static_dir()

# The cookie the token is remembered in, so a phone needs the token in
# the URL once rather than on every request.
TOKEN_COOKIE = "pony_web_token"


@dataclass(frozen=True, slots=True)
class WebServices:
    """Everything a request needs, opened once at startup."""

    config: AppConfig
    index: IndexRepository
    mirrors: dict[str, MirrorRepository]
    contacts: ContactRepository | None = None
    calendar: CalendarRuntime | None = None
    # None disables the check entirely, which is what the tests use and
    # what makes sense behind a tailnet that already authenticates the
    # device.
    token: str | None = None

    @property
    def mailboxes(self) -> Mailboxes:
        return Mailboxes(self.config, self.index, self.mirrors)


def build_web_app(services: WebServices) -> Starlette:
    """The ASGI application, with *services* bound into every route."""
    routes = [
        Route("/", _handler(services, _index_page)),
        Route("/folders", _handler(services, _folders_fragment)),
        Route("/f/{account}/{folder}", _handler(services, _folder_page)),
        Route("/f/{account}/{folder}/rows", _handler(services, _rows_fragment)),
        Route(
            "/m/{account}/{folder}/{message_id:int}",
            _handler(services, _message_page),
        ),
        Route(
            "/m/{account}/{folder}/{message_id:int}/body",
            _handler(services, _message_body),
        ),
        Route("/agenda", _handler(services, _agenda_page)),
        Route("/keymap.json", _handler(services, _keymap)),
        Route("/action/{name}", _handler(services, _run_action), methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    return Starlette(routes=routes)


def _handler(
    services: WebServices,
    view: Callable[[WebServices, Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    """Bind *services* to *view* and gate it on the token."""

    async def endpoint(request: Request) -> Response:
        refused = _check_token(services, request)
        if refused is not None:
            return refused
        response = await view(services, request)
        _remember_token(services, request, response)
        return response

    return endpoint


def _check_token(services: WebServices, request: Request) -> Response | None:
    """``None`` when the request may proceed, else what to send instead.

    The token arrives once in the URL — which is what makes this usable
    from a phone — and is then kept in a cookie.  Comparison is
    constant-time because the token is a secret and the endpoint is
    reachable by anything that can route to the port.
    """
    expected = services.token
    if expected is None:
        return None
    offered = request.query_params.get("token") or request.cookies.get(TOKEN_COOKIE)
    if offered is not None and secrets.compare_digest(offered, expected):
        return None
    return PlainTextResponse("Not authorised.", status_code=403)


def _remember_token(
    services: WebServices,
    request: Request,
    response: Response,
) -> None:
    if services.token is None:
        return
    if request.query_params.get("token") != services.token:
        return
    response.set_cookie(
        TOKEN_COOKIE,
        services.token,
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 24 * 365,
    )


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------


async def _index_page(services: WebServices, request: Request) -> Response:
    """Open the first folder there is, or say there is nothing to open."""
    del request
    first = _first_folder(services)
    if first is None:
        return HTMLResponse(
            _shell(
                services,
                title="Pony Express",
                main=element(
                    "p",
                    "No folders yet. Run 'pony sync' to fetch some mail.",
                    class_="empty",
                ),
                selected=None,
            ),
        )
    return RedirectResponse(_folder_url(first))


async def _folder_page(services: WebServices, request: Request) -> Response:
    folder = _folder_from(request)
    return HTMLResponse(
        _shell(
            services,
            title=f"{folder.folder_name} — Pony Express",
            main=_rows(services, folder),
            selected=folder,
        ),
    )


async def _message_page(services: WebServices, request: Request) -> Response:
    """The list with one message open beside (or below) it."""
    folder = _folder_from(request)
    message = _message_from(services, request)
    if message is None:
        return PlainTextResponse("No such message.", status_code=404)
    # Opening a message is what marks it read, as it is in the reader.
    mailbox_actions.add_flag(services.mailboxes, message, MessageFlag.SEEN)
    return HTMLResponse(
        _shell(
            services,
            title=f"{message.subject or '(no subject)'} — Pony Express",
            main=join(
                (
                    _rows(services, folder, open_id=message.message_ref.id),
                    _reader(message),
                )
            ),
            selected=folder,
        ),
    )


async def _message_body(services: WebServices, request: Request) -> Response:
    """The message's own HTML, for the sandboxed frame.

    Served with everything switched off: no scripts, no plugins, no
    network. ``build_browser_html`` has already inlined the inline
    images as data URIs, so there is nothing legitimate left to fetch.
    """
    message = _message_from(services, request)
    if message is None:
        return PlainTextResponse("No such message.", status_code=404)
    raw = _raw_bytes(services, message)
    if raw is None:
        return PlainTextResponse("Message bytes are missing.", status_code=410)
    return HTMLResponse(
        build_browser_html(raw),
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
            ),
            "Referrer-Policy": "no-referrer",
        },
    )


async def _agenda_page(services: WebServices, request: Request) -> Response:
    del request
    calendar = services.calendar
    if calendar is None:
        return HTMLResponse(
            _shell(
                services,
                title="Agenda — Pony Express",
                main=element("p", "No calendar configured.", class_="empty"),
                selected=None,
            ),
        )
    from chronos.views import (
        CalendarSelection,
        agenda_window,
        all_calendar_refs,
        format_event_row,
        gather_occurrences,
    )

    now = datetime.now(UTC)
    calendars = all_calendar_refs(calendar.config, calendar.mirror)
    rows = gather_occurrences(
        index=calendar.index,
        calendars=calendars,
        # Empty selection means every calendar, which is what an
        # agenda with no picker should show.
        selection=CalendarSelection(refs=frozenset()),
        window=agenda_window(now.astimezone().date()),
    )
    today = now.astimezone().date()
    entries = [
        element(
            "li",
            element("span", row.cells[0], class_="agenda-day"),
            element("span", row.cells[1], class_="agenda-time"),
            element("span", row.cells[3], class_="agenda-summary"),
            element("span", row.cells[5], class_="agenda-location"),
            class_=f"agenda-row {row.emphasis}",
        )
        for row in (format_event_row(occurrence, today, now=now) for occurrence in rows)
    ]
    main = (
        element("ul", join(entries), class_="agenda")
        if entries
        else element("p", "Nothing in the next fortnight.", class_="empty")
    )
    return HTMLResponse(
        _shell(services, title="Agenda — Pony Express", main=main, selected=None),
    )


# ----------------------------------------------------------------------
# Fragments, fetched to refresh part of a page
# ----------------------------------------------------------------------


async def _folders_fragment(services: WebServices, request: Request) -> Response:
    del request
    return HTMLResponse(_folders(services, selected=None))


async def _rows_fragment(services: WebServices, request: Request) -> Response:
    return HTMLResponse(_rows(services, _folder_from(request)))


async def _keymap(services: WebServices, request: Request) -> Response:
    """The one keymap, as the browser needs to match it.

    ``available`` is what keeps the help honest: a key the terminal has
    and the browser has not is listed and marked, rather than appearing
    to work.
    """
    del services, request
    return JSONResponse(
        [
            {
                "key": web_key(binding.key),
                "action": binding.action,
                "description": binding.description,
                "surface": binding.surface,
                "show": binding.show,
                "available": binding.action in SERVER_ACTIONS
                or binding.action in CLIENT_ACTIONS,
            }
            for binding in MAIL_KEYMAP
        ],
    )


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------

# Actions the browser performs for itself, with no request: moving the
# selection, opening the help. Named here so `/keymap.json` can say they
# work.
CLIENT_ACTIONS = frozenset(
    {
        "cursor_down",
        "cursor_up",
        "cursor_first",
        "cursor_last",
        "mark_down",
        "mark_up",
        "show_help",
        "toggle_calendar",
    },
)


def _archive(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    folder: FolderRef,
) -> Outcome:
    return mailbox_actions.archive_messages(mailboxes, messages, folder)


def _trash(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    folder: FolderRef,
) -> Outcome:
    del folder
    return mailbox_actions.trash_messages(mailboxes, messages, now=datetime.now(UTC))


def _toggle_flagged(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    folder: FolderRef,
) -> Outcome:
    del folder
    return mailbox_actions.toggle_flag(mailboxes, messages, MessageFlag.FLAGGED)


def _mark_unread(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    folder: FolderRef,
) -> Outcome:
    del folder
    return mailbox_actions.set_flag(
        mailboxes,
        messages,
        MessageFlag.SEEN,
        present=False,
    )


def _mark_all_read(
    mailboxes: Mailboxes,
    messages: Sequence[IndexedMessage],
    folder: FolderRef,
) -> Outcome:
    del messages
    return mailbox_actions.mark_folder_read(mailboxes, folder)


# Each entry is exactly the call the TUI screen makes for the same key.
SERVER_ACTIONS: dict[
    str,
    Callable[[Mailboxes, Sequence[IndexedMessage], FolderRef], Outcome],
] = {
    "archive": _archive,
    "trash": _trash,
    "toggle_flagged": _toggle_flagged,
    "mark_unread": _mark_unread,
    "mark_all_read": _mark_all_read,
}


async def _run_action(services: WebServices, request: Request) -> Response:
    """Run one named action over the rows the page says are selected.

    The response is the outcome as data — a count and the notices — and
    the page decides how to show them, exactly as the screen does with
    ``_report``.
    """
    name = request.path_params["name"]
    action = SERVER_ACTIONS.get(name)
    if action is None:
        return JSONResponse(
            {
                "changed": 0,
                "notices": [
                    {
                        "text": f"{name} is not available in the browser.",
                        "severity": "warning",
                    },
                ],
            },
            status_code=404,
        )
    # Parsed here rather than through `request.form()`, which reaches
    # for python-multipart: the only body this endpoint ever receives is
    # a short urlencoded one, and a third dependency to read it would be
    # a poor trade.
    fields = parse_qs((await request.body()).decode("utf-8", "replace"))
    folder = FolderRef(
        account_name=_first(fields, "account"),
        folder_name=_first(fields, "folder"),
    )
    ids = [value for value in fields.get("id", []) if value.isdigit()]
    refs = [
        MessageRef(
            account_name=folder.account_name,
            folder_name=folder.folder_name,
            id=int(message_id),
        )
        for message_id in ids
    ]
    messages = mailbox_actions.resolve_messages(services.mailboxes, refs)
    outcome = action(services.mailboxes, messages, folder)
    return JSONResponse(
        {
            "changed": outcome.changed,
            "notices": [
                {"text": notice.text, "severity": notice.severity}
                for notice in outcome.notices
            ],
        },
    )


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _shell(
    services: WebServices,
    *,
    title: str,
    main: Raw,
    selected: FolderRef | None,
) -> str:
    """The page every route returns: sidebar, main pane, help overlay."""
    data = {"account": "", "folder": ""}
    if selected is not None:
        data = {"account": selected.account_name, "folder": selected.folder_name}
    return document(
        title=title,
        stylesheets=("/static/pony.css",),
        scripts=("/static/pony.js",),
        data=data,
        body=join(
            (
                element(
                    "nav",
                    _folders(services, selected=selected),
                    id="folders",
                ),
                element("main", main, id="main"),
                element("div", "", id="notices", role="status", aria_live="polite"),
                element("dialog", "", id="help"),
            ),
        ),
    )


def _folders(services: WebServices, *, selected: FolderRef | None) -> Raw:
    """The folder tree, one list per account."""
    sections: list[Raw] = [
        element(
            "a",
            "Agenda",
            href="/agenda",
            class_="agenda-link",
        ),
    ]
    for account in services.config.accounts:
        mirror = services.mirrors.get(account.name)
        if mirror is None:
            continue
        names = [
            ref.folder_name for ref in mirror.list_folders(account_name=account.name)
        ]
        counts = services.index.unread_counts_by_folder(account_name=account.name)
        tree = build_folder_tree(
            folder_names=names,
            unread_counts=counts,
            account_name=account.name,
        )
        sections.append(
            element(
                "section",
                element("h2", account.name),
                element("ul", join(_folder_nodes(tree, selected)), class_="tree"),
            ),
        )
    return join(sections)


def _folder_nodes(
    nodes: tuple[FolderTreeNode, ...],
    selected: FolderRef | None,
) -> list[Raw]:
    items: list[Raw] = []
    for node in nodes:
        unread = node.descendant_unread()
        label = element("span", node.label, class_="folder-name")
        count = element("span", str(unread), class_="unread") if unread else None
        # A synthetic level — one that exists only because a folder name
        # was hierarchical — has nothing to open.
        body = (
            element(
                "a",
                label,
                count,
                href=_folder_url(node.folder_ref),
                class_="folder" + (" current" if node.folder_ref == selected else ""),
            )
            if node.folder_ref is not None
            else element("span", label, count, class_="folder synthetic")
        )
        children = (
            element("ul", join(_folder_nodes(node.children, selected)), class_="tree")
            if node.children
            else None
        )
        items.append(element("li", body, children))
    return items


def _rows(
    services: WebServices,
    folder: FolderRef,
    *,
    open_id: int | None = None,
) -> Raw:
    """The message list, newest first, as one selectable list."""
    summaries = services.index.list_folder_message_summaries(folder=folder)
    now = datetime.now(UTC)
    rendered = [
        _row(summary, folder, now=now, is_open=summary.message_ref.id == open_id)
        for summary in summaries
    ]
    if not rendered:
        return element("p", "Nothing here.", class_="empty")
    return element(
        "ol",
        join(rendered),
        id="rows",
        class_="rows",
        data_account=folder.account_name,
        data_folder=folder.folder_name,
    )


def _row(
    summary: FolderMessageSummary,
    folder: FolderRef,
    *,
    now: datetime,
    is_open: bool,
) -> Raw:
    row = message_row(summary, show_recipients=False, now=now)
    classes = ["row"]
    if row.unread:
        classes.append("unread")
    if is_open:
        classes.append("open")
    return element(
        "li",
        element(
            "a",
            element("span", row.icon, class_="mark"),
            element("span", row.date, class_="date"),
            element("span", row.correspondent, class_="who"),
            element("span", row.subject, class_="subject"),
            href=f"/m/{folder.account_name}/{folder.folder_name}/{summary.message_ref.id}",
        ),
        class_=" ".join(classes),
        data_id=str(summary.message_ref.id),
        aria_selected="true" if is_open else "false",
    )


def _reader(message: IndexedMessage) -> Raw:
    """The headers, the attachment list, and a frame for the body."""
    path = (
        f"/m/{message.message_ref.account_name}"
        f"/{message.message_ref.folder_name}/{message.message_ref.id}/body"
    )
    return element(
        "article",
        element(
            "header",
            element("h1", message.subject or "(no subject)"),
            element("p", message.sender, class_="from"),
            element("p", message.recipients, class_="to"),
        ),
        element(
            "iframe",
            "",
            src=path,
            title="Message body",
            # Mail is hostile input: nothing in this frame may run, and
            # it may not reach back out to the page around it.
            sandbox="",
            loading="lazy",
            class_="body",
        ),
        id="reader",
    )


# ----------------------------------------------------------------------
# Request plumbing
# ----------------------------------------------------------------------


def _first(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name, [])
    return values[0] if values else ""


def _folder_from(request: Request) -> FolderRef:
    return FolderRef(
        account_name=request.path_params["account"],
        folder_name=request.path_params["folder"],
    )


def _message_from(services: WebServices, request: Request) -> IndexedMessage | None:
    return services.index.get_message(
        message_ref=MessageRef(
            account_name=request.path_params["account"],
            folder_name=request.path_params["folder"],
            id=int(request.path_params["message_id"]),
        ),
    )


def _raw_bytes(services: WebServices, message: IndexedMessage) -> bytes | None:
    mirror = services.mirrors.get(message.message_ref.account_name)
    if mirror is None:
        return None
    folder = FolderRef(
        account_name=message.message_ref.account_name,
        folder_name=message.message_ref.folder_name,
    )
    try:
        return mirror.get_message_bytes(
            folder=folder,
            storage_key=message.storage_key,
        )
    except Exception:  # noqa: BLE001 — a missing file is a 410, not a crash
        return None


def _first_folder(services: WebServices) -> FolderRef | None:
    for account in services.config.accounts:
        mirror = services.mirrors.get(account.name)
        if mirror is None:
            continue
        for ref in mirror.list_folders(account_name=account.name):
            return ref
    return None


def _folder_url(folder: FolderRef | None) -> str:
    if folder is None:
        return "/"
    return f"/f/{folder.account_name}/{folder.folder_name}"


__all__ = [
    "CLIENT_ACTIONS",
    "SERVER_ACTIONS",
    "STATIC_DIR",
    "TOKEN_COOKIE",
    "WebServices",
    "build_web_app",
]
