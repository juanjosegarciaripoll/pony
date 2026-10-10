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

import dataclasses
import functools
import re
import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from chronos.outcome import Outcome, warn

from ..compose_utils import build_email_message
from ..composer import (
    DraftSpec,
    forward_attachment_name,
    forward_draft,
    new_draft,
    reply_draft,
    resumed_draft,
)
from ..delivery import deliver_message, file_message
from ..domain import FolderRef, MessageRef
from ..message_renderer import render_message, safe_attachment_filename
from . import urls
from .html import Raw, element, fragment, join
from .layout import PageSurface, Tab, page, pane

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from chronos.outcome import Notice

    from ..domain import AnyAccount, IndexedMessage
    from .app import WebServices

_TOKEN = re.compile(r"\A[0-9a-f]{16}\Z")


@functools.cache
def staging_dir() -> Path:
    """Where uploaded attachments wait until the message is sent.

    One directory per server process, made by the server rather than
    named by a request: a token addresses a file inside it, so nothing
    the page sends is ever used as a path.
    """
    return Path(tempfile.mkdtemp(prefix="pony-web-attachments-"))


def stage_attachment(name: str, data: bytes) -> tuple[str, Path]:
    """Keep *data* for a later send; return its token and path."""
    token = secrets.token_hex(8)
    folder = staging_dir() / token
    folder.mkdir(parents=True)
    path = folder / safe_attachment_filename(name)
    path.write_bytes(data)
    return token, path


def staged_path(token: str) -> Path | None:
    """The file *token* stands for, or None when there is no such file."""
    if not _TOKEN.match(token):
        return None
    folder = staging_dir() / token
    if not folder.is_dir():
        return None
    files = sorted(folder.iterdir())
    return files[0] if files else None


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
    # The draft this message was opened from, retired when it is sent.
    source: MessageRef | None = None
    # The message a forward carries, attached at send time.
    forward_of: MessageRef | None = None
    attachments: tuple[tuple[str, str], ...] = ()

    @staticmethod
    def of(
        spec: DraftSpec,
        *,
        source: MessageRef | None = None,
        forward_of: MessageRef | None = None,
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


def form_from_fields(fields: Mapping[str, list[str]]) -> Form:
    """What the browser posted, as a :class:`Form`."""

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
        attachments=tuple(
            (token, _staged_name(token)) for token in tokens if staged_path(token)
        ),
    )


def _staged_name(token: str) -> str:
    path = staged_path(token)
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
        message = build_email_message(
            from_address=_from_address(account),
            to=form.to,
            cc=form.cc,
            bcc=form.bcc,
            subject=form.subject,
            body=form.body,
            attachment_paths=list(attachments),
            markdown_mode=form.markdown,
            in_reply_to=form.in_reply_to,
            references=form.references,
        )
    except FileNotFoundError as missing:
        return Outcome.refused(warn(f"Attachment is gone: {missing}"))
    source = (
        None
        if form.source is None
        else services.index.get_message(
            message_ref=form.source,
        )
    )
    return deliver_message(
        services.mailboxes,
        message,
        account,
        credentials=services.credentials,
        contacts=services.contacts,
        to=form.to,
        cc=form.cc,
        connect_timeout=services.config.smtp_connect_timeout_seconds,
        source_draft=source,
    )


def save_draft(services: WebServices, form: Form) -> Outcome:
    """Put *form* in Drafts without sending it."""
    account = _account(services, form.account_name)
    if account is None:
        return Outcome.refused(warn("That account cannot send mail."))
    attachments, extra = _attachment_paths(services, form)
    if extra is not None:
        return Outcome.refused(extra)
    message = build_email_message(
        from_address=_from_address(account),
        to=form.to,
        cc=form.cc,
        bcc=form.bcc,
        subject=form.subject,
        body=form.body,
        attachment_paths=list(attachments),
        markdown_mode=form.markdown,
        in_reply_to=form.in_reply_to,
        references=form.references,
    )
    filed = file_message(
        services.mailboxes,
        message.as_bytes(),
        account,
        folder_hint="Drafts",
        override=account.drafts_folder,
    )
    if filed.message is None:
        return filed.outcome
    return Outcome(
        changed=filed.outcome.changed,
        notices=(*filed.outcome.notices, warn("Saved in Drafts.")),
    )


def _account(services: WebServices, name: str) -> AnyAccount | None:
    for account in services.config.accounts:
        if account.name == name and account.can_send:
            return account
    return None


def _from_address(account: AnyAccount) -> str:
    return account.email_address or (account.username or account.name)


def _attachment_paths(
    services: WebServices,
    form: Form,
) -> tuple[tuple[Path, ...], Notice | None]:
    """Every file this message carries: staged uploads, then a forward."""
    paths: list[Path] = []
    for token, _name in form.attachments:
        path = staged_path(token)
        if path is None:
            return (), warn("One of the attachments expired — attach it again.")
        paths.append(path)
    if form.forward_of is not None:
        original = _forwarded_copy(services, form.forward_of)
        if original is None:
            return (), warn("The message being forwarded is no longer here.")
        paths.append(original)
    return tuple(paths), None


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
        forward_attachment_name(message.subject),
        raw,
    )
    return path


__all__ = [
    "Form",
    "build_form",
    "compose_page",
    "form_from_fields",
    "parse_ref",
    "save_draft",
    "send",
    "stage_attachment",
    "staged_path",
    "staging_dir",
]
