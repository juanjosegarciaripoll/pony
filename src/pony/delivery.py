"""Filing a message that has been written or sent.

Sending a message is three things, and only one of them is SMTP.  Before
the conversation starts the bytes go into Drafts, so a connection failure
leaves the message somewhere rather than nowhere; after it succeeds that
pre-send draft is retired, a copy is filed in Sent for the next sync to
append, and the addresses it went to are worth remembering.

Only the SMTP call needs to be off the event loop, and only the progress
toasts belong to a screen.  Everything else is bookkeeping over the
stores, and it lives here: the composer, a future web composer and the
invitation reply that :mod:`pony.invitation` produces all need the same
filing done the same way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chronos.outcome import Notice, Outcome, failure, info, warn

from .domain import FolderRef, IndexedMessage, MessageFlag, MessageRef
from .folder_utils import holds_drafts, resolve_folder
from .mailbox_actions import Mailboxes, add_flag, retire_message
from .message_projection import project_rfc822_message
from .smtp_sender import DEFAULT_CONNECT_ATTEMPTS, SMTPError
from .smtp_sender import send_message as smtp_send

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from email.message import EmailMessage

    from .domain import AnyAccount
    from .protocols import ContactRepository, CredentialsProvider

_log = logging.getLogger(__name__)

#: What both front ends say about a message with nowhere to go.  The
#: rule belongs with sending rather than with either form: the composer
#: said "'To' field is required." and the browser "A message needs at
#: least one recipient.", and `deliver_message` refuses it either way
#: so a third front end cannot forget to ask.
NEEDS_A_RECIPIENT = "A message needs at least one recipient."


@dataclass(frozen=True, slots=True)
class FiledMessage:
    """Where a message landed, and what to say if it did not.

    ``message`` is the inserted index row, with its assigned id, or
    ``None`` when the folder could not be found or the write failed.
    """

    message: IndexedMessage | None
    outcome: Outcome


def file_message(
    mailboxes: Mailboxes,
    raw: bytes,
    account: AnyAccount,
    *,
    folder_hint: str,
    override: str | None,
) -> FiledMessage:
    """Store *raw* in the account's *folder_hint* folder and index it.

    *override* is the folder named in the account's configuration, used
    in preference to looking one up; without it the hint is resolved
    through :func:`pony.folder_utils.resolve_folder`, which is what
    finds a server's localised ``Sent`` or ``Drafts``.

    The inserted row has no UID, which is what makes sync append it to
    the server on its next run.
    """
    mirror = mailboxes.mirror(account.name)
    if mirror is None:
        _log.warning(
            "No mirror for account %r — cannot file in %s",
            account.name,
            folder_hint,
        )
        # Said, not only logged: a front end that reports nothing here
        # leaves the user believing the copy was filed.
        return FiledMessage(
            None,
            Outcome.refused(
                warn(f"No mirror for {account.name}, so nothing was filed."),
            ),
        )

    folder_names = [
        ref.folder_name for ref in mirror.list_folders(account_name=account.name)
    ]
    folder_name = override or resolve_folder(folder_names, folder_hint)
    if folder_name is None:
        return FiledMessage(
            None,
            Outcome.refused(
                warn(f"Could not find '{folder_hint}' folder for {account.name}"),
            ),
        )

    folder = FolderRef(account_name=account.name, folder_name=folder_name)
    try:
        storage_key = mirror.store_message(folder=folder, raw_message=raw)
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "Failed to store message in %s/%s: %s",
            account.name,
            folder_name,
            exc,
        )
        return FiledMessage(
            None,
            Outcome.refused(warn(f"Could not save to {folder_hint}: {exc}")),
        )

    projected = project_rfc822_message(
        message_ref=MessageRef(
            account_name=account.name,
            folder_name=folder_name,
            id=0,
        ),
        raw_message=raw,
        storage_key=storage_key,
    )
    stored = mailboxes.index.insert_message(message=projected)
    return FiledMessage(stored, Outcome(changed=1))


def _retire_source_draft(
    mailboxes: Mailboxes,
    source_draft: IndexedMessage,
) -> Notice | None:
    """Retire the draft a message was resumed from, or say why not.

    Retiring is a deletion: the index row goes ``TRASHED`` and, for a
    message the server already knows, the next sync expunges it there
    too.  Whether that is the right thing to do is a question about the
    message, so the folder *it* is in answers it rather than whatever
    folder a front end happened to have open — a screen that asked the
    open folder instead made ``e`` live on a received message in a
    search result and then destroyed it.

    Returns ``None`` when the draft was retired, and the notice to show
    when it was left alone.  Refusing rather than raising is deliberate:
    by the time :func:`deliver_message` reaches here the message has
    already been sent, so there is nothing left to abort.
    """
    folder = FolderRef(
        account_name=source_draft.message_ref.account_name,
        folder_name=source_draft.message_ref.folder_name,
    )
    if not holds_drafts(mailboxes.config, folder):
        _log.error(
            "Refusing to retire %s/%s id=%s: not a Drafts folder",
            folder.account_name,
            folder.folder_name,
            source_draft.message_ref.id,
        )
        return warn(
            f"{folder.folder_name} is not a Drafts folder,"
            " so the message in it was left alone.",
        )
    # A notice when the mirror file outlived the row, which is the one
    # thing retiring can half-do; `None` when it went cleanly.
    return retire_message(
        mailboxes,
        source_draft,
        folder,
        # A draft that reached the server has to go there too, or the
        # next sync brings the old version back.
        expunge_on_server=source_draft.uid is not None,
    )


def file_draft(
    mailboxes: Mailboxes,
    raw: bytes,
    account: AnyAccount,
    *,
    source_draft: IndexedMessage | None = None,
) -> Outcome:
    """Put *raw* in Drafts, retiring the draft it was resumed from.

    Saving an edited draft is a replacement, not an addition: without
    retiring *source_draft* the folder ends up holding both the old
    version and the new one, which is what the browser used to do and
    the terminal did not.

    A *source_draft* that is not in a Drafts folder is not a draft at
    all, and retiring it would destroy received mail.  The new copy is
    still saved — the user's typing is theirs — and the original is left
    where it is, with a notice saying so.
    """
    filed = file_message(
        mailboxes,
        raw,
        account,
        folder_hint="Drafts",
        override=account.drafts_folder,
    )
    if filed.message is None:
        return filed.outcome
    refused = (
        None if source_draft is None else _retire_source_draft(mailboxes, source_draft)
    )
    extra = () if refused is None else (refused,)
    return Outcome(
        changed=filed.outcome.changed,
        notices=(*filed.outcome.notices, info("Saved in Drafts."), *extra),
    )


def record_sent(
    mailboxes: Mailboxes,
    raw: bytes,
    account: AnyAccount,
    *,
    draft: IndexedMessage | None,
    contacts: ContactRepository | None,
    recipients: Sequence[str],
) -> Outcome:
    """Tidy up after a successful send.

    The pre-send draft goes — it was only there in case the send failed —
    then the sent copy is filed, then the addresses are harvested.  In
    that order: a Sent copy that exists alongside a Drafts copy of the
    same message is the one state worth avoiding, so the draft is retired
    first.

    Harvesting is best-effort and reported by count, not per address.
    """
    from .contact_actions import harvest_addresses

    notices: list[Notice] = []
    if draft is not None:
        left_behind = retire_message(
            mailboxes,
            draft,
            FolderRef(
                account_name=draft.message_ref.account_name,
                folder_name=draft.message_ref.folder_name,
            ),
            # The pre-send draft was never on a server: it was written
            # moments ago for exactly this eventuality.
            expunge_on_server=False,
        )
        if left_behind is not None:
            notices.append(left_behind)
    filed = file_message(
        mailboxes,
        raw,
        account,
        folder_hint="Sent",
        override=account.sent_folder,
    )
    harvested = harvest_addresses(contacts, *recipients)
    return Outcome(
        changed=filed.outcome.changed + harvested.changed,
        # The draft's own notice first, because it is about the message
        # the user was looking at a moment ago.
        notices=(*notices, *filed.outcome.notices),
    )


def deliver_message(
    mailboxes: Mailboxes,
    msg: EmailMessage,
    account: AnyAccount,
    *,
    credentials: CredentialsProvider | None,
    contacts: ContactRepository | None,
    to: str,
    cc: str = "",
    connect_timeout: float,
    connect_attempts: int = DEFAULT_CONNECT_ATTEMPTS,
    on_attempt: Callable[[int, int], None] | None = None,
    source_draft: IndexedMessage | None = None,
    answering: IndexedMessage | None = None,
) -> Outcome:
    """Send *msg* and do the bookkeeping either outcome calls for.

    The whole sequence, in the order that survives a failure: resolve
    the password, put the bytes in Drafts, talk to the server, and only
    then retire the draft, flag what was answered, file the Sent copy
    and harvest the addresses.  *source_draft* is the draft the message
    was composed *from*, which a successful send also retires — the
    pre-send copy this function files is a different row, and one that
    is not in a Drafts folder is not retired at all (see
    :func:`_retire_source_draft`).  *answering*
    is the message being replied to, which a successful send marks
    answered; doing it here is what keeps the mark from depending on
    which front end sent the reply.

    This call blocks for as long as SMTP does, which is why both front
    ends run it off their own loop: the terminal hands it to a worker
    thread, the browser to the thread pool.  *on_attempt* is called with
    ``(attempt, total)`` before each connection attempt so a UI can say
    which try it is waiting on.
    """
    if not to.strip():
        # The rule, not the form's: the composer said "'To' field is
        # required." and the browser said "A message needs at least one
        # recipient.", which is one rule in two wordings -- and nothing
        # stopped a third front end from forgetting it entirely.
        return Outcome.refused(warn(NEEDS_A_RECIPIENT))
    if account.smtp is None or account.username is None:
        return Outcome.refused(
            failure(f"Account {account.name!r} has no SMTP server configured."),
        )
    password = resolve_send_password(credentials, account)
    if isinstance(password, Notice):
        return Outcome.refused(password)

    raw = msg.as_bytes()
    host = account.smtp.host
    # Saved before the conversation starts, so a connection failure
    # leaves the message somewhere rather than nowhere.  Filing it is
    # best-effort: a Drafts folder that cannot be found must not stop a
    # send, so its notices are dropped and only its absence is reported
    # (in the failure message below).
    draft = file_message(
        mailboxes,
        raw,
        account,
        folder_hint="Drafts",
        override=account.drafts_folder,
    ).message

    try:
        smtp_send(
            smtp=account.smtp,
            username=account.username,
            password=password,
            msg=msg,
            connect_timeout=connect_timeout,
            connect_attempts=connect_attempts,
            on_attempt=on_attempt,
        )
    except (SMTPError, ValueError) as exc:
        _log.error("SMTP send to %s failed: %s", host, exc)
        suffix = (
            " The message is in Drafts."
            if draft is not None
            else " The message was not saved."
        )
        return Outcome.refused(failure(f"Send failed: {exc}{suffix}"))

    filed = record_sent(
        mailboxes,
        raw,
        account,
        draft=draft,
        contacts=contacts,
        recipients=(to, cc),
    )
    if answering is not None:
        add_flag(mailboxes, answering, MessageFlag.ANSWERED)
    refused = (
        None if source_draft is None else _retire_source_draft(mailboxes, source_draft)
    )
    extra = () if refused is None else (refused,)
    return Outcome(
        changed=filed.changed + 1,
        notices=(
            *filed.notices,
            info(f"Message sent to {to} via {host}."),
            *extra,
        ),
    )


def resolve_send_password(
    credentials: CredentialsProvider | None,
    account: AnyAccount,
) -> str | Notice:
    """The password to send *account*'s mail with, or why there is none.

    Sending goes through the same credentials provider as syncing, so the
    ``env``, ``command`` and ``encrypted`` backends work here too.
    Reading ``account.password`` directly — as the composer used to —
    meant every account not using the plaintext backend appeared in the
    From dropdown and was then refused at send time.
    """
    if credentials is not None:
        try:
            password = credentials.get_password(account_name=account.name)
        except Exception as exc:  # noqa: BLE001
            _log.error("credential lookup failed for %r: %s", account.name, exc)
            return Notice(f"Could not get password: {exc}", "error")
        if password:
            return password
    # No provider wired in (a bare composer in a test, or an embedding
    # that never built one): fall back to the config field.
    elif account.password:
        return account.password
    return Notice(
        f"No password available for account {account.name!r}.",
        "error",
    )


__all__ = [
    "NEEDS_A_RECIPIENT",
    "FiledMessage",
    "deliver_message",
    "file_message",
    "record_sent",
    "resolve_send_password",
]
