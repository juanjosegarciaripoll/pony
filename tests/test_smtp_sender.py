"""Tests for ``pony.smtp_sender`` — connect bounding, retries, messages."""

from __future__ import annotations

import smtplib
import unittest
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from pony.domain import SmtpConfig
from pony.smtp_sender import (
    DEFAULT_CONNECT_ATTEMPTS,
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    SMTPError,
    send_message,
)

SSL_SMTP = SmtpConfig(host="smtp.example.com", port=465, ssl=True)
PLAIN_SMTP = SmtpConfig(host="smtp.example.com", port=587, ssl=False)


def _message() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "me@example.com"
    msg["To"] = "you@example.com"
    msg["Subject"] = "hi"
    msg.set_content("body")
    return msg


def _server() -> MagicMock:
    """A mock standing in for a connected ``smtplib.SMTP``."""
    server = MagicMock()
    server.__enter__.return_value = server
    server.__exit__.return_value = False
    return server


def _send(**kwargs: object) -> None:
    """Call ``send_message`` with the boilerplate arguments filled in."""
    params: dict[str, object] = {
        "smtp": SSL_SMTP,
        "username": "me@example.com",
        "password": "secret",
        "msg": _message(),
    }
    params.update(kwargs)
    send_message(**params)  # pyright: ignore[reportArgumentType]


class ConnectBoundingTest(unittest.TestCase):
    """The socket must never be left blocking."""

    def test_ssl_connect_passes_the_timeout(self) -> None:
        with patch("smtplib.SMTP_SSL", return_value=_server()) as ctor:
            _send()
        ctor.assert_called_once_with(
            "smtp.example.com", 465, timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS
        )

    def test_starttls_connect_passes_the_timeout(self) -> None:
        server = _server()
        with patch("smtplib.SMTP", return_value=server) as ctor:
            _send(smtp=PLAIN_SMTP)
        ctor.assert_called_once_with(
            "smtp.example.com", 587, timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS
        )
        server.starttls.assert_called_once_with()
        self.assertEqual(server.ehlo.call_count, 2)

    def test_explicit_timeout_is_honoured(self) -> None:
        with patch("smtplib.SMTP_SSL", return_value=_server()) as ctor:
            _send(connect_timeout=3.0)
        ctor.assert_called_once_with("smtp.example.com", 465, timeout=3.0)

    def test_failed_starttls_closes_the_socket(self) -> None:
        server = _server()
        server.starttls.side_effect = smtplib.SMTPNotSupportedError("no TLS")
        with (
            patch("smtplib.SMTP", return_value=server),
            self.assertRaises(SMTPError),
        ):
            _send(smtp=PLAIN_SMTP)
        server.close.assert_called_once_with()

    def test_a_refused_greeting_becomes_an_smtp_error(self) -> None:
        with (
            patch("smtplib.SMTP_SSL", side_effect=smtplib.SMTPException("go away")),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        self.assertIn("could not open a session", str(caught.exception))


class ConnectRetryTest(unittest.TestCase):
    """A dropped SYN gets another attempt on a fresh source port."""

    def test_retries_until_a_connection_succeeds(self) -> None:
        server = _server()
        attempts = [TimeoutError("timed out"), TimeoutError("timed out"), server]
        with (
            patch("smtplib.SMTP_SSL", side_effect=attempts) as ctor,
            patch("pony.smtp_sender.time.sleep"),
        ):
            _send()
        self.assertEqual(ctor.call_count, 3)
        server.send_message.assert_called_once()

    def test_gives_up_after_the_attempt_limit(self) -> None:
        with (
            patch("smtplib.SMTP_SSL", side_effect=TimeoutError("timed out")) as ctor,
            patch("pony.smtp_sender.time.sleep"),
            self.assertRaises(SMTPError) as caught,
        ):
            _send(connect_attempts=3)
        self.assertEqual(ctor.call_count, 3)
        message = str(caught.exception)
        self.assertIn("smtp.example.com:465", message)
        self.assertIn("3 connection attempts", message)
        self.assertIn("unreachable", message)

    def test_default_makes_more_than_one_attempt(self) -> None:
        self.assertGreater(DEFAULT_CONNECT_ATTEMPTS, 1)
        with (
            patch("smtplib.SMTP_SSL", side_effect=TimeoutError("timed out")) as ctor,
            patch("pony.smtp_sender.time.sleep"),
            self.assertRaises(SMTPError),
        ):
            _send()
        self.assertEqual(ctor.call_count, DEFAULT_CONNECT_ATTEMPTS)

    def test_a_refused_connection_is_not_retried(self) -> None:
        """Something answered and said no; another try will not change that."""
        with (
            patch(
                "smtplib.SMTP_SSL", side_effect=ConnectionRefusedError("refused")
            ) as ctor,
            patch("pony.smtp_sender.time.sleep"),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        self.assertEqual(ctor.call_count, 1)
        self.assertIn(
            "could not connect to smtp.example.com:465", str(caught.exception)
        )

    def test_progress_callback_reports_each_attempt(self) -> None:
        seen: list[tuple[int, int]] = []
        with (
            patch("smtplib.SMTP_SSL", side_effect=[TimeoutError("x"), _server()]),
            patch("pony.smtp_sender.time.sleep"),
        ):
            _send(on_attempt=lambda a, t: seen.append((a, t)), connect_attempts=4)
        self.assertEqual(seen, [(1, 4), (2, 4)])

    def test_a_send_that_works_first_time_does_not_sleep(self) -> None:
        with (
            patch("smtplib.SMTP_SSL", return_value=_server()),
            patch("pony.smtp_sender.time.sleep") as sleep,
        ):
            _send()
        sleep.assert_not_called()


class ErrorMessageTest(unittest.TestCase):
    """Failures name the host and repeat what the server said."""

    def test_empty_password_is_rejected_before_connecting(self) -> None:
        with patch("smtplib.SMTP_SSL") as ctor, self.assertRaises(ValueError):
            _send(password="")
        ctor.assert_not_called()

    def test_rejected_credentials_name_the_user_and_the_reason(self) -> None:
        server = _server()
        server.login.side_effect = smtplib.SMTPAuthenticationError(
            535, b"5.7.8 Bad password"
        )
        with (
            patch("smtplib.SMTP_SSL", return_value=server),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        message = str(caught.exception)
        self.assertIn("me@example.com", message)
        self.assertIn("535", message)
        self.assertIn("Bad password", message)

    def test_missing_auth_support_is_explained(self) -> None:
        server = _server()
        server.login.side_effect = smtplib.SMTPNotSupportedError("no AUTH here")
        with (
            patch("smtplib.SMTP_SSL", return_value=server),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        self.assertIn("no authentication method", str(caught.exception))

    def test_refused_recipients_are_listed(self) -> None:
        server = _server()
        server.send_message.side_effect = smtplib.SMTPRecipientsRefused(
            {"you@example.com": (550, b"No such user")}
        )
        with (
            patch("smtplib.SMTP_SSL", return_value=server),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        self.assertIn("you@example.com", str(caught.exception))

    def test_a_refused_sender_is_named(self) -> None:
        server = _server()
        server.send_message.side_effect = smtplib.SMTPSenderRefused(
            553, b"5.7.1 Sender denied", "me@example.com"
        )
        with (
            patch("smtplib.SMTP_SSL", return_value=server),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        message = str(caught.exception)
        self.assertIn("me@example.com", message)
        self.assertIn("Sender denied", message)

    def test_a_rejected_message_repeats_the_server_response(self) -> None:
        server = _server()
        server.send_message.side_effect = smtplib.SMTPDataError(
            552, b"5.3.4 Message too big"
        )
        with (
            patch("smtplib.SMTP_SSL", return_value=server),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        message = str(caught.exception)
        self.assertIn("552", message)
        self.assertIn("Message too big", message)

    def test_a_plain_smtp_error_names_the_host_and_port(self) -> None:
        server = _server()
        server.send_message.side_effect = smtplib.SMTPException("protocol confusion")
        with (
            patch("smtplib.SMTP_SSL", return_value=server),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        message = str(caught.exception)
        self.assertIn("smtp.example.com:465", message)
        self.assertIn("protocol confusion", message)

    def test_a_broken_connection_mid_send_is_distinguished(self) -> None:
        server = _server()
        server.send_message.side_effect = ConnectionResetError("peer went away")
        with (
            patch("smtplib.SMTP_SSL", return_value=server),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        self.assertIn("broke while sending", str(caught.exception))

    def test_a_string_smtp_error_is_rendered(self) -> None:
        """``smtp_error`` is bytes on the wire but str for some exceptions."""
        server = _server()
        server.send_message.side_effect = smtplib.SMTPDataError(452, "out of space")
        with (
            patch("smtplib.SMTP_SSL", return_value=server),
            self.assertRaises(SMTPError) as caught,
        ):
            _send()
        self.assertIn("out of space", str(caught.exception))


class SuccessTest(unittest.TestCase):
    """The happy path still logs in and delivers exactly once."""

    def test_login_and_send_happen_once_each(self) -> None:
        server = _server()
        msg = _message()
        with patch("smtplib.SMTP_SSL", return_value=server):
            _send(msg=msg)
        server.login.assert_called_once_with("me@example.com", "secret")
        server.send_message.assert_called_once_with(msg)


if __name__ == "__main__":
    unittest.main()
