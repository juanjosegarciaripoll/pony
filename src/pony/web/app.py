"""The browser front end: routes over the same actions the TUI calls.

This is a second front end, not a second program.  Every operation it
offers is a call into :mod:`pony.mailbox_actions`, :mod:`pony.delivery`,
:mod:`pony.contact_actions`, :mod:`pony.invitation_actions` or
:mod:`pony.calendar.event_actions`; every row it renders comes from
:mod:`pony.views` or :mod:`pony.calendar.views`; every key it answers to comes
from :mod:`pony.keymap` or :mod:`pony.calendar.keymap`.  What is new here is
HTTP, HTML and a stylesheet.

Two things it does differently from the terminal, both deliberate:

* **A message body is shown as text**, with the HTML version one key
  away in a page of its own.  The text is what ``render_message``
  produces for the reader, the CLI and the MCP tools, and it carries
  neither tracking nor anything to execute.  The HTML version is served
  under a policy that allows it no scripts, no plugins and no network,
  and never inside the application's own page.

* **Attachments are uploaded as raw bytes** on a route of their own
  rather than through a multipart form, which keeps the extra at two
  packages.

Syncing is offered here like anything else.  The server holds one sync
slot of its own (see :mod:`pony.web.syncing`); whether a terminal is
also open is the user's business.
"""

from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode, urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from pony.calendar.outcome import Notice, Outcome, Severity, warn

from .. import contact_actions, mailbox_actions
from ..domain import FolderRef, MessageFlag, MessageRef
from ..html_policy import MESSAGE_HTML_CSP, MESSAGE_HTML_HEADERS
from ..message_export import (
    message_parts,
    parse_part_selection,
    part_key,
    part_payload,
)
from ..message_renderer import build_browser_html, render_message
from . import calendar as calendar_view
from . import compose as compose_view
from . import contacts as contacts_view
from . import mail as mail_view
from . import urls
from .actions import ActionContext, Reply, calendar_value, holds_form
from .layout import PageSurface, keymap_rows
from .syncing import SyncGate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from starlette.requests import Request

    from ..calendar_runtime import CalendarRuntime
    from ..domain import AppConfig, IndexedMessage
    from ..protocols import (
        ContactRepository,
        CredentialsProvider,
        IndexRepository,
        MirrorRepository,
    )
    from ..sync import MailSyncScheduler
    from .actions import Action


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

# How much of a notice survives a redirect.  Long enough for anything
# the actions say, short enough that the URL stays a URL.
NOTICE_LIMIT = 300

# How many notices a redirect carries before the rest become a count.
NOTICE_COUNT_LIMIT = 6


@dataclass(slots=True)
class Shutdown:
    """How the browser's quit key stops the server.

    The services are built before the server exists, so the hook is
    wired afterwards -- the same shape as :class:`SyncGate` getting its
    engine.  Keeping it behind an object rather than reaching for the
    server also keeps ``pony.web`` from knowing what uvicorn is.
    """

    stop: Callable[[], None] | None = None

    def request(self) -> bool:
        """Ask the host to stop.  False when nobody is listening."""
        if self.stop is None:
            return False
        self.stop()
        return True


@dataclass(frozen=True, slots=True)
class WebServices:
    """Everything a request needs, opened once at startup."""

    config: AppConfig
    index: IndexRepository
    mirrors: dict[str, MirrorRepository]
    contacts: ContactRepository | None = None
    calendar: CalendarRuntime | None = None
    credentials: CredentialsProvider | None = None
    # The periodic mail sync, when one is running: a manual sync takes
    # its slot for the duration so the two cannot overlap.
    scheduler: MailSyncScheduler | None = None
    # None disables the check entirely, which is what the tests use and
    # what makes sense behind a tailnet that already authenticates the
    # device.
    token: str | None = None
    # What ``Q`` does here.  The browser cannot stop a process on its
    # own, so the server is asked to stop itself; the host wires in how.
    shutdown: Shutdown = field(default_factory=Shutdown)
    sync: SyncGate = field(default_factory=SyncGate)

    @property
    def mailboxes(self) -> mailbox_actions.Mailboxes:
        return mailbox_actions.Mailboxes(self.config, self.index, self.mirrors)


def build_web_app(services: WebServices) -> Starlette:
    """The ASGI application, with *services* bound into every route."""
    _wire_sync(services)
    routes = [
        Route("/", _handler(services, _index_page)),
        Route(urls.MAIL, _handler(services, _mail_page)),
        Route(urls.ROWS, _handler(services, _rows_fragment)),
        Route(urls.PANES, _handler(services, _panes_fragment)),
        Route(urls.MESSAGE, _handler(services, _message_page)),
        Route(urls.BODY, _handler(services, _message_body)),
        Route(urls.RAW, _handler(services, _message_raw)),
        Route(urls.ATTACHMENT, _handler(services, _attachment)),
        Route(urls.PARTS, _handler(services, _parts)),
        Route(urls.COMPOSE, _handler(services, _compose_page)),
        Route(urls.COMPOSE, _handler(services, _compose_post), methods=["POST"]),
        Route(
            "/compose/attachment",
            _handler(services, _stage_attachment),
            methods=["POST"],
        ),
        Route(urls.CONTACTS, _handler(services, _contacts_page)),
        Route(urls.CONTACT_FORM, _handler(services, _contact_form)),
        Route(
            urls.CONTACT_FORM,
            _handler(services, _contact_save),
            methods=["POST"],
        ),
        Route(urls.CALENDAR, _handler(services, _calendar_page)),
        Route(urls.EVENT_FORM, _handler(services, _event_form)),
        Route(urls.EVENT_FORM, _handler(services, _event_save), methods=["POST"]),
        Route("/keymap.json", _handler(services, _keymap)),
        Route("/action/{name}", _handler(services, _run_action), methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    return Starlette(routes=routes)


def _wire_sync(services: WebServices) -> None:
    """Give the sync gate the engine and the slot it has to share."""
    gate = services.sync
    gate.calendar = services.calendar
    gate.scheduler = services.scheduler
    if services.credentials is not None and gate.service is None:
        from ..sync import build_sync_service

        gate.service = build_sync_service(
            config=services.config,
            index=services.index,
            mirrors=services.mirrors,
            credentials=services.credentials,
        )


def _handler(
    services: WebServices,
    view: Callable[[WebServices, Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    """Bind *services* to *view*, gated on the token and on the origin."""

    async def endpoint(request: Request) -> Response:
        refused = _check_token(services, request) or _check_origin(request)
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
    # `compare_digest` on two `str` raises for anything outside ASCII, so
    # `?token=%C3%A9` answered 500 with a traceback where it should answer
    # 403.  A non-ASCII offer cannot be the token anyway — that is
    # `secrets.token_urlsafe` output — so this loses no legitimate client.
    if (
        offered is not None
        and offered.isascii()
        and secrets.compare_digest(offered, expected)
    ):
        return None
    return PlainTextResponse("Not authorised.", status_code=403)


# Methods that cannot change anything, so nothing has to be proved about
# where they came from.  `GET /message` is the one read that writes, and
# it asks `_from_own_page` for itself.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# What `Sec-Fetch-Site` says when the request is Pony's own page asking,
# or the user navigating by hand.  Everything else — `cross-site`,
# `same-site` — is some other page's request, and no other page has
# business changing the mailbox.
_OWN_SITE = frozenset({"same-origin", "none"})


def _from_own_page(request: Request) -> bool:
    """Whether a browser says this request came from Pony's own page.

    Browsers label every request they make: ``Sec-Fetch-Site`` says
    where it came from, and an ``Origin`` accompanies anything that is
    not a plain navigation.  A client that sends neither — curl, a
    script, a phone shortcut — is taken at its word, because it is not a
    browser being aimed at the user's mailbox by a page they happened to
    open.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site.lower() in _OWN_SITE
    origin = request.headers.get("origin")
    return origin is None or _is_own_origin(origin, request)


def _is_own_origin(origin: str, request: Request) -> bool:
    """Whether *origin* names the host this request was addressed to.

    Compared by authority rather than by whole URL: the scheme a proxy
    terminated is not ours to know, while the host and port the browser
    dialled are in the request itself.
    """
    if origin == "null":
        return False
    offered = urlsplit(origin).netloc.lower()
    host = (request.headers.get("host") or "").lower()
    return bool(offered) and offered == host


def _check_origin(request: Request) -> Response | None:
    """Refuse a state-changing request that some other page sent.

    This is the front end's CSRF defence, and it is deliberately
    independent of the token: a token kept in a cookie rides along with
    a cross-site form post exactly as it rides along with the page's own
    requests, and under ``--no-token`` there is nothing else at all.  It
    covers every mutating route at once — ``/action/*``, ``/compose``,
    ``/compose/attachment``, ``/contacts/edit``, ``/calendar/event`` —
    because it sits in front of all of them rather than inside any one.
    """
    if request.method.upper() in _SAFE_METHODS or _from_own_page(request):
        return None
    return PlainTextResponse(
        "That request came from another site.",
        status_code=403,
    )


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
        # Security, not phone convenience: `strict` is what keeps this
        # cookie out of a request some other page made, so a token
        # remembered here cannot be spent on the user's behalf by a page
        # they opened.  `_check_origin` is the defence that holds when
        # the token is off altogether.
        samesite="strict",
        max_age=60 * 60 * 24 * 365,
    )


# ----------------------------------------------------------------------
# Mail
# ----------------------------------------------------------------------


async def _index_page(services: WebServices, request: Request) -> Response:
    """Open the first folder there is, or say there is nothing to open."""
    del request
    first = _first_folder(services)
    if first is None:
        return HTMLResponse(
            mail_view.mail_page(
                services,
                folder=FolderRef(account_name="", folder_name=""),
                notices=(
                    Notice("No folders yet. Press g to fetch some mail.", "warning"),
                ),
            ),
        )
    return RedirectResponse(urls.mail_url(first))


async def _mail_page(services: WebServices, request: Request) -> Response:
    """The reader: folders, the list, and a message if one is open.

    Rendered in the thread pool, not on the event loop: it is a SQL
    read plus a string build per row, and on the loop that is the whole
    server stopped for the duration -- the other tab, an upload in
    flight, the sync's own requests.  The thread does not make the page
    faster; it makes the wait this request's own.

    What makes it faster is the batch.  One unpaginated read of an
    8,000-row folder was 618 ms and 2.4 MB, measured; the page carries
    `ROW_BATCH` rows now and the rest arrive after it, the way the
    reader streams rows into its table.  ``offset`` is the batch to
    start at, which is also the link a page with no script follows.
    """
    folder = _folder_from(request)
    query = request.query_params.get("q", "")
    notices = _notices(request)
    return HTMLResponse(
        await run_in_threadpool(
            lambda: mail_view.mail_page(
                services,
                folder=folder,
                query=query,
                notices=notices,
                offset=_offset_from(request),
            ),
        ),
    )


def _offset_from(request: Request) -> int:
    """How far down the folder a list starts, and never off the end.

    A hand-edited ``offset`` is a number from outside: anything that is
    not a non-negative integer is the top of the folder, which is what
    the reader shows when it is asked for a row that is not there.
    """
    raw = request.query_params.get("offset", "")
    return int(raw) if raw.isdigit() else 0


async def _rows_fragment(services: WebServices, request: Request) -> Response:
    """The list on its own, for the page to put back after an action.

    ``id`` is the message the reader has open, and it has to travel:
    without it the fragment carries no ``aria-selected`` and no ``.open``
    at all, so the page lost both the cue to the reader and the cursor
    the next action works off.
    """
    identifier = request.query_params.get("id", "")
    folder = _folder_from(request)
    open_id = int(identifier) if identifier.isdigit() else None
    query = request.query_params.get("q", "")
    return HTMLResponse(
        # In the thread pool for the same reason the whole page is: this
        # is a batch of the list again, and `refresh()` asks for it
        # after every action.
        await run_in_threadpool(
            lambda: mail_view.rows(
                services,
                folder,
                open_id=open_id,
                query=query,
                offset=_offset_from(request),
            ),
        ),
    )


async def _panes_fragment(services: WebServices, request: Request) -> Response:
    """The list and the folder tree together, for one refresh.

    The unread counts beside the folders are a projection of the same
    rows the list is, so answering both in one response is a round
    trip saved on every archive and every mark-as-read -- and it
    closes the window the two requests left open, in which the list
    had been repainted from after the action and the counts still said
    what they had said before it.

    Two fragments in one document: the page parses it once and swaps
    each half into place.
    """
    identifier = request.query_params.get("id", "")
    folder = _folder_from(request)
    open_id = int(identifier) if identifier.isdigit() else None
    query = request.query_params.get("q", "")

    return HTMLResponse(
        # One hop into the pool for both halves, where two requests
        # made two.
        await run_in_threadpool(
            lambda: mail_view.panes(
                services,
                folder,
                open_id=open_id,
                query=query,
            ),
        ),
    )


def _opened_by_a_reader(request: Request) -> bool:
    """Whether somebody is really looking at this message.

    ``GET /message`` marks it read, which is right when a person opened
    it and wrong when a prefetcher, a link-preview bot or a cross-site
    ``<img src>`` asked for it.  Browsers label all three: ``Sec-Purpose``
    says ``prefetch``, and a request from some other page is not from
    Pony's own.
    """
    purpose = request.headers.get("sec-purpose", "")
    if "prefetch" in purpose or "prerender" in purpose:
        return False
    return _from_own_page(request)


async def _message_page(services: WebServices, request: Request) -> Response:
    """The list with the message open beside (or below) it.

    The render is the list again plus this message's own MIME parsing,
    so it goes to the thread pool with the rest.
    """
    message = _message_from(services, request)
    if message is None:
        return PlainTextResponse("No such message.", status_code=404)
    if _opened_by_a_reader(request):
        # Opening a message is what marks it read, as it is in the
        # reader, and this stays on the GET: reading mail is what the
        # request is.  The write goes to the thread pool because it is a
        # write -- SQLite and the mirror on the event loop stop every
        # other request for as long as they take.
        await run_in_threadpool(
            mailbox_actions.add_flag,
            services.mailboxes,
            message,
            MessageFlag.SEEN,
        )
    folder = FolderRef(
        account_name=message.message_ref.account_name,
        folder_name=message.message_ref.folder_name,
    )
    query = request.query_params.get("q", "")
    notices = _notices(request)
    return HTMLResponse(
        await run_in_threadpool(
            lambda: mail_view.mail_page(
                services,
                folder=folder,
                message=message,
                query=query,
                notices=notices,
            ),
        ),
    )


async def _message_body(services: WebServices, request: Request) -> Response:
    """The message's own HTML, as a page of its own.

    Served with everything switched off: no scripts, no plugins, no
    network, nowhere to post and nothing to re-base.
    ``build_browser_html`` has already inlined the inline images as data
    URIs, so there is nothing legitimate left to fetch.  It is a
    top-level page rather than a frame in the application, because mail
    is hostile input and the reader shows text.
    """
    # Reading the file and rebuilding the message's HTML are both the
    # thread pool's work: a message with inlined images is megabytes of
    # it, and the event loop is the whole server.
    raw = await run_in_threadpool(_raw_from, services, request)
    if isinstance(raw, Response):
        return raw
    html = await run_in_threadpool(build_browser_html, raw)
    return HTMLResponse(html, headers=dict(MESSAGE_HTML_HEADERS))


async def _message_raw(services: WebServices, request: Request) -> Response:
    """The message as it arrived, for saving it somewhere else.

    Named the way the terminal's save dialog proposes a name -- date and
    subject -- because the proposal is `message_renderer`'s now rather
    than the dialog's.  Every message downloaded as ``message.eml``
    before, so three of them were distinguished by the browser's own
    numbering and nothing else.
    """
    from ..message_renderer import proposed_filename

    raw = await run_in_threadpool(_raw_from, services, request)
    if isinstance(raw, Response):
        return raw
    name = await run_in_threadpool(
        lambda: proposed_filename(render_message(raw), suffix=".eml"),
    )
    return Response(
        raw,
        media_type="message/rfc822",
        headers={"Content-Disposition": _content_disposition(name)},
    )


async def _parts(services: WebServices, request: Request) -> Response:
    """What a typed part selection names, as JSON the dispatcher can use.

    ``select`` is the grammar the terminal's ``S`` prompt takes --
    ``0``, ``T``, ``1,3``, ``1-3``, ``*`` -- parsed by the one function
    that defines it (:func:`pony.message_export.parse_part_selection`).
    The browser had no way to reach that grammar, so ``S`` there sent
    whatever was typed to ``/message/attachment?n=`` as a single part
    name and anything else landed on a bare "Which part?" page, with
    the application navigated away from.  Now the answer comes back
    here and the page stays where it is.
    """
    raw = await run_in_threadpool(_raw_from, services, request)
    if isinstance(raw, Response):
        return raw
    ref = _optional_ref(request)
    if ref is None:  # pragma: no cover - _raw_from answered 404 already
        return JSONResponse({"error": "No such message."}, status_code=404)
    rendered = await run_in_threadpool(render_message, raw)
    total = len(rendered.attachments)
    keys = parse_part_selection(request.query_params.get("select", ""), total=total)
    if keys is None:
        return JSONResponse({"error": _part_help(total)}, status_code=400)
    # Every part can be named, listed or not: `0` and `T` are what the
    # page stopped showing, not what it stopped serving.
    names = {part.key: part.filename for part in message_parts(rendered)}
    return JSONResponse(
        {
            "parts": [
                {
                    "key": str(key),
                    "filename": names.get(key, ""),
                    "url": urls.part_url(ref, key),
                }
                for key in keys
            ]
        }
    )


def _part_help(total: int) -> str:
    """What to type, for a selection that named nothing.

    Says what this message has rather than only what the grammar is: a
    reader who typed ``3`` at a message with one attachment wants to
    know that, not to read the syntax again.
    """
    has = (
        f"1-{total} are its attachments"
        if total > 1
        else "1 is its attachment"
        if total == 1
        else "it has no attachments"
    )
    return f"Which parts? 0 is the message, T its text as a file, {has}."


async def _attachment(services: WebServices, request: Request) -> Response:
    """One part's bytes, by the name the reader lists it under.

    ``0`` is the message as it arrived and ``T`` is its body as text,
    so the browser offers the same parts the terminal's ``S`` prompt
    takes rather than attachments alone.
    """
    raw = await run_in_threadpool(_raw_from, services, request)
    if isinstance(raw, Response):
        return raw
    key = part_key(request.query_params.get("n", ""))
    if key is None:
        return PlainTextResponse("Which part?", status_code=400)
    # Parsing a multipart message to find part N is the one piece of
    # work here that grows with the attachment.
    payload = await run_in_threadpool(
        lambda: part_payload(render_message(raw), key),
    )
    if payload is None:
        return PlainTextResponse("No such part.", status_code=404)
    return Response(
        payload.data,
        media_type=payload.content_type,
        headers={
            "Content-Disposition": _content_disposition(payload.filename),
            # An attachment is the most hostile byte string the program
            # handles, and nothing should sniff it into something
            # executable.
            "X-Content-Type-Options": "nosniff",
        },
    )


# What may stand in a quoted header parameter: printable ASCII, less the
# quote and the backslash that would end it early.  Everything else —
# a control character, a newline, or any of the Cyrillic in
# `документ.pdf` — is replaced, and the real name travels in
# ``filename*`` instead.
_HEADER_SAFE = frozenset(
    chr(code) for code in range(0x20, 0x7F) if chr(code) not in {'"', "\\"}
)


def _content_disposition(filename: str) -> str:
    """``Content-Disposition`` for *filename*, as RFC 6266 spells it.

    Starlette encodes a header as latin-1, so a name carrying anything
    outside it — an ordinary Russian or Japanese attachment — raised
    ``UnicodeEncodeError`` and the file could not be downloaded at all.
    RFC 6266 answers that with two parameters: an ASCII ``filename=``
    every client understands, and a percent-encoded ``filename*=`` that
    carries the name the sender actually used.
    """
    plain = "".join(char if char in _HEADER_SAFE else "_" for char in filename)
    encoded = quote(filename, safe="")
    return (
        f'attachment; filename="{plain or "attachment"}"; '
        f"filename*=UTF-8''{encoded or 'attachment'}"
    )


# ----------------------------------------------------------------------
# Composing
# ----------------------------------------------------------------------


async def _compose_page(services: WebServices, request: Request) -> Response:
    mode = request.query_params.get("mode", "new")
    ref = _optional_ref(request)
    form = compose_view.build_form(
        services,
        mode=mode,
        ref=ref,
        to=request.query_params.get("to", ""),
    )
    if isinstance(form, Notice):
        return _back_with(urls.MAIL, Outcome.refused(form))
    return HTMLResponse(
        compose_view.compose_page(services, form, notices=_notices(request)),
    )


async def _compose_post(services: WebServices, request: Request) -> Response:
    """Send the form, or save it in Drafts, and say what happened."""
    context = ActionContext.of(services, await request.body())
    form = compose_view.form_from_fields(services.config, dict(context.fields))
    if context.first("save_draft"):
        outcome = compose_view.save_draft(services, form)
    else:
        outcome = await run_in_threadpool(compose_view.send, services, form)
    if holds_form(outcome):
        # Something is wrong, so the form stays up with what the user
        # typed still in it, exactly as the composer screen does.
        return HTMLResponse(
            compose_view.compose_page(services, form, notices=outcome.notices),
        )
    return _back_with(compose_view.back_to(form), outcome)


# The largest file this route will take.  Above it the upload is
# refused rather than read, because it arrives as one byte string in
# memory and most mail servers would reject the message anyway.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


async def _stage_attachment(services: WebServices, request: Request) -> Response:
    """Keep an uploaded file until the message it belongs to is sent."""
    name = request.query_params.get("name", "attachment")
    data = await _bounded_body(request)
    if data is None:
        megabytes = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        return JSONResponse(
            {"error": f"That file is larger than {megabytes} MB."},
            status_code=413,
        )
    if not data:
        return JSONResponse({"error": "That file is empty."}, status_code=400)
    token, path = compose_view.stage_attachment(services.config, name, data)
    return JSONResponse({"token": token, "name": path.name, "size": len(data)})


async def _bounded_body(request: Request) -> bytes | None:
    """The request body, or ``None`` when it is over the limit.

    Read a chunk at a time and given up on rather than taken whole:
    ``request.body()`` would hold whatever was sent in memory first and
    ask questions afterwards, and the default ``TMPDIR`` it then writes
    to is often a share of RAM.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_ATTACHMENT_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ATTACHMENT_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


# ----------------------------------------------------------------------
# Contacts
# ----------------------------------------------------------------------


async def _contacts_page(services: WebServices, request: Request) -> Response:
    return HTMLResponse(
        contacts_view.contacts_page(
            services,
            query=request.query_params.get("q", ""),
            notices=_notices(request),
        ),
    )


async def _contact_form(services: WebServices, request: Request) -> Response:
    contacts = services.contacts
    identifier = request.query_params.get("id", "")
    contact = contacts_view.blank_contact()
    if contacts is not None and identifier.isdigit():
        found = contact_actions.contact_by_id(contacts, int(identifier))
        if found is None:
            return PlainTextResponse("No such contact.", status_code=404)
        contact = found
    return HTMLResponse(
        contacts_view.contact_form_page(
            services,
            contact=contact,
            notices=_notices(request),
        ),
    )


async def _contact_save(services: WebServices, request: Request) -> Response:
    contacts = services.contacts
    if contacts is None:
        return _back_with(
            urls.contacts_url(),
            Outcome.refused(Notice("No contact store is open.", "warning")),
        )
    context = ActionContext.of(services, await request.body())
    edit = contacts_view.edit_from_fields(dict(context.fields))
    _saved, outcome = contacts_view.store_contact(
        contacts,
        contact_id=context.integer("id"),
        edit=edit,
    )
    if holds_form(outcome):
        return HTMLResponse(
            contacts_view.contact_form_page(
                services,
                contact=contacts_view.blank_contact(),
                edit=edit,
                notices=outcome.notices,
            ),
        )
    return _back_with(urls.contacts_url(), outcome)


# ----------------------------------------------------------------------
# Calendar
# ----------------------------------------------------------------------


async def _calendar_page(services: WebServices, request: Request) -> Response:
    view = calendar_view.read_view(
        _multi(request),
        today=datetime.now(UTC).astimezone().date(),
    )
    return HTMLResponse(
        calendar_view.calendar_page(services, view, notices=_notices(request)),
    )


async def _event_form(services: WebServices, request: Request) -> Response:
    uid = request.query_params.get("uid", "")
    if uid:
        from pony.calendar.domain import CalendarRef
        from pony.calendar.event_actions import find_component

        runtime = services.calendar
        account = request.query_params.get("account", "")
        calendar = request.query_params.get("calendar", "")
        # The link that opened this form names the calendar, and a UID
        # only identifies a component inside one.
        within = CalendarRef(account, calendar) if account and calendar else None
        found = (
            None
            if runtime is None
            else find_component(runtime.index, uid, within=within)
        )
        if found is None:
            return PlainTextResponse("No such event.", status_code=404)
        form = calendar_view.EventForm.of(found)
    else:
        day = request.query_params.get("date", "")
        try:
            viewed = (
                datetime.fromisoformat(day).date()
                if day
                else datetime.now(UTC).astimezone().date()
            )
        except ValueError:
            viewed = datetime.now(UTC).astimezone().date()
        form = calendar_view.EventForm.blank(
            target=_first_calendar(services, request),
            day=viewed,
        )
    return HTMLResponse(
        calendar_view.event_form_page(services, form, notices=_notices(request)),
    )


async def _event_save(services: WebServices, request: Request) -> Response:
    context = ActionContext.of(services, await request.body())
    form = calendar_view.form_from_fields(dict(context.fields))
    # Saving an event with attendees posts the invitations, which is an
    # SMTP conversation: off the loop, as the composer's send is.
    outcome = await run_in_threadpool(calendar_view.save, services, form)
    # `holds_form` alone is not enough here, because this form *creates*
    # a record: an event that was stored and then failed to invite its
    # attendees has something wrong with it and must still not keep a
    # form that would store it a second time.  Nothing was stored is
    # the only reason to keep the typing.
    if holds_form(outcome) and not outcome.acted:
        return HTMLResponse(
            calendar_view.event_form_page(services, form, notices=outcome.notices),
        )
    return _back_with(urls.calendar_url(), outcome)


def _first_calendar(services: WebServices, request: Request) -> str:
    """Which calendar a new event defaults to.

    The first one in view, as it is in the terminal: creating an event
    while looking at one calendar files it there.
    """
    from pony.calendar.event_actions import new_event_defaults

    refs = calendar_view.calendar_targets(services)
    today = datetime.now(UTC).astimezone().date()
    view = calendar_view.read_view(_multi(request), today=today)
    defaults = new_event_defaults(
        refs,
        day=today,
        now=datetime.now(UTC),
        preferred=view.selection.first_of(refs),
    )
    return "" if defaults is None else calendar_value(defaults.calendar)


# ----------------------------------------------------------------------
# The keymap
# ----------------------------------------------------------------------


# Actions the page performs for itself, with no request: moving the
# selection, opening a form, prompting for a folder.  Named here so the
# help overlay can say what works; `tests/test_web_app.py` checks that
# the dispatcher really implements each one.
CLIENT_ACTIONS = frozenset(
    {
        "cursor_down",
        "cursor_up",
        "close_message",
        "page_down_or_next",
        "cursor_first",
        "cursor_last",
        "mark_down",
        "mark_up",
        "show_help",
        "toggle_calendar",
        "search",
        "goto_folder",
        "compose_new",
        "compose_reply",
        "compose_reply_all",
        "compose_forward",
        "edit_draft",
        "browse_contacts",
        "open_browser",
        "print_pdf",
        "show_sender_check",
        "save_message",
        "attachments_open",
        "attachments_save",
        "narrow_folders",
        "widen_folders",
        "shrink_list",
        "grow_list",
        "move",
        "copy",
        "new_folder",
        "respond_invitation",
        # The calendar's own
        "view_agenda",
        "view_month",
        "select_span",
        "agenda_window_day",
        "agenda_window_week",
        "agenda_window_month",
        "today",
        "goto_date",
        "next_day",
        "prev_day",
        "next_week",
        "prev_week",
        "new_event",
        "edit_event",
        "open_event",
        # The keyboard picks the event out of the list and then posts,
        # which is why these last few are on both sides.
        "delete_event",
        "toggle_calendars",
        "sync_dialog",
        # Forms
        "save",
        "cancel",
        "delete",
        "send",
        "add_attachment",
        "toggle_markdown",
        # The contacts browser's
        "new_contact",
        "edit",
        "close",
    },
)


async def _keymap(services: WebServices, request: Request) -> Response:
    """Every surface's keymap, for anything that wants the whole table.

    The page it is serving carries its own surface's copy inline, from
    the same :func:`~pony.web.layout.keymap_rows`, so this is the table
    rather than the page's working copy.
    """
    del services, request
    return JSONResponse(
        {surface.value: keymap_rows(surface) for surface in PageSurface},
    )


def is_available(action: str) -> bool:
    """Whether the browser can really run *action*.

    Public because :mod:`pony.web.layout` renders the keymap into every
    page and has to mark what the browser cannot do; the action tables
    are assembled here.
    """
    if action in SERVER_ACTIONS or action in CLIENT_ACTIONS:
        return True
    # `select_span(3)` and friends carry their argument in the name.
    return action.partition("(")[0] in CLIENT_ACTIONS


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------


def _quit(context: ActionContext) -> Outcome:
    """Stop the server, as ``Q`` stops the reader.

    The page closes itself afterwards.  Both halves are needed: closing
    the tab alone leaves the process serving, and stopping the process
    alone leaves a dead page on screen.
    """
    if not context.services.shutdown.request():
        return Outcome.refused(
            warn("This server cannot stop itself; close it where it was started."),
        )
    return Outcome(changed=1, notices=(Notice("Pony Express is stopping."),))


def _sync_mail(context: ActionContext) -> Reply:
    # The halt the engine raises on a mass deletion is confirmed here
    # the way the terminal confirms it: nothing is applied until the
    # user has seen what it would do and said yes.
    return context.services.sync.mail(confirmed=bool(context.first("confirm")))


def _sync_calendar(context: ActionContext) -> Outcome:
    return context.services.sync.calendars()


SERVER_ACTIONS: dict[str, Action] = {
    **mail_view.MAIL_ACTIONS,
    **contacts_view.CONTACT_ACTIONS,
    **calendar_view.CALENDAR_ACTIONS,
    # `g` and ctrl+g both fetch mail here: a browser request is already
    # out of the way of the page, so there is nothing for a second,
    # quieter sync key to mean.
    "sync": _sync_mail,
    "background_sync": _sync_mail,
    "sync_calendar": _sync_calendar,
    "quit": _quit,
}


async def _run_action(services: WebServices, request: Request) -> Response:
    """Run one named action over the rows the page says are selected.

    The response is the outcome as data — a count and the notices — and
    the page decides how to show them, exactly as the screen does with
    ``_report``.  Anything that blocks (a sync, a send, an invitation
    reply) goes to the thread pool, so one slow action does not stop the
    rest of the page being served.
    """
    name = request.path_params["name"]
    action = SERVER_ACTIONS.get(name)
    if action is None:
        return _outcome_response(
            Outcome.refused(
                Notice(f"{name} is not available in the browser.", "warning"),
            ),
            status_code=404,
        )
    context = ActionContext.of(services, await request.body())
    reply = await run_in_threadpool(action, context)
    return _outcome_response(reply)


def _outcome_response(
    reply: Outcome | Reply,
    *,
    status_code: int = 200,
) -> JSONResponse:
    """One action's result as data, for the page to render.

    ``confirm``, when present, is a question the page has to put to the
    user before anything happens; answering it re-sends the action with
    the fields it names.
    """
    if isinstance(reply, Outcome):
        reply = Reply(reply)
    body: dict[str, object] = {
        "changed": reply.outcome.changed,
        "notices": [
            {"text": notice.text, "severity": notice.severity}
            for notice in reply.outcome.notices
        ],
    }
    if reply.confirm is not None:
        body["confirm"] = {
            "question": reply.confirm.question,
            "fields": dict(reply.confirm.fields),
        }
    return JSONResponse(body, status_code=status_code)


# ----------------------------------------------------------------------
# Request plumbing
# ----------------------------------------------------------------------


def _multi(request: Request) -> dict[str, list[str]]:
    """The query string with its repeated parameters kept."""
    found: dict[str, list[str]] = {}
    for name, value in request.query_params.multi_items():
        found.setdefault(name, []).append(value)
    return found


def _notices(request: Request) -> Sequence[Notice]:
    """What a redirect carried over, if anything.

    An action that navigates has nowhere to put its outcome but the
    URL, and the text is escaped like anything else on the way back in.
    One ``notice`` and one ``severity`` per notice, paired by position.

    Only from Pony's own page, though: this is Pony talking to itself
    across its own 303, and a link from anywhere else could put
    attacker-chosen prose inside Pony's chrome with error styling on it
    -- escaped, so phishing rather than script, and the escaping is no
    answer to "Your session expired, sign in again".  Same question as
    :func:`_check_origin` asks of a POST, and the same answer: a client
    that labels nothing is the user, not a page aimed at them.
    """
    if not _from_own_page(request):
        return ()
    texts = request.query_params.getlist("notice")
    severities = request.query_params.getlist("severity")
    return tuple(
        Notice(
            text[:NOTICE_LIMIT],
            _severity(severities[index] if index < len(severities) else ""),
        )
        # One more than a redirect writes: the last may be its own
        # "...and N more" summary.
        for index, text in enumerate(texts[: NOTICE_COUNT_LIMIT + 1])
        if text
    )


def _severity(offered: str) -> Severity:
    if offered == "warning":
        return "warning"
    if offered == "error":
        return "error"
    return "information"


def _back_with(location: str, outcome: Outcome) -> RedirectResponse:
    """Go to *location*, carrying every notice the outcome produced.

    A loop over several messages reports one notice per message, and
    the terminal shows all of them; dropping all but the last would
    hide exactly the per-message failures ``NoticeLog`` exists to
    collect.  Beyond ``NOTICE_COUNT_LIMIT`` the rest are counted rather
    than spelled out, to keep the URL a URL.
    """
    carried = list(outcome.notices[:NOTICE_COUNT_LIMIT])
    dropped = len(outcome.notices) - len(carried)
    if dropped > 0:
        carried.append(warn(f"...and {dropped} more."))
    if carried:
        pairs = [
            (name, value)
            for notice in carried
            for name, value in (
                ("notice", notice.text[:NOTICE_LIMIT]),
                ("severity", notice.severity),
            )
        ]
        separator = "&" if "?" in location else "?"
        location += separator + urlencode(pairs)
    return RedirectResponse(location, status_code=303)


def _folder_from(request: Request) -> FolderRef:
    return FolderRef(
        account_name=request.query_params.get("account", ""),
        folder_name=request.query_params.get("folder", ""),
    )


def _optional_ref(request: Request) -> MessageRef | None:
    identifier = request.query_params.get("id", "")
    if not identifier.isdigit():
        return None
    return MessageRef(
        account_name=request.query_params.get("account", ""),
        folder_name=request.query_params.get("folder", ""),
        id=int(identifier),
    )


def _message_from(
    services: WebServices,
    request: Request,
) -> IndexedMessage | None:
    ref = _optional_ref(request)
    if ref is None:
        return None
    return services.index.get_message(message_ref=ref)


def _raw_from(services: WebServices, request: Request) -> bytes | Response:
    """The open message's bytes, or the response to send instead."""
    message = _message_from(services, request)
    if message is None:
        return PlainTextResponse("No such message.", status_code=404)
    raw = mail_view.raw_bytes(services, message)
    if raw is None:
        return PlainTextResponse("Message bytes are missing.", status_code=410)
    return raw


def _first_folder(services: WebServices) -> FolderRef | None:
    return next(iter(mail_view.all_folders(services)), None)


__all__ = [
    "Shutdown",
    "CLIENT_ACTIONS",
    "MESSAGE_HTML_CSP",
    "NOTICE_COUNT_LIMIT",
    "NOTICE_LIMIT",
    "SERVER_ACTIONS",
    "STATIC_DIR",
    "TOKEN_COOKIE",
    "WebServices",
    "build_web_app",
    "is_available",
]
