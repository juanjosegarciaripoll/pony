"""SMTP sending for Pony Express.

The sender takes an explicit :class:`SmtpConfig` plus ``username`` and
``password``, so the same function serves both :class:`AccountConfig`
(where these are account-level fields shared with IMAP) and
:class:`LocalAccountConfig` (where the SMTP block and credentials are
optional extras enabling a local account to send).

Connecting is bounded and retried on the same terms as
:mod:`pony.imap_client`; see :data:`DEFAULT_CONNECT_TIMEOUT_SECONDS` and
:data:`DEFAULT_CONNECT_ATTEMPTS`.  Only the connect is retried — once the
session reaches ``DATA`` a retry risks delivering the message twice.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from collections.abc import Callable
from email.message import EmailMessage

from .domain import SmtpConfig

_log = logging.getLogger(__name__)

# Seconds to wait for the TCP handshake.  ``smtplib`` defaults the socket
# to blocking, so without an explicit value the kernel's SYN retry ceiling
# applies (~130 s on Linux) and a packet filter that silently drops SYNs
# freezes the send instead of failing it.  A handshake that is going to
# succeed does so in milliseconds, so a larger value buys nothing.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0

# How many times to attempt the connection before giving up.  Networks
# that spread flows over several paths pick the path by hashing the
# connection's addresses and ports, so a single blackholed path drops a
# reproducible fraction of connections while leaving the rest healthy.
# Each retry opens a new socket with a new ephemeral source port, which
# rehashes onto a possibly different path — an independent attempt rather
# than a repeat of the same one.
DEFAULT_CONNECT_ATTEMPTS = 4

# Failures worth another attempt with a fresh source port.  A refused
# connection is deliberately absent: something answered and said no, and
# that does not improve on a second try.
_TRANSIENT = (
    TimeoutError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    ssl.SSLEOFError,
    smtplib.SMTPConnectError,
    smtplib.SMTPServerDisconnected,
)


class SMTPError(RuntimeError):
    """Raised when a message cannot be sent via SMTP."""


def _describe(smtp: SmtpConfig) -> str:
    """Human-readable ``host:port`` label used in messages and logs."""
    return f"{smtp.host}:{smtp.port}"


def _smtp_detail(exc: smtplib.SMTPResponseException) -> str:
    """Render a server's refusal as ``code message``."""
    error = exc.smtp_error
    text = error.decode(errors="replace") if isinstance(error, bytes) else str(error)
    return f"{exc.smtp_code} {text.strip()}"


def _open(smtp: SmtpConfig, connect_timeout: float) -> smtplib.SMTP:
    """Open one transport-level connection, TLS established either way."""
    if smtp.ssl:
        return smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=connect_timeout)
    server = smtplib.SMTP(smtp.host, smtp.port, timeout=connect_timeout)
    try:
        server.ehlo()
        server.starttls()
        server.ehlo()
    except Exception:
        server.close()
        raise
    return server


def _connect(
    smtp: SmtpConfig,
    connect_timeout: float,
    connect_attempts: int,
    on_attempt: Callable[[int, int], None] | None,
) -> smtplib.SMTP:
    """Connect to *smtp*, retrying transient transport failures.

    Raises :class:`SMTPError` naming the host, the number of attempts and
    the timeout when every attempt fails, so the caller can report what
    was actually tried rather than a bare ``errno``.
    """
    delay = 0.5
    for attempt in range(1, connect_attempts + 1):
        if on_attempt is not None:
            on_attempt(attempt, connect_attempts)
        try:
            return _open(smtp, connect_timeout)
        except _TRANSIENT as exc:
            if attempt == connect_attempts:
                raise SMTPError(
                    f"could not reach {_describe(smtp)}: {connect_attempts} "
                    f"connection attempts all failed after {connect_timeout:.0f}s "
                    f"({type(exc).__name__}: {exc}). The server is unreachable "
                    f"from this network."
                ) from exc
            _log.info(
                "Connecting to %s failed (attempt %d/%d): %s — retrying",
                _describe(smtp),
                attempt,
                connect_attempts,
                exc,
            )
            time.sleep(delay)
            delay = min(delay * 2, 5.0)
    raise AssertionError("unreachable")  # pragma: no cover


def send_message(
    *,
    smtp: SmtpConfig,
    username: str,
    password: str,
    msg: EmailMessage,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    connect_attempts: int = DEFAULT_CONNECT_ATTEMPTS,
    on_attempt: Callable[[int, int], None] | None = None,
) -> None:
    """Send *msg* through the given SMTP server using the given credentials.

    Uses implicit TLS when ``smtp.ssl`` is True (SMTP_SSL); otherwise
    connects in plaintext and upgrades with STARTTLS.  The connection is
    attempted up to *connect_attempts* times, each bounded by
    *connect_timeout* seconds; *on_attempt* is called with
    ``(attempt, total)`` before each one so a UI can say which try it is
    waiting on.

    Raises :class:`SMTPError` on authentication failure, connection
    error, or any SMTP-level rejection — always with a message naming the
    host and what the server said.  Raises :class:`ValueError` when
    *password* is empty (callers must resolve credentials beforehand).
    """
    if not password:
        raise ValueError("cannot send: empty password")

    try:
        server = _connect(smtp, connect_timeout, connect_attempts, on_attempt)
    except SMTPError:
        raise
    except smtplib.SMTPException as exc:
        # A greeting or STARTTLS that the server refused outright.  Not
        # retried, and not an OSError, so it needs its own conversion to
        # keep the documented contract: everything out of here is SMTPError.
        raise SMTPError(
            f"could not open a session with {_describe(smtp)}: {exc}"
        ) from exc
    except OSError as exc:
        raise SMTPError(f"could not connect to {_describe(smtp)}: {exc}") from exc

    try:
        with server:
            try:
                server.login(username, password)
            except smtplib.SMTPAuthenticationError as exc:
                raise SMTPError(
                    f"{smtp.host} rejected the credentials for "
                    f"{username}: {_smtp_detail(exc)}"
                ) from exc
            except smtplib.SMTPNotSupportedError as exc:
                raise SMTPError(
                    f"{smtp.host} offers no authentication method pony can use: {exc}"
                ) from exc
            try:
                server.send_message(msg)
            except smtplib.SMTPRecipientsRefused as exc:
                refused = ", ".join(sorted(exc.recipients))
                raise SMTPError(
                    f"{smtp.host} refused every recipient ({refused})"
                ) from exc
            except smtplib.SMTPSenderRefused as exc:
                raise SMTPError(
                    f"{smtp.host} refused the sender address {exc.sender}: "
                    f"{_smtp_detail(exc)}"
                ) from exc
    except smtplib.SMTPResponseException as exc:
        raise SMTPError(
            f"{smtp.host} rejected the message: {_smtp_detail(exc)}"
        ) from exc
    except smtplib.SMTPException as exc:
        raise SMTPError(f"sending through {_describe(smtp)} failed: {exc}") from exc
    except OSError as exc:
        raise SMTPError(
            f"connection to {_describe(smtp)} broke while sending: {exc}"
        ) from exc
