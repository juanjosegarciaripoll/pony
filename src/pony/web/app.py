"""The browser front end: routes over the same actions the TUI calls.

This is a second front end, not a second program.  Every operation it
offers is a call into :mod:`pony.mailbox_actions`, :mod:`pony.delivery`,
:mod:`pony.contact_actions`, :mod:`pony.invitation_actions` or
:mod:`chronos.event_actions`; every row it renders comes from
:mod:`pony.views` or :mod:`chronos.views`; every key it answers to comes
from :mod:`pony.keymap` or :mod:`chronos.keymap`.  What is new here is
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

from chronos.keymap import web_key
from chronos.outcome import Notice, Outcome, Severity

from .. import mailbox_actions
from ..domain import FolderRef, MessageFlag, MessageRef
from ..message_renderer import build_browser_html, extract_attachment
from . import calendar as calendar_view
from . import compose as compose_view
from . import contacts as contacts_view
from . import mail as mail_view
from . import urls
from .actions import ActionContext
from .layout import PageSurface, bindings_for
from .syncing import SyncGate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from starlette.requests import Request

    from ..calendar import CalendarRuntime
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
        Route("/folders", _handler(services, _folders_fragment)),
        Route(urls.MESSAGE, _handler(services, _message_page)),
        Route(urls.BODY, _handler(services, _message_body)),
        Route(urls.RAW, _handler(services, _message_raw)),
        Route(urls.ATTACHMENT, _handler(services, _attachment)),
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
    folder = _folder_from(request)
    return HTMLResponse(
        mail_view.mail_page(
            services,
            folder=folder,
            query=request.query_params.get("q", ""),
            notices=_notices(request),
        ),
    )


async def _rows_fragment(services: WebServices, request: Request) -> Response:
    return HTMLResponse(
        mail_view.rows(
            services,
            _folder_from(request),
            query=request.query_params.get("q", ""),
        ),
    )


async def _folders_fragment(services: WebServices, request: Request) -> Response:
    return HTMLResponse(mail_view.folder_pane(services, _folder_from(request)))


async def _message_page(services: WebServices, request: Request) -> Response:
    """The list with the message open beside (or below) it."""
    message = _message_from(services, request)
    if message is None:
        return PlainTextResponse("No such message.", status_code=404)
    # Opening a message is what marks it read, as it is in the reader.
    mailbox_actions.add_flag(services.mailboxes, message, MessageFlag.SEEN)
    return HTMLResponse(
        mail_view.mail_page(
            services,
            folder=FolderRef(
                account_name=message.message_ref.account_name,
                folder_name=message.message_ref.folder_name,
            ),
            message=message,
            query=request.query_params.get("q", ""),
            notices=_notices(request),
        ),
    )


async def _message_body(services: WebServices, request: Request) -> Response:
    """The message's own HTML, as a page of its own.

    Served with everything switched off: no scripts, no plugins, no
    network. ``build_browser_html`` has already inlined the inline
    images as data URIs, so there is nothing legitimate left to fetch.
    It is a top-level page rather than a frame in the application,
    because mail is hostile input and the reader shows text.
    """
    raw = _raw_from(services, request)
    if isinstance(raw, Response):
        return raw
    return HTMLResponse(
        build_browser_html(raw),
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
            ),
            "Referrer-Policy": "no-referrer",
        },
    )


async def _message_raw(services: WebServices, request: Request) -> Response:
    """The message as it arrived, for saving it somewhere else."""
    raw = _raw_from(services, request)
    if isinstance(raw, Response):
        return raw
    return Response(
        raw,
        media_type="message/rfc822",
        headers={"Content-Disposition": 'attachment; filename="message.eml"'},
    )


async def _attachment(services: WebServices, request: Request) -> Response:
    """One attachment's bytes, by the index the reader lists it under."""
    raw = _raw_from(services, request)
    if isinstance(raw, Response):
        return raw
    index = request.query_params.get("n", "")
    if not index.isdigit():
        return PlainTextResponse("Which attachment?", status_code=400)
    payload = extract_attachment(raw, int(index))
    if payload is None:
        return PlainTextResponse("No such attachment.", status_code=404)
    return Response(
        payload.data,
        media_type=payload.content_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_quotable(payload.filename)}"'
            ),
            # An attachment is the most hostile byte string the program
            # handles, and nothing should sniff it into something
            # executable.
            "X-Content-Type-Options": "nosniff",
        },
    )


def _quotable(filename: str) -> str:
    """A filename safe to put inside a quoted header parameter."""
    return filename.replace('"', "").replace("\\", "").replace("\r", "")


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
    form = compose_view.form_from_fields(dict(context.fields))
    if context.first("save_draft"):
        outcome = compose_view.save_draft(services, form)
    else:
        outcome = await run_in_threadpool(compose_view.send, services, form)
    if outcome.failed:
        return HTMLResponse(
            compose_view.compose_page(services, form, notices=outcome.notices),
        )
    return _back_with(_after_compose(form), outcome)


def _after_compose(form: compose_view.Form) -> str:
    ref = form.source or form.forward_of
    if ref is None:
        return urls.MAIL
    return urls.mail_url(
        FolderRef(account_name=ref.account_name, folder_name=ref.folder_name),
    )


async def _stage_attachment(services: WebServices, request: Request) -> Response:
    """Keep an uploaded file until the message it belongs to is sent."""
    del services
    name = request.query_params.get("name", "attachment")
    data = await request.body()
    if not data:
        return JSONResponse({"error": "That file is empty."}, status_code=400)
    token, path = compose_view.stage_attachment(name, data)
    return JSONResponse({"token": token, "name": path.name, "size": len(data)})


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
        found = [c for c in contacts.list_all_contacts() if c.id == int(identifier)]
        if not found:
            return PlainTextResponse("No such contact.", status_code=404)
        contact = found[0]
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
    saved, outcome = contacts_view.store_contact(
        contacts,
        contact_id=context.integer("id"),
        edit=edit,
    )
    if saved is None:
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
        runtime = services.calendar
        found = (
            [] if runtime is None else list(runtime.index.list_components_by_uid(uid))
        )
        if not found:
            return PlainTextResponse("No such event.", status_code=404)
        form = calendar_view.EventForm.of(found[0])
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
            target=_first_calendar(services),
            day=viewed,
        )
    return HTMLResponse(
        calendar_view.event_form_page(services, form, notices=_notices(request)),
    )


async def _event_save(services: WebServices, request: Request) -> Response:
    context = ActionContext.of(services, await request.body())
    form = calendar_view.form_from_fields(dict(context.fields))
    outcome = calendar_view.save(services, form)
    if outcome.failed:
        return HTMLResponse(
            calendar_view.event_form_page(services, form, notices=outcome.notices),
        )
    return _back_with(urls.calendar_url(), outcome)


def _first_calendar(services: WebServices) -> str:
    from chronos.views import all_calendar_refs

    runtime = services.calendar
    if runtime is None:
        return ""
    refs = all_calendar_refs(runtime.config, runtime.mirror)
    if not refs:
        return ""
    return f"{refs[0].account_name}/{refs[0].calendar_name}"


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
    """The keymaps, by page surface, as the browser needs to match them.

    ``available`` is what keeps the help honest: a key the terminal has
    and the browser has not is listed and marked, rather than appearing
    to work.
    """
    del services, request
    return JSONResponse(
        {
            surface.value: [
                {
                    "key": web_key(binding.key),
                    "action": binding.action,
                    "description": binding.description,
                    "surface": binding.surface,
                    "show": binding.show,
                    "available": _is_available(binding.action),
                }
                for binding in bindings_for(surface)
            ]
            for surface in PageSurface
        },
    )


def _is_available(action: str) -> bool:
    if action in SERVER_ACTIONS or action in CLIENT_ACTIONS:
        return True
    # `select_span(3)` and friends carry their argument in the name.
    return action.partition("(")[0] in CLIENT_ACTIONS


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------


def _sync_mail(context: ActionContext) -> Outcome:
    return context.services.sync.mail()


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
    outcome = await run_in_threadpool(action, context)
    return _outcome_response(outcome)


def _outcome_response(outcome: Outcome, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {
            "changed": outcome.changed,
            "notices": [
                {"text": notice.text, "severity": notice.severity}
                for notice in outcome.notices
            ],
        },
        status_code=status_code,
    )


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
    """
    text = request.query_params.get("notice", "")[:NOTICE_LIMIT]
    if not text:
        return ()
    offered = request.query_params.get("severity", "")
    severity: Severity = (
        "warning"
        if offered == "warning"
        else "error"
        if offered == "error"
        else "information"
    )
    return (Notice(text, severity),)


def _back_with(location: str, outcome: Outcome) -> RedirectResponse:
    """Go to *location*, carrying the outcome's last word with us."""
    if outcome.notices:
        notice = outcome.notices[-1]
        separator = "&" if "?" in location else "?"
        location += separator + urls.query(
            notice=notice.text[:NOTICE_LIMIT],
            severity=notice.severity,
        ).removeprefix("?")
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
    for account in services.config.accounts:
        mirror = services.mirrors.get(account.name)
        if mirror is None:
            continue
        for ref in mirror.list_folders(account_name=account.name):
            return ref
    return None


__all__ = [
    "CLIENT_ACTIONS",
    "NOTICE_LIMIT",
    "SERVER_ACTIONS",
    "STATIC_DIR",
    "TOKEN_COOKIE",
    "WebServices",
    "build_web_app",
]
