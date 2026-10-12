"""Writing and sending mail from the browser.

The form is the composer's fields and nothing else: what goes in them
for a reply, a forward or a resumed draft is decided by
:mod:`pony.composer`, exactly as it is for the terminal, and what
happens when Send is pressed is :func:`pony.delivery.deliver_message` —
one call that drafts, sends, files the Sent copy and harvests the
addresses.

Attachments arrive on a route of their own as raw bytes, because a
multipart form would mean a third dependency for the sake of one
request.  The browser reads the file it was given and posts it; the
server keeps it in a staging directory of its own making and the form
carries a token, so no path the page sends is ever opened.
"""

from __future__ import annotations

import atexit
import dataclasses
import functools
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from chronos.outcome import Outcome, warn

from ..compose_utils import build_email_message
from ..composer import (
    DraftSpec,
    forward_attachment_name,
    forward_draft,
    from_header,
    new_draft,
    reply_draft,
    resumed_draft,
)
from ..delivery import deliver_message, file_draft
from ..domain import FolderRef, MessageRef
from ..folder_utils import holds_drafts
from ..message_renderer import render_message, safe_attachment_filename
from . import urls
from .html import Raw, element, fragment, join
from .layout import PageSurface, Tab, page, pane

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from chronos.outcome import Notice

    from ..domain import AnyAccount, AppConfig, IndexedMessage
    from .app import WebServices

_TOKEN = re.compile(r"\A[0-9a-f]{16}\Z")


@functools.cache
def _private_root() -> Path:
    """The fallback staging root, made once per process."""
    return Path(tempfile.mkdtemp(prefix="pony-web-attachments-"))


#: Roots an ``atexit`` hook has already been registered for, so a suite
#: that builds a hundred applications registers one hook per directory
#: rather than a hundred.
_SWEPT: set[Path] = set()


def staging_dir(config: AppConfig) -> Path:
    """Where uploaded attachments wait until the message is sent.

    Under ``data_dir``, which is the directory ``paths`` promises
    everything Pony writes lives under and the one ``PONY_ROOT``
    relocates: a ``tempfile.mkdtemp`` sat outside both, was never
    removed by anything, and on a machine whose ``/tmp`` is a share of
    RAM every half-finished forward stayed there until the next reboot.

    Made by the server rather than named by a request — a token
    addresses a file inside it, so nothing the page sends is ever used
    as a path — and emptied when the process ends.
    """
    declared = config.data_dir
    # A config with no data directory is only reachable by building one
    # in code; a private directory of this process's own is still better
    # than nothing, and it has to be the *same* one every time or a
    # token staged by one request is unfindable by the next.
    root = _private_root() if declared is None else declared / "web-attachments"
    root.mkdir(parents=True, exist_ok=True)
    if root not in _SWEPT:
        _SWEPT.add(root)
        atexit.register(shutil.rmtree, root, ignore_errors=True)
    return root


def stage_attachment(config: AppConfig, name: str, data: bytes) -> tuple[str, Path]:
    """Keep *data* for a later send; return its token and path."""
    token = secrets.token_hex(8)
    folder = staging_dir(config) / token
    folder.mkdir(parents=True)
    path = folder / safe_attachment_filename(name)
    path.write_bytes(data)
    return token, path


def staged_path(config: AppConfig, token: str) -> Path | None:
    """The file *token* stands for, or None when there is no such file."""
    if not _TOKEN.match(token):
        return None
    folder = staging_dir(config) / token
    if not folder.is_dir():
        return None
    files = sorted(folder.iterdir())
    return files[0] if files else None


def release_staged(config: AppConfig, paths: Iterable[Path]) -> None:
    """Drop the staged files *paths* name, this attempt being over.

    Addressed by the directory each file sits in, which is the token's
    own, so an upload and the copy of a forwarded message are let go the
    same way.  Nothing let go of either before: four failed sends of one
    forward left four whole copies of it behind.
    """
    root = staging_dir(config)
    for path in paths:
        folder = path.parent
        if folder.parent == root and _TOKEN.match(folder.name):
            shutil.rmtree(folder, ignore_errors=True)


# ----------------------------------------------------------------------
# The form
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Form:
    """What the compose page shows, whether prefilled or resubmitted."""

    account_name: str
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body: str = ""
    markdown: bool = False
    in_reply_to: str = ""
    references: str = ""
    # The draft this message was opened from, retired when it is sent
    # or saved again.
    source: MessageRef | None = None
    # The message a forward carries, attached at send time.
    forward_of: MessageRef | None = None
    # The message being replied to, marked \Answered once this is sent.
    answering: MessageRef | None = None
    attachments: tuple[tuple[str, str], ...] = ()

    @staticmethod
    def of(
        spec: DraftSpec,
        *,
        source: MessageRef | None = None,
        forward_of: MessageRef | None = None,
        answering: MessageRef | None = None,
    ) -> Form:
        return Form(
            account_name=spec.account_name,
            to=spec.to,
            cc=spec.cc,
            bcc=spec.bcc,
            subject=spec.subject,
            body=spec.body,
            markdown=spec.markdown_mode,
            in_reply_to=spec.in_reply_to,
            references=spec.references,
            source=source,
            forward_of=forward_of,
            answering=answering,
        )


def build_form(
    services: WebServices,
    *,
    mode: str,
    ref: MessageRef | None,
    to: str = "",
) -> Form | Notice:
    """The form for *mode*, prefilled the way the composer prefills it."""
    sendable = [account for account in services.config.accounts if account.can_send]
    if not sendable:
        return warn("No account in this configuration can send mail.")
    prefer = None if ref is None else ref.account_name
    if mode == "new":
        return Form.of(
            new_draft(
                accounts=sendable,
                config=services.config,
                prefer_name=prefer,
                to=to,
            ),
        )

    if ref is None:
        return warn("That needs a message to work from.")
    message = services.index.get_message(message_ref=ref)
    if message is None:
        return warn("That message is no longer here.")
    from .mail import raw_bytes

    raw = raw_bytes(services, message)
    if raw is None:
        return warn("The message file is missing — sync to fetch it again.")

    match mode:
        case "reply" | "reply_all":
            return Form.of(
                reply_draft(
                    accounts=sendable,
                    config=services.config,
                    rendered=render_message(raw),
                    prefer_name=prefer,
                    reply_all=mode == "reply_all",
                ),
                answering=ref,
            )
        case "forward":
            rendered = render_message(raw)
            # The attachment itself is materialised at send time, from
            # the index, so the page never carries a path.
            return Form.of(
                forward_draft(
                    accounts=sendable,
                    config=services.config,
                    rendered=rendered,
                    attachment=Path(forward_attachment_name(rendered.subject)),
                    prefer_name=prefer,
                ),
                forward_of=ref,
            )
        case "draft":
            # Only a draft is editable as one.  The terminal gates the
            # key on the folder (`check_action`); a URL cannot be
            # gated, so the same question is asked here.
            if not holds_drafts(
                services.config,
                FolderRef(
                    account_name=ref.account_name,
                    folder_name=ref.folder_name,
                ),
            ):
                return warn("Only a message in Drafts can be edited as one.")
            return Form.of(
                resumed_draft(
                    accounts=sendable,
                    config=services.config,
                    raw=raw,
                    prefer_name=prefer,
                ),
                source=ref,
            )
    return warn(f"{mode!r} is not a way to compose a message.")


def compose_page(
    services: WebServices,
    form: Form,
    *,
    notices: Sequence[Notice] = (),
) -> str:
    """The compose form, as one pane filling the window."""
    sendable = [account for account in services.config.accounts if account.can_send]
    return page(
        title=f"{form.subject or 'New message'} — Pony Express",
        surface=PageSurface.COMPOSE,
        tab=Tab.MAIL,
        status="Writing a message",
        panes=pane(
            name="compose",
            heading=fragment(
                element("span", form.subject or "New message", class_="title"),
                element(
                    "span",
                    element(
                        "button",
                        "Send",
                        type="submit",
                        form="compose-form",
                        class_="action primary",
                    ),
                    element(
                        "button",
                        "Save draft",
                        type="submit",
                        form="compose-form",
                        name="save_draft",
                        value="1",
                        class_="action",
                    ),
                    element("a", "Discard", href=_back_to(form), class_="action"),
                    class_="actions",
                ),
            ),
            body=_form(form, sendable),
        ),
        data={"account": form.account_name},
        notices=notices,
    )


def _back_to(form: Form) -> str:
    ref = form.source or form.forward_of
    if ref is None:
        return urls.MAIL
    return urls.mail_url(
        FolderRef(account_name=ref.account_name, folder_name=ref.folder_name),
    )


def _form(form: Form, sendable: Sequence[AnyAccount]) -> Raw:
    return element(
        "form",
        join(
            (
                _hidden("in_reply_to", form.in_reply_to),
                _hidden("references", form.references),
                _hidden("source", _ref_value(form.source)),
                _hidden("forward_of", _ref_value(form.forward_of)),
                _hidden("answering", _ref_value(form.answering)),
                _field(
                    "From",
                    element(
                        "select",
                        join(
                            element(
                                "option",
                                f"{account.name} <{account.email_address}>",
                                value=account.name,
                                selected=account.name == form.account_name,
                            )
                            for account in sendable
                        ),
                        name="account",
                        id="account",
                    ),
                ),
                _text_field("To", "to", form.to, autofocus=not form.to),
                _text_field("Cc", "cc", form.cc),
                _text_field("Bcc", "bcc", form.bcc),
                _text_field(
                    "Subject",
                    "subject",
                    form.subject,
                    autofocus=bool(form.to) and not form.subject,
                ),
                _field(
                    "Body",
                    element(
                        "textarea",
                        form.body,
                        name="body",
                        id="body",
                        rows="20",
                        autofocus=bool(form.to) and bool(form.subject),
                    ),
                ),
                element(
                    "div",
                    element(
                        "label",
                        element(
                            "input",
                            type="checkbox",
                            name="markdown",
                            value="1",
                            checked=form.markdown,
                        ),
                        element("span", "Markdown"),
                        class_="checkbox",
                    ),
                    element(
                        "label",
                        element("span", "Attach"),
                        element(
                            "input",
                            type="file",
                            id="attach",
                            multiple=True,
                            aria_label="Attach files",
                        ),
                        class_="attach",
                    ),
                    class_="compose-options",
                ),
                element(
                    "ul",
                    join(
                        element(
                            "li",
                            _hidden("attachment", token),
                            element("span", name),
                            class_="staged",
                        )
                        for token, name in form.attachments
                    ),
                    id="staged",
                    class_="attachments",
                ),
            ),
        ),
        id="compose-form",
        method="post",
        action=urls.COMPOSE,
    )


def _field(label: str, control: Raw) -> Raw:
    return element(
        "div",
        element("label", label),
        control,
        class_="field",
    )


def _text_field(
    label: str,
    name: str,
    value: str,
    *,
    autofocus: bool = False,
) -> Raw:
    return _field(
        label,
        element(
            "input",
            type="text",
            name=name,
            id=name,
            value=value,
            autocomplete="off",
            autofocus=autofocus,
        ),
    )


def _hidden(name: str, value: str) -> Raw | None:
    if not value:
        return None
    return element("input", type="hidden", name=name, value=value)


def _ref_value(ref: MessageRef | None) -> str:
    if ref is None:
        return ""
    return f"{ref.account_name}\x1f{ref.folder_name}\x1f{ref.id}"


def parse_ref(value: str) -> MessageRef | None:
    """A message reference as a hidden field carries it."""
    parts = value.split("\x1f")
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    return MessageRef(
        account_name=parts[0],
        folder_name=parts[1],
        id=int(parts[2]),
    )


def form_from_fields(config: AppConfig, fields: Mapping[str, list[str]]) -> Form:
    """What the browser posted, as a :class:`Form`.

    *config* says where the staging directory is, which is how a token
    the page sends is turned back into a file.
    """

    def one(name: str) -> str:
        values = fields.get(name, [])
        return values[0] if values else ""

    tokens = [token for token in fields.get("attachment", []) if _TOKEN.match(token)]
    return Form(
        account_name=one("account"),
        to=one("to"),
        cc=one("cc"),
        bcc=one("bcc"),
        subject=one("subject"),
        body=one("body"),
        markdown=bool(one("markdown")),
        in_reply_to=one("in_reply_to"),
        references=one("references"),
        source=parse_ref(one("source")),
        forward_of=parse_ref(one("forward_of")),
        answering=parse_ref(one("answering")),
        attachments=tuple(
            (token, _staged_name(config, token))
            for token in tokens
            if staged_path(config, token)
        ),
    )


def _staged_name(config: AppConfig, token: str) -> str:
    path = staged_path(config, token)
    return path.name if path is not None else token


# ----------------------------------------------------------------------
# Sending
# ----------------------------------------------------------------------


def send(services: WebServices, form: Form) -> Outcome:
    """Send *form*, or say what stopped it.

    Blocks for as long as SMTP does — the caller runs it off the event
    loop — and reports exactly what the terminal reports, because it is
    the same call underneath.
    """
    if not form.to.strip():
        return Outcome.refused(warn("A message needs at least one recipient."))
    account = _account(services, form.account_name)
    if account is None:
        return Outcome.refused(warn("That account cannot send mail."))
    attachments, extra = _attachment_paths(services, form)
    if extra is not None:
        return Outcome.refused(extra)
    try:
        try:
            message = build_email_message(
                from_address=_from_address(services, account),
                to=form.to,
                cc=form.cc,
                bcc=form.bcc,
                subject=form.subject,
                body=form.body,
                attachment_paths=list(attachments.paths),
                markdown_mode=form.markdown,
                in_reply_to=form.in_reply_to,
                references=form.references,
            )
        except FileNotFoundError as missing:
            return Outcome.refused(warn(f"Attachment is gone: {missing}"))
        outcome = deliver_message(
            services.mailboxes,
            message,
            account,
            credentials=services.credentials,
            contacts=services.contacts,
            to=form.to,
            cc=form.cc,
            connect_timeout=services.config.smtp_connect_timeout_seconds,
            source_draft=_resolve(services, form.source),
            # Replying marks what was replied to, here as in the terminal.
            answering=_resolve(services, form.answering),
        )
    finally:
        _let_go(services, attachments, sent=False)
    if not outcome.blocked:
        _let_go(services, attachments, sent=True)
    return outcome


def save_draft(services: WebServices, form: Form) -> Outcome:
    """Put *form* in Drafts without sending it."""
    account = _account(services, form.account_name)
    if account is None:
        return Outcome.refused(warn("That account cannot send mail."))
    attachments, extra = _attachment_paths(services, form)
    if extra is not None:
        return Outcome.refused(extra)
    try:
        message = build_email_message(
            from_address=_from_address(services, account),
            to=form.to,
            cc=form.cc,
            bcc=form.bcc,
            subject=form.subject,
            body=form.body,
            attachment_paths=list(attachments.paths),
            markdown_mode=form.markdown,
            in_reply_to=form.in_reply_to,
            references=form.references,
        )
    finally:
        _let_go(services, attachments, sent=False)
    outcome = file_draft(
        services.mailboxes,
        message.as_bytes(),
        account,
        # The draft this form was resumed from is replaced, not joined.
        source_draft=_resolve(services, form.source),
    )
    # The bytes are in the mirror now, so the staged copies are spent.
    if not outcome.blocked:
        _let_go(services, attachments, sent=True)
    return outcome


def _let_go(services: WebServices, attachments: Attached, *, sent: bool) -> None:
    """Drop what this attempt was holding, and nothing it still needs."""
    if sent:
        release_staged(services.config, attachments.uploaded)
    elif attachments.forwarded is not None:
        release_staged(services.config, (attachments.forwarded,))


def _resolve(services: WebServices, ref: MessageRef | None) -> IndexedMessage | None:
    """The row *ref* names, or None — the form only carries the ref."""
    if ref is None:
        return None
    return services.index.get_message(message_ref=ref)


def _account(services: WebServices, name: str) -> AnyAccount | None:
    for account in services.config.accounts:
        if account.name == name and account.can_send:
            return account
    return None


def _from_address(services: WebServices, account: AnyAccount) -> str:
    return from_header(account, contacts=services.contacts)


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Attached:
    """The files a send carries, and which of them it is holding.

    The two kinds have different lifetimes.  An upload belongs to the
    form and has to survive a refused send, because the user will press
    Send again with the same tokens.  The copy of a forwarded message is
    written afresh on every attempt, so it is this attempt's and goes
    with it either way.
    """

    uploaded: tuple[Path, ...] = ()
    forwarded: Path | None = None

    @property
    def paths(self) -> tuple[Path, ...]:
        if self.forwarded is None:
            return self.uploaded
        return (*self.uploaded, self.forwarded)


def _attachment_paths(
    services: WebServices,
    form: Form,
) -> tuple[Attached, Notice | None]:
    """Every file this message carries: staged uploads, then a forward."""
    paths: list[Path] = []
    for token, _name in form.attachments:
        path = staged_path(services.config, token)
        if path is None:
            return Attached(), warn(
                "One of the attachments expired — attach it again.",
            )
        paths.append(path)
    forwarded: Path | None = None
    if form.forward_of is not None:
        forwarded = _forwarded_copy(services, form.forward_of)
        if forwarded is None:
            return Attached(), warn("The message being forwarded is no longer here.")
    return Attached(uploaded=tuple(paths), forwarded=forwarded), None


def _forwarded_copy(services: WebServices, ref: MessageRef) -> Path | None:
    """The forwarded message, written where a send can attach it."""
    from .mail import raw_bytes

    message: IndexedMessage | None = services.index.get_message(message_ref=ref)
    if message is None:
        return None
    raw = raw_bytes(services, message)
    if raw is None:
        return None
    _token, path = stage_attachment(
        services.config,
        forward_attachment_name(message.subject),
        raw,
    )
    return path


__all__ = [
    "Attached",
    "Form",
    "build_form",
    "compose_page",
    "form_from_fields",
    "parse_ref",
    "release_staged",
    "save_draft",
    "send",
    "stage_attachment",
    "staged_path",
    "staging_dir",
]
