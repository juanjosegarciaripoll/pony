"""SMTP sending for Pony Express.

The sender takes an explicit :class:`SmtpConfig` plus ``username`` and
``password``, so the same function serves both :class:`AccountConfig`
(where these are account-level fields shared with IMAP) and
:class:`LocalAccountConfig` (where the SMTP block and credentials are
optional extras enabling a local account to send).
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .domain import SmtpConfig


class SMTPError(RuntimeError):
    """Raised when a message cannot be sent via SMTP."""


def send_message(
    *,
    smtp: SmtpConfig,
    username: str,
    password: str,
    msg: EmailMessage,
) -> None:
    """Send *msg* through the given SMTP server using the given credentials.

    Uses implicit TLS when ``smtp.ssl`` is True (SMTP_SSL); otherwise
    connects in plaintext and upgrades with STARTTLS.

    Raises :class:`SMTPError` on authentication failure, connection
    error, or any SMTP-level rejection.  Raises :class:`ValueError` when
    *password* is empty (callers must resolve credentials beforehand).
    """
    if not password:
        raise ValueError("cannot send: empty password")

    try:
        if smtp.ssl:
            with smtplib.SMTP_SSL(smtp.host, smtp.port) as server:
                server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp.host, smtp.port) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(username, password)
                server.send_message(msg)
    except smtplib.SMTPException as exc:
        raise SMTPError(str(exc)) from exc
    except OSError as exc:
        raise SMTPError(str(exc)) from exc
