"""Account lookup and mirror construction.

Every entry point — the CLI, the MCP server, the sync engine and the TUI
— needs the same two things before it can do any work: turn an account
name into an account, and turn an account into the mirror repository
that holds its mail.  Both had grown a copy per entry point, and the
copies disagreed: ``cli`` and ``mcp_server`` carried byte-identical
mirror factories under different names, and "the IMAP account called X"
was spelled three different ways.

These are deliberately pure: they resolve or they return ``None``.
Turning "not found" into an error message belongs to whichever front end
asked, because a CLI exits, the TUI notifies, and the MCP server returns
a fault.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .domain import AccountConfig
from .storage import MaildirMirrorRepository, MboxMirrorRepository

if TYPE_CHECKING:
    from .domain import AnyAccount, AppConfig
    from .protocols import MirrorRepository


def build_mirror(account: AnyAccount) -> MirrorRepository:
    """Return the mirror repository backing *account*."""
    if account.mirror.format == "maildir":
        return MaildirMirrorRepository(
            account_name=account.name, root_dir=account.mirror.path
        )
    return MboxMirrorRepository(account_name=account.name, root_dir=account.mirror.path)


def build_mirrors(config: AppConfig) -> dict[str, MirrorRepository]:
    """Return a mirror per configured account, keyed by account name."""
    return {account.name: build_mirror(account) for account in config.accounts}


def find_account(config: AppConfig, name: str) -> AnyAccount | None:
    """Return the account named *name* whatever its type, or ``None``.

    Use this for anything that only reads the local mirror — a local
    (mbox/Maildir) account is a perfectly good source of mail to read.
    """
    return next((a for a in config.accounts if a.name == name), None)


def find_imap_account(config: AppConfig, name: str) -> AccountConfig | None:
    """Return the IMAP account named *name*, or ``None``.

    Local accounts return ``None`` even when the name matches: they carry
    neither server connection details nor the folder-policy fields that
    sync and the move guards consult.
    """
    return next(
        (a for a in config.accounts if isinstance(a, AccountConfig) and a.name == name),
        None,
    )


def imap_accounts(config: AppConfig) -> list[AccountConfig]:
    """Return every IMAP account, in configuration order."""
    return [a for a in config.accounts if isinstance(a, AccountConfig)]


def select_imap_accounts(config: AppConfig, name: str | None) -> list[AccountConfig]:
    """Return the IMAP accounts *name* selects — all of them when ``None``.

    An unknown name yields an empty list rather than an error so that
    callers can distinguish "nothing to do" from "you asked for something
    that does not exist" in whatever way suits them.
    """
    accounts = imap_accounts(config)
    if name is None:
        return accounts
    return [a for a in accounts if a.name == name]
