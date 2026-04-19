"""Tests for pony.tui.compose_utils."""

from __future__ import annotations

import dataclasses
import smtplib
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

from pony.domain import AccountConfig, MirrorConfig, SmtpConfig
from pony.smtp_sender import SMTPError, send_message
from pony.tui.compose_utils import (
    build_email_message,
    build_forward_body,
    build_reply_body,
    forward_subject,
    reply_subject,
)
from pony.tui.message_renderer import RenderedMessage


def _rendered(**kwargs: str) -> RenderedMessage:
    defaults = dict(
        subject="Hello",
        from_="Alice <alice@example.com>",
        to="Bob <bob@example.com>",
        cc="",
        date="Mon, 1 Jan 2024 12:00:00 +0000",
        body="Original body.",
        attachments=(),
        raw_bytes=b"",
    )
    defaults.update(kwargs)
    return RenderedMessage(**defaults)  # type: ignore[arg-type]


class ReplySubjectTest(unittest.TestCase):
    def test_adds_re_prefix(self) -> None:
        assert reply_subject("Hello") == "Re: Hello"

    def test_no_double_re(self) -> None:
        assert reply_subject("Re: Hello") == "Re: Hello"

    def test_case_insensitive_re(self) -> None:
        assert reply_subject("RE: Hello") == "RE: Hello"


class ForwardSubjectTest(unittest.TestCase):
    def test_adds_fwd_prefix(self) -> None:
        assert forward_subject("Hello") == "Fwd: Hello"

    def test_no_double_fwd(self) -> None:
        assert forward_subject("Fwd: Hello") == "Fwd: Hello"

    def test_fw_variant(self) -> None:
        assert forward_subject("FW: Hello") == "FW: Hello"

    def test_case_insensitive(self) -> None:
        assert forward_subject("fwd: Hello") == "fwd: Hello"


class BuildReplyBodyTest(unittest.TestCase):
    def test_starts_with_two_newlines(self) -> None:
        body = build_reply_body(_rendered(body="Hi there."))
        assert body.startswith("\n\n")

    def test_contains_attribution(self) -> None:
        r = _rendered(from_="Alice <alice@example.com>", date="2024-01-01")
        body = build_reply_body(r)
        assert "Alice <alice@example.com> wrote:" in body

    def test_lines_prefixed_with_quote_marker(self) -> None:
        r = _rendered(body="Line one\nLine two")
        body = build_reply_body(r)
        assert "> Line one" in body
        assert "> Line two" in body


class BuildForwardBodyTest(unittest.TestCase):
    def test_contains_separator(self) -> None:
        body = build_forward_body(_rendered())
        assert "Forwarded message" in body

    def test_contains_from_header(self) -> None:
        r = _rendered(from_="Alice <alice@example.com>")
        body = build_forward_body(r)
        assert "From: Alice <alice@example.com>" in body

    def test_contains_original_body(self) -> None:
        r = _rendered(body="The original content.")
        body = build_forward_body(r)
        assert "The original content." in body


class BuildEmailMessageTest(unittest.TestCase):
    def _build(self, **kwargs: object) -> EmailMessage:
        defaults: dict[str, object] = dict(
            from_address="alice@example.com",
            to="bob@example.com",
            cc="",
            bcc="",
            subject="Test",
            body="Hello.",
            attachment_paths=[],
        )
        defaults.update(kwargs)
        return build_email_message(**defaults)  # type: ignore[arg-type]

    def test_basic_headers(self) -> None:
        msg = self._build()
        assert msg["From"] == "alice@example.com"
        assert msg["To"] == "bob@example.com"
        assert msg["Subject"] == "Test"

    def test_cc_set_when_provided(self) -> None:
        msg = self._build(cc="carol@example.com")
        assert msg["Cc"] == "carol@example.com"

    def test_cc_omitted_when_empty(self) -> None:
        msg = self._build(cc="")
        assert msg["Cc"] is None

    def test_bcc_omitted_when_empty(self) -> None:
        msg = self._build(bcc="")
        assert msg["Bcc"] is None

    def test_message_id_present(self) -> None:
        msg = self._build()
        assert msg["Message-ID"]

    def test_body_content(self) -> None:
        msg = self._build(body="Custom body text.")
        assert isinstance(msg, EmailMessage)
        assert "Custom body text." in msg.get_body().get_content()  # type: ignore[union-attr]

    def test_attachment_added(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"attachment content")
            tmp = Path(f.name)
        try:
            msg = self._build(attachment_paths=[tmp])
            payloads = list(msg.iter_attachments())
            assert len(payloads) == 1
            assert payloads[0].get_filename() == tmp.name
        finally:
            tmp.unlink(missing_ok=True)

    def test_multiple_attachments(self) -> None:
        paths: list[Path] = []
        try:
            for i in range(3):
                with tempfile.NamedTemporaryFile(
                    suffix=f"_{i}.bin", delete=False
                ) as f:
                    f.write(b"x")
                    paths.append(Path(f.name))
            msg = self._build(attachment_paths=paths)
            assert len(list(msg.iter_attachments())) == 3
        finally:
            for p in paths:
                p.unlink(missing_ok=True)


def _make_account(
    *, smtp: SmtpConfig | None = None, **kwargs: object,
) -> AccountConfig:
    base = AccountConfig(
        name="test",
        email_address="alice@example.com",
        username="alice",
        credentials_source="plaintext",
        imap_host="imap.example.com",
        smtp=smtp or SmtpConfig(host="smtp.example.com"),
        mirror=MirrorConfig(path=Path("/tmp/mirror"), format="maildir"),
        password="secret",
    )
    return dataclasses.replace(base, **kwargs)  # type: ignore[arg-type]


def _mock_smtp() -> MagicMock:
    """Return a MagicMock that acts as its own context-manager value."""
    m = MagicMock()
    m.__enter__.return_value = m
    return m


class SmtpSenderTest(unittest.TestCase):
    """Unit tests for smtp_sender.send_message using a mock SMTP connection."""

    def test_raises_value_error_with_no_password(self) -> None:
        smtp = SmtpConfig(host="smtp.example.com")
        with self.assertRaises(ValueError):
            send_message(smtp=smtp, username="alice", password="", msg=EmailMessage())

    def test_ssl_path_uses_smtp_ssl(self) -> None:
        smtp = SmtpConfig(host="smtp.example.com", ssl=True)
        mock = _mock_smtp()
        with patch("smtplib.SMTP_SSL", return_value=mock) as smtp_ssl_cls:
            send_message(
                smtp=smtp, username="alice", password="secret",
                msg=EmailMessage(),
            )
            smtp_ssl_cls.assert_called_once_with("smtp.example.com", 465)
            mock.login.assert_called_once_with("alice", "secret")
            mock.send_message.assert_called_once()

    def test_starttls_path(self) -> None:
        smtp = SmtpConfig(host="smtp.example.com", ssl=False, port=587)
        mock = _mock_smtp()
        with patch("smtplib.SMTP", return_value=mock):
            send_message(
                smtp=smtp, username="alice", password="secret",
                msg=EmailMessage(),
            )
            mock.ehlo.assert_called()
            mock.starttls.assert_called_once()
            mock.login.assert_called_once_with("alice", "secret")

    def test_smtp_exception_wrapped_as_smtp_error(self) -> None:
        smtp = SmtpConfig(host="smtp.example.com", ssl=True)
        mock = _mock_smtp()
        mock.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad creds")
        with (
            patch("smtplib.SMTP_SSL", return_value=mock),
            self.assertRaises(SMTPError),
        ):
            send_message(
                smtp=smtp, username="alice", password="secret",
                msg=EmailMessage(),
            )


if __name__ == "__main__":
    unittest.main()
