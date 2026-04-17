"""SMTP sending for Pony Express.

Credentials (username / password) are reused from the account's IMAP
configuration.  The account must supply ``smtp_host`` and ``smtp_port``;
``smtp_ssl`` selects between implicit TLS (SMTP_SSL, port 465) and STARTTLS
(SMTP, port 587).
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .domain import AccountConfig


class SMTPError(RuntimeError):
    """Raised when a message cannot be sent via SMTP."""


def send_message(account: AccountConfig, msg: EmailMessage) -> None:
    """Send *msg* through the SMTP server configured in *account*.

    Uses implicit TLS when ``account.smtp_ssl`` is True (SMTP_SSL); otherwise
    connects in plaintext and upgrades with STARTTLS.

    Raises ``SMTPError`` on authentication failure, connection error, or any
    SMTP-level rejection.  Raises ``ValueError`` if the account has no
    password configured.
    """
    password = account.password
    if not password:
        raise ValueError(
            f"no password configured for account {account.name!r}"
        )

    try:
        if account.smtp_ssl:
            with smtplib.SMTP_SSL(account.smtp_host, account.smtp_port) as smtp:
                smtp.login(account.username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(account.smtp_host, account.smtp_port) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(account.username, password)
                smtp.send_message(msg)
    except smtplib.SMTPException as exc:
        raise SMTPError(str(exc)) from exc
