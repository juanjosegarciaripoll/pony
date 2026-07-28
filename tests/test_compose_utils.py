"""Tests for pony.compose_utils."""

from __future__ import annotations

import dataclasses
import smtplib
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import corpus

from pony.compose_utils import (
    _add_blockquote_hardbreaks,
    _split_at_quote_boundary,
    build_email_message,
    build_forward_body,
    build_reply_all_recipients,
    build_reply_body,
    forward_subject,
    new_compose_body,
    parse_draft_fields,
    reply_subject,
    split_address_list,
    split_trailing_address,
)
from pony.domain import AccountConfig, MirrorConfig, SmtpConfig
from pony.message_renderer import RenderedMessage
from pony.smtp_sender import SMTPError, send_message


def _rendered(**kwargs: object) -> RenderedMessage:
    subject = str(kwargs.get("subject", "Hello"))
    from_ = str(kwargs.get("from_", "Alice <alice@example.com>"))
    to = str(kwargs.get("to", "Bob <bob@example.com>"))
    cc = str(kwargs.get("cc", ""))
    date = str(kwargs.get("date", "Mon, 1 Jan 2024 12:00:00 +0000"))
    body_text = str(kwargs.get("body", "Original body."))
    raw_bytes = kwargs.get("raw_bytes")

    if not raw_bytes:
        lines = [f"From: {from_}", f"To: {to}"]
        if cc:
            lines.append(f"Cc: {cc}")
        lines.extend([f"Subject: {subject}", f"Date: {date}", "", body_text])
        raw_bytes = "\r\n".join(lines).encode("utf-8")

    return RenderedMessage(
        subject=subject,
        from_=from_,
        to=to,
        cc=cc,
        date=date,
        body=body_text,
        attachments=(),
        raw_bytes=raw_bytes,  # type: ignore[arg-type]
    )


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


class BuildReplyAllRecipientsTest(unittest.TestCase):
    def test_to_is_original_sender(self) -> None:
        r = _rendered(from_="Alice <alice@example.com>", to="me@example.com")
        to, _cc = build_reply_all_recipients(r, self_address="me@example.com")
        assert to == "Alice <alice@example.com>"

    def test_cc_includes_original_to_minus_self(self) -> None:
        r = _rendered(
            from_="Alice <alice@example.com>",
            to="me@example.com, Carol <carol@example.com>",
            cc="",
        )
        _to, cc = build_reply_all_recipients(r, self_address="me@example.com")
        assert "carol@example.com" in cc
        assert "me@example.com" not in cc

    def test_cc_merges_to_and_cc(self) -> None:
        r = _rendered(
            from_="alice@example.com",
            to="bob@example.com",
            cc="Dan <dan@example.com>, eve@example.com",
        )
        _to, cc = build_reply_all_recipients(r, self_address="me@example.com")
        assert "bob@example.com" in cc
        assert "Dan <dan@example.com>" in cc
        assert "eve@example.com" in cc

    def test_cc_excludes_sender(self) -> None:
        r = _rendered(
            from_="Alice <alice@example.com>",
            to="alice@example.com, bob@example.com",
            cc="",
        )
        _to, cc = build_reply_all_recipients(r, self_address="me@example.com")
        assert "alice@example.com" not in cc
        assert "bob@example.com" in cc

    def test_self_address_match_is_case_insensitive(self) -> None:
        r = _rendered(
            from_="alice@example.com",
            to="Me <ME@Example.COM>, bob@example.com",
            cc="",
        )
        _to, cc = build_reply_all_recipients(r, self_address="me@example.com")
        assert "Example.COM" not in cc
        assert "bob@example.com" in cc

    def test_duplicates_are_removed(self) -> None:
        r = _rendered(
            from_="alice@example.com",
            to="bob@example.com",
            cc="bob@example.com, carol@example.com",
        )
        _to, cc = build_reply_all_recipients(r, self_address="me@example.com")
        assert cc.count("bob@example.com") == 1
        assert "carol@example.com" in cc

    def test_empty_cc_when_no_other_recipients(self) -> None:
        r = _rendered(
            from_="alice@example.com",
            to="me@example.com",
            cc="",
        )
        _to, cc = build_reply_all_recipients(r, self_address="me@example.com")
        assert cc == ""

    def test_cc_display_name_with_comma_not_split(self) -> None:
        # RFC 2047-encoded display names containing commas (e.g. "Last, First")
        # must not be split into multiple Cc entries.
        raw = (
            b"From: alice@example.com\r\n"
            b"To: me@example.com\r\n"
            b"Cc: =?utf-8?q?Smith=2C_John?= <john@example.com>\r\n"
            b"\r\n"
            b"Body\r\n"
        )
        r = _rendered(raw_bytes=raw, from_="alice@example.com", to="me@example.com")
        _to, cc = build_reply_all_recipients(r, self_address="me@example.com")
        assert cc.count("john@example.com") == 1


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
                with tempfile.NamedTemporaryFile(suffix=f"_{i}.bin", delete=False) as f:
                    f.write(b"x")
                    paths.append(Path(f.name))
            msg = self._build(attachment_paths=paths)
            assert len(list(msg.iter_attachments())) == 3
        finally:
            for p in paths:
                p.unlink(missing_ok=True)

    def test_eml_file_attachment_uses_message_rfc822_type(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as f:
            f.write(corpus.multipart_mixed_attachment())
            tmp = Path(f.name)
        try:
            msg = self._build(attachment_paths=[tmp])
        finally:
            tmp.unlink(missing_ok=True)

        attachments = list(msg.iter_attachments())
        assert len(attachments) == 1
        assert attachments[0].get_content_type() == "message/rfc822"
        assert attachments[0].get_filename() == tmp.name


class AddBlockquoteHardbreaksTest(unittest.TestCase):
    def test_adds_two_spaces_to_blockquote_lines(self) -> None:
        result = _add_blockquote_hardbreaks("> line1\n> line2\n> line3")
        assert result == "> line1  \n> line2  \n> line3  "

    def test_leaves_non_blockquote_lines_unchanged(self) -> None:
        result = _add_blockquote_hardbreaks("normal\n> quoted\nnormal again")
        assert result == "normal\n> quoted  \nnormal again"

    def test_strips_existing_trailing_spaces_before_adding(self) -> None:
        result = _add_blockquote_hardbreaks("> line   ")
        assert result == "> line  "

    def test_empty_string(self) -> None:
        assert _add_blockquote_hardbreaks("") == ""


class MarkdownModeTest(unittest.TestCase):
    def _html_part(self, body: str) -> str:
        msg = build_email_message(
            from_address="alice@example.com",
            to="bob@example.com",
            cc="",
            bcc="",
            subject="Test",
            body=body,
            attachment_paths=[],
            markdown_mode=True,
        )
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                assert isinstance(payload, bytes)
                return payload.decode("utf-8")
        return ""

    def test_blockquote_lines_have_breaks_in_html(self) -> None:
        body = "> line one\n> line two\n> line three"
        html = self._html_part(body)
        assert "<br" in html

    def test_plain_part_preserves_blockquote_lines(self) -> None:
        body = "> line one\n> line two\n> line three"
        msg = build_email_message(
            from_address="alice@example.com",
            to="bob@example.com",
            cc="",
            bcc="",
            subject="Test",
            body=body,
            attachment_paths=[],
            markdown_mode=True,
        )
        plain = msg.get_body(preferencelist=("plain",))
        assert plain is not None
        content = plain.get_payload(decode=True)
        assert isinstance(content, bytes)
        text = content.decode("utf-8")
        assert "> line one" in text
        assert "> line two" in text
        assert "> line three" in text

    def test_forwarded_headers_have_html_line_breaks(self) -> None:
        body = build_forward_body(_rendered(body="Forwarded body."))
        html = self._html_part(body)

        assert "---------- Forwarded message ----------<br" in html
        assert "From: Alice &lt;alice@example.com&gt;<br" in html
        assert "Subject: Hello<br" in html

    def test_short_forward_separator_is_not_rendered_as_markdown(self) -> None:
        body = "\n---- Forwarded ----\nFrom: Alice\nDate: Today\n\nBody"
        html = self._html_part(body)

        assert "---- Forwarded ----<br" in html
        assert "From: Alice<br" in html


def _make_account(
    *,
    smtp: SmtpConfig | None = None,
    **kwargs: object,
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
                smtp=smtp,
                username="alice",
                password="secret",
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
                smtp=smtp,
                username="alice",
                password="secret",
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
                smtp=smtp,
                username="alice",
                password="secret",
                msg=EmailMessage(),
            )


class SplitAtQuoteBoundaryTest(unittest.TestCase):
    def test_no_boundary_returns_full_text(self) -> None:
        text = "Hello world"
        user, quoted = _split_at_quote_boundary(text)
        assert user == "Hello world"
        assert quoted == ""

    def test_on_wrote_boundary_splits_correctly(self) -> None:
        text = "My reply\n\nOn Mon, 1 Jan wrote:\n\n> Original"
        user, quoted = _split_at_quote_boundary(text)
        assert "My reply" in user
        assert "On Mon, 1 Jan wrote:" in quoted

    def test_boundary_at_start_no_newline_before(self) -> None:
        text = "On Mon wrote:\n\n> Original"
        user, quoted = _split_at_quote_boundary(text)
        assert user == ""
        assert "On Mon wrote:" in quoted


class NewComposeBodyTest(unittest.TestCase):
    def test_no_signature_returns_empty(self) -> None:
        assert new_compose_body(None) == ""

    def test_with_signature_includes_sig_block(self) -> None:
        body = new_compose_body("Alice Smith")
        assert "Alice Smith" in body
        assert "-- " in body


class BuildReplyBodyWithSignatureTest(unittest.TestCase):
    def test_reply_body_includes_signature(self) -> None:
        r = _rendered(body="Some message.")
        body = build_reply_body(r, signature="Alice")
        assert "Alice" in body
        assert "-- " in body


class BuildForwardBodyWithSignatureTest(unittest.TestCase):
    def test_forward_body_includes_signature(self) -> None:
        r = _rendered(body="Forward content.")
        body = build_forward_body(r, signature="Alice")
        assert "Alice" in body
        assert "-- " in body


class ParseDraftFieldsTest(unittest.TestCase):
    def test_extracts_headers_and_body(self) -> None:
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Cc"] = "carol@example.com"
        msg["Bcc"] = "dan@example.com"
        msg["Subject"] = "Test draft"
        msg.set_content("Draft body text")
        raw = msg.as_bytes()

        fields = parse_draft_fields(raw)
        assert fields["to"] == "bob@example.com"
        assert fields["cc"] == "carol@example.com"
        assert fields["bcc"] == "dan@example.com"
        assert fields["subject"] == "Test draft"
        assert "Draft body text" in fields["body"]

    def test_missing_headers_return_empty_strings(self) -> None:
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg.set_content("body only")
        fields = parse_draft_fields(msg.as_bytes())
        assert fields["to"] == ""
        assert fields["cc"] == ""
        assert fields["subject"] == ""


class BuildEmailMessageBranchesTest(unittest.TestCase):
    def _build_md(self, body: str, **kwargs: Any) -> EmailMessage:
        return build_email_message(
            from_address="alice@example.com",
            to="bob@example.com",
            cc="",
            bcc="",
            subject="Test",
            body=body,
            attachment_paths=[],
            markdown_mode=True,
            **kwargs,
        )

    def test_markdown_with_signature_renders_sig_section(self) -> None:
        body = "Hello world\n\n-- \nAlice Smith"
        msg = self._build_md(body)
        html_parts = [
            str(part.get_content())
            for part in msg.walk()
            if part.get_content_type() == "text/html"
        ]
        assert html_parts
        assert any("Alice" in h for h in html_parts)

    def test_markdown_with_quoted_content(self) -> None:
        body = "My reply\n\nOn Mon wrote:\n\n> Original text"
        msg = self._build_md(body)
        html_parts = [
            str(part.get_content())
            for part in msg.walk()
            if part.get_content_type() == "text/html"
        ]
        assert any("Original text" in h for h in html_parts)

    def test_build_email_with_bcc(self) -> None:
        msg = build_email_message(
            from_address="alice@example.com",
            to="bob@example.com",
            cc="",
            bcc="carol@example.com",
            subject="BCC test",
            body="body",
            attachment_paths=[],
        )
        assert msg["Bcc"] == "carol@example.com"

    def test_unknown_mime_type_falls_back_to_octet_stream(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".xyz_unknown", delete=False) as f:
            f.write(b"binary data")
            tmp = Path(f.name)
        try:
            msg = build_email_message(
                from_address="a@example.com",
                to="b@example.com",
                cc="",
                bcc="",
                subject="x",
                body="x",
                attachment_paths=[tmp],
            )
            payloads = list(msg.iter_attachments())
            assert len(payloads) == 1
            assert payloads[0].get_content_type() == "application/octet-stream"
        finally:
            tmp.unlink(missing_ok=True)


_FORWARDED_EML = (
    b"From: Carol <carol@example.com>\r\n"
    b"To: Alice <alice@example.com>\r\n"
    b"Subject: Original subject\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n"
    b"\r\n"
    b"Original body with \xc3\xa9 accents.\r\n"
)


class ForwardedMessageAttachmentTest(unittest.TestCase):
    """A forwarded .eml must stay openable in the receiving client."""

    def _forward_with_attachment(self) -> EmailMessage:
        with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as f:
            f.write(_FORWARDED_EML)
            tmp = Path(f.name)
        try:
            return build_email_message(
                from_address="a@example.com",
                to="b@example.com",
                cc="",
                bcc="",
                subject="Fwd: Original subject",
                body="See below.",
                attachment_paths=[tmp],
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_message_rfc822_is_not_base64_encoded(self) -> None:
        msg = self._forward_with_attachment()

        attachment = next(iter(msg.iter_attachments()))
        assert attachment.get_content_type() == "message/rfc822"
        # RFC 2046 s5.2.1: only 7bit/8bit/binary are legal here.
        assert attachment["Content-Transfer-Encoding"] in ("7bit", "8bit", "binary")

    def test_forwarded_message_survives_a_serialise_parse_round_trip(self) -> None:
        import email
        import email.policy

        reparsed = email.message_from_bytes(
            self._forward_with_attachment().as_bytes(), policy=email.policy.default
        )

        attached = next(
            p for p in reparsed.walk() if p.get_content_type() == "message/rfc822"
        )
        inner = attached.get_payload(0)
        assert isinstance(inner, EmailMessage)
        assert str(inner["Subject"]) == "Original subject"
        assert "Original body with é accents." in inner.get_content()


class CrlfBodyTest(unittest.TestCase):
    """CRLF bodies must not defeat the quote/signature splits."""

    def _html_part(self, body: str) -> str:
        msg = build_email_message(
            from_address="a@example.com",
            to="b@example.com",
            cc="",
            bcc="",
            subject="s",
            body=body,
            attachment_paths=[],
            markdown_mode=True,
        )
        html = next(
            p for p in msg.walk() if p.get_content_type() == "text/html"
        ).get_content()
        assert isinstance(html, str)
        return html

    def test_forward_body_normalises_crlf_from_the_source_message(self) -> None:
        body = build_forward_body(_rendered(body="Line one.\r\nLine two.\r\n"))

        assert "\r" not in body

    def test_crlf_forward_still_uses_the_escaped_blockquote(self) -> None:
        body = build_forward_body(_rendered(body="Quoted line one.\nQuoted line two."))

        crlf_html = self._html_part(body.replace("\n", "\r\n"))

        assert crlf_html == self._html_part(body)
        assert "white-space:pre-wrap" in crlf_html
        assert "---------- Forwarded message ----------<br" in crlf_html

    def test_crlf_signature_block_is_still_split_off(self) -> None:
        body = build_forward_body(_rendered(body="Quoted."), signature="Alice\nCSIC")

        html = self._html_part(body.replace("\n", "\r\n"))

        assert "<hr" in html

    def test_parse_draft_fields_normalises_crlf(self) -> None:
        draft = EmailMessage()
        draft["To"] = "b@example.com"
        draft["Subject"] = "s"
        draft.set_content("Line one.\r\nLine two.\r\n")

        fields = parse_draft_fields(draft.as_bytes())

        assert "\r" not in fields["body"]


if __name__ == "__main__":
    unittest.main()


class SplitAddressListTest(unittest.TestCase):
    """Address lists must be split on separators, not on every comma.

    Regression: a display name containing a comma is quoted by the
    sending client (``"Doe, John" <j@example.com>``).  Splitting the
    header naively turned that one recipient into two broken entries —
    ``"Doe`` and ``John" <j@example.com>`` — which is what the user saw
    after a reply-all.  Such names are also the ones long enough to be
    folded across lines, which is why the two symptoms travel together.
    """

    def test_a_quoted_comma_does_not_start_a_new_address(self) -> None:
        self.assertEqual(
            split_address_list('"Doe, John" <john@example.com>, plain@example.com'),
            ['"Doe, John" <john@example.com>', "plain@example.com"],
        )

    def test_several_quoted_names_stay_intact(self) -> None:
        header = (
            '"Doe, John" <john@example.com>, '
            '"O\'Brien, Mary" <mary@example.com>, '
            "plain@example.com"
        )
        self.assertEqual(len(split_address_list(header)), 3)

    def test_plain_lists_are_unchanged(self) -> None:
        self.assertEqual(
            split_address_list("a@example.com, b@example.com"),
            ["a@example.com", "b@example.com"],
        )

    def test_an_escaped_quote_inside_a_name_is_not_a_delimiter(self) -> None:
        self.assertEqual(
            split_address_list(r'"a\"b, c" <x@example.com>'),
            [r'"a\"b, c" <x@example.com>'],
        )

    def test_blank_and_empty_entries_are_dropped(self) -> None:
        self.assertEqual(split_address_list(""), [])
        self.assertEqual(split_address_list("   "), [])
        self.assertEqual(
            split_address_list("a@example.com,, b@example.com"),
            ["a@example.com", "b@example.com"],
        )

    def test_rejoining_reproduces_the_header(self) -> None:
        """The composer collects rows back with ``", "`` — that must round-trip."""
        header = '"Doe, John" <john@example.com>, plain@example.com'
        self.assertEqual(", ".join(split_address_list(header)), header)

    def test_unbalanced_quotes_fall_back_to_the_old_split(self) -> None:
        """Malformed input must not be handled worse than it used to be."""
        header = '"Unclosed <broken@example.com>, other@example.com'
        naive = [part.strip() for part in header.split(",") if part.strip()]
        self.assertEqual(split_address_list(header), naive)


class SplitTrailingAddressTest(unittest.TestCase):
    """Autocompletion must replace only the address being typed.

    Regression: with a complete quoted address already in the field, the
    last comma is *inside* the display name, so the old ``rfind(",")``
    treated the field as "``"Doe,`` plus a token being typed".  Accepting
    a completion then overwrote the existing recipient, leaving
    ``"Doe, mary@example.com``.
    """

    def test_a_complete_quoted_address_leaves_nothing_to_preserve(self) -> None:
        prefix, typed = split_trailing_address('"Doe, John" <john@example.com>')
        self.assertEqual(prefix, "")
        self.assertEqual(typed, '"Doe, John" <john@example.com>')

    def test_a_quoted_address_is_preserved_while_typing_the_next(self) -> None:
        prefix, typed = split_trailing_address('"Doe, John" <john@example.com>, ma')
        self.assertEqual(prefix, '"Doe, John" <john@example.com>, ')
        self.assertEqual(typed, "ma")
        self.assertEqual(
            prefix + "mary@example.com",
            ('"Doe, John" <john@example.com>, mary@example.com'),
        )

    def test_plain_addresses_behave_as_before(self) -> None:
        prefix, typed = split_trailing_address("alice@example.com, ma")
        self.assertEqual(prefix, "alice@example.com, ")
        self.assertEqual(typed, "ma")

    def test_a_single_token_has_no_prefix(self) -> None:
        self.assertEqual(split_trailing_address("ma"), ("", "ma"))

    def test_unbalanced_quotes_fall_back_to_the_last_comma(self) -> None:
        value = '"Unclosed <broken@example.com>, ma'
        prefix, typed = split_trailing_address(value)
        last = value.rfind(",")
        self.assertEqual(prefix, value[: last + 1] + " ")
        self.assertEqual(typed, "ma")
