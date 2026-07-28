"""Building the draft that a reply, forward or new message starts from.

The composer UI should not have to know how a reply is assembled — which
identity sends it, how the subject is prefixed, which recipients survive
a reply-all, how it threads. It asks for a :class:`DraftSpec` and renders
it.

Each of those decisions had been inlined at its own entry point, so the
five ways of opening the composer repeated the same identity fallback
and the same Markdown-default lookup five times, and any rule added to
one was missing from the others. Threading was the most recent example:
it has to be identical for reply and reply-all, and there was no single
place to put it.

Nothing here does I/O or touches the terminal. Forwarding needs the
original message written somewhere as a file, and that stays with the
caller; :func:`forward_attachment_name` supplies the name so the choice
is at least made once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .compose_utils import (
    build_forward_body,
    build_reply_all_recipients,
    build_reply_body,
    forward_subject,
    new_compose_body,
    parse_draft_fields,
    reply_subject,
    reply_thread_headers,
)
from .message_renderer import safe_attachment_filename

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .domain import AnyAccount, AppConfig
    from .message_renderer import RenderedMessage


@dataclass(frozen=True, slots=True)
class DraftSpec:
    """Everything the composer needs to open, and nothing about how.

    ``owned_paths`` are files the composer is responsible for deleting
    when it closes — a forward's materialised copy of the original.
    They are listed separately from ``attachment_paths`` so that a file
    the user attached from their own disk is never removed.
    """

    account_name: str
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body: str = ""
    attachment_paths: tuple[Path, ...] = ()
    markdown_mode: bool = False
    # Threading (RFC 5322 s3.6.4); empty for a fresh compose or a
    # forward, which start their own thread.
    in_reply_to: str = ""
    references: str = ""
    owned_paths: tuple[Path, ...] = field(default=())


def preferred_account(
    accounts: Sequence[AnyAccount], *, prefer_name: str | None
) -> AnyAccount:
    """Pick the identity to send as, given a preference.

    *accounts* must be non-empty and already filtered to those that can
    send. The preference is the account the message being answered
    belongs to; it is a preference rather than a rule because that
    account may be a local one, which has no way to send.
    """
    if prefer_name is not None:
        for account in accounts:
            if account.name == prefer_name:
                return account
    return accounts[0]


def markdown_default(account: AnyAccount, config: AppConfig) -> bool:
    """Whether the composer opens in Markdown mode for *account*."""
    return account.markdown_compose or config.markdown_compose


def new_draft(
    *,
    accounts: Sequence[AnyAccount],
    config: AppConfig,
    prefer_name: str | None = None,
    to: str = "",
) -> DraftSpec:
    """A blank message, optionally pre-addressed to *to*."""
    account = preferred_account(accounts, prefer_name=prefer_name)
    return DraftSpec(
        account_name=account.name,
        to=to,
        body=new_compose_body(account.signature),
        markdown_mode=markdown_default(account, config),
    )


def reply_draft(
    *,
    accounts: Sequence[AnyAccount],
    config: AppConfig,
    rendered: RenderedMessage,
    prefer_name: str | None = None,
    reply_all: bool = False,
) -> DraftSpec:
    """A reply to *rendered*, threaded under it.

    ``reply_all`` widens the recipients to everyone the original reached,
    minus the sending identity. Everything else — subject prefix, quoted
    body, threading — is identical either way, which is the point of
    having one function rather than two.
    """
    account = preferred_account(accounts, prefer_name=prefer_name)
    if reply_all:
        to, cc = build_reply_all_recipients(
            rendered, self_address=account.email_address
        )
    else:
        to, cc = rendered.from_, ""
    in_reply_to, references = reply_thread_headers(rendered)
    return DraftSpec(
        account_name=account.name,
        to=to,
        cc=cc,
        subject=reply_subject(rendered.subject),
        body=build_reply_body(rendered, signature=account.signature),
        markdown_mode=markdown_default(account, config),
        in_reply_to=in_reply_to,
        references=references,
    )


def forward_attachment_name(subject: str) -> str:
    """The filename a forwarded copy of a message should carry.

    The recipient sees this name, so it is derived from the subject
    rather than left as whatever a temporary-file API produced.
    """
    return safe_attachment_filename(
        f"{subject.strip() or 'forwarded message'}.eml",
        fallback="forwarded message.eml",
    )


def forward_draft(
    *,
    accounts: Sequence[AnyAccount],
    config: AppConfig,
    rendered: RenderedMessage,
    attachment: Path,
    prefer_name: str | None = None,
) -> DraftSpec:
    """A forward of *rendered*, carrying *attachment* as the original.

    *attachment* is a file the caller has already written; the draft
    claims ownership of it so the composer deletes it on close. No
    threading headers: a forward starts its own thread.
    """
    account = preferred_account(accounts, prefer_name=prefer_name)
    return DraftSpec(
        account_name=account.name,
        subject=forward_subject(rendered.subject),
        body=build_forward_body(rendered, signature=account.signature),
        attachment_paths=(attachment,),
        owned_paths=(attachment,),
        markdown_mode=markdown_default(account, config),
    )


def resumed_draft(
    *,
    accounts: Sequence[AnyAccount],
    config: AppConfig,
    raw: bytes,
    prefer_name: str | None = None,
) -> DraftSpec:
    """A draft reopened from its stored bytes.

    Threading headers are carried through, so a reply saved half-written
    still lands in its thread when it is finished and sent.
    """
    account = preferred_account(accounts, prefer_name=prefer_name)
    fields = parse_draft_fields(raw)
    return DraftSpec(
        account_name=account.name,
        to=fields["to"],
        cc=fields["cc"],
        bcc=fields["bcc"],
        subject=fields["subject"],
        body=fields["body"],
        markdown_mode=markdown_default(account, config),
        in_reply_to=fields["in_reply_to"],
        references=fields["references"],
    )
