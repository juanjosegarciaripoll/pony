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

from chronos.outcome import Notice, Outcome, warn

from .domain import FolderRef, IndexedMessage, MessageRef
from .folder_utils import find_folder
from .mailbox_actions import Mailboxes, retire_message
from .message_projection import project_rfc822_message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .domain import AnyAccount
    from .protocols import ContactRepository, CredentialsProvider

_log = logging.getLogger(__name__)


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
    in preference to looking one up; without it the hint is matched
    against the folders the mirror actually has, which is what finds a
    server's localised ``Sent`` or ``Drafts``.

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
        return FiledMessage(None, Outcome())

    folder_names = [
        ref.folder_name for ref in mirror.list_folders(account_name=account.name)
    ]
    folder_name = override or find_folder(folder_names, folder_hint)
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

    if draft is not None:
        retire_message(
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
        notices=filed.outcome.notices,
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
    "FiledMessage",
    "file_message",
    "record_sent",
    "resolve_send_password",
]
