"""Tests for projecting raw RFC 5322 messages into indexed metadata."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING

import corpus

from pony.domain import MessageRef
from pony.message_projection import project_rfc822_message

if TYPE_CHECKING:
    from pony.domain import IndexedMessage

# Shared MessageRef used across all projection tests.
_REF = MessageRef(
    account_name="personal",
    folder_name="INBOX",
    rfc5322_id="test-msg",
)


def _project(raw: bytes) -> IndexedMessage:
    """Convenience wrapper that projects *raw* bytes with a fixed ref."""
    return project_rfc822_message(
        message_ref=_REF,
        raw_message=raw,
        storage_key="test-msg",
    )


class PlainTextProjectionTest(unittest.TestCase):
    """Baseline: simple text/plain message."""

    def setUp(self) -> None:
        self.msg = _project(corpus.plain_text())

    def test_headers_extracted(self) -> None:
        self.assertEqual(self.msg.sender, "Alice Smith <alice@example.com>")
        self.assertEqual(self.msg.recipients, "Bob Jones <bob@example.com>")
        self.assertEqual(self.msg.cc, "Carol White <carol@example.com>")
        self.assertEqual(self.msg.subject, "Tuesday meeting confirmed")

    def test_body_preview_contains_text(self) -> None:
        self.assertIn("Tuesday", self.msg.body_preview)

    def test_no_attachments(self) -> None:
        self.assertFalse(self.msg.has_attachments)

    def test_date_parsed(self) -> None:
        self.assertEqual(self.msg.received_at.year, 2026)
        self.assertEqual(self.msg.received_at.month, 4)


class MultipartAlternativeProjectionTest(unittest.TestCase):
    """multipart/alternative: must prefer text/plain over text/html."""

    def setUp(self) -> None:
        self.msg = _project(corpus.multipart_alternative())

    def test_body_preview_is_plain_text_not_html(self) -> None:
        preview = self.msg.body_preview
        self.assertIn("Tuesday", preview)
        self.assertNotIn("<", preview)
        self.assertNotIn(">", preview)

    def test_no_attachments(self) -> None:
        self.assertFalse(self.msg.has_attachments)


class MultipartMixedAttachmentProjectionTest(unittest.TestCase):
    """multipart/mixed with one attachment."""

    def setUp(self) -> None:
        self.msg = _project(corpus.multipart_mixed_attachment())

    def test_has_attachments(self) -> None:
        self.assertTrue(self.msg.has_attachments)

    def test_body_preview_is_text_not_pdf(self) -> None:
        preview = self.msg.body_preview
        self.assertIn("Q1 report", preview)
        self.assertNotIn("%PDF", preview)


class MultipartMixedMultiAttachmentProjectionTest(unittest.TestCase):
    """multipart/mixed with two attachments."""

    def setUp(self) -> None:
        self.msg = _project(corpus.multipart_mixed_multi())

    def test_has_attachments(self) -> None:
        self.assertTrue(self.msg.has_attachments)

    def test_body_preview_from_text_part(self) -> None:
        self.assertIn("files", self.msg.body_preview)


class HtmlOnlyProjectionTest(unittest.TestCase):
    """HTML-only message: body_preview must fall back to stripped HTML."""

    def setUp(self) -> None:
        self.msg = _project(corpus.html_only())

    def test_body_preview_not_empty(self) -> None:
        self.assertNotEqual(self.msg.body_preview, "")

    def test_body_preview_contains_text_not_tags(self) -> None:
        preview = self.msg.body_preview
        self.assertIn("Tuesday", preview)
        self.assertNotIn("<html>", preview)
        self.assertNotIn("<p>", preview)

    def test_no_attachments(self) -> None:
        self.assertFalse(self.msg.has_attachments)


class HtmlStyleScriptProjectionTest(unittest.TestCase):
    """HTML body_preview must strip <style>/<script> content, not just tags."""

    @staticmethod
    def _html_message(html_body: str) -> bytes:
        msg = EmailMessage()
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Subject"] = "HTML with style"
        msg.set_content(html_body, subtype="html")
        return msg.as_bytes()

    def test_style_block_content_not_in_preview(self) -> None:
        raw = self._html_message(
            "<html><head><style>.foo { color: red; }</style></head>"
            "<body><p>Hello world</p></body></html>"
        )
        preview = _project(raw).body_preview
        self.assertIn("Hello world", preview)
        self.assertNotIn("color", preview)
        self.assertNotIn(".foo", preview)

    def test_script_block_content_not_in_preview(self) -> None:
        raw = self._html_message(
            "<html><body><script>var x = 1;</script>"
            "<p>Visible text</p></body></html>"
        )
        preview = _project(raw).body_preview
        self.assertIn("Visible text", preview)
        self.assertNotIn("var x", preview)


class EncodedHeadersProjectionTest(unittest.TestCase):
    """RFC 2047 encoded Subject and From must be decoded to Unicode."""

    def setUp(self) -> None:
        self.msg = _project(corpus.encoded_headers())

    def test_subject_decoded(self) -> None:
        # Raw bytes: =?UTF-8?Q?R=C3=A9union=3A_Probl=C3=A8me_r=C3=A9solu?=
        # Decoded:   Réunion: Problème résolu
        self.assertIn("Réunion", self.msg.subject)
        self.assertNotIn("=?UTF-8", self.msg.subject)

    def test_sender_display_name_decoded(self) -> None:
        # Raw: =?UTF-8?Q?Andr=C3=A9_M=C3=BCller?=
        # Decoded: André Müller
        self.assertIn("André", self.msg.sender)
        self.assertNotIn("=?UTF-8", self.msg.sender)

    def test_body_preview_present(self) -> None:
        self.assertIn("Corps", self.msg.body_preview)


class MissingDateProjectionTest(unittest.TestCase):
    """No Date header: projection must fall back to now() without raising."""

    def test_received_at_fallback_is_recent(self) -> None:
        before = datetime.now(tz=UTC)
        msg = _project(corpus.missing_date())
        after = datetime.now(tz=UTC)
        self.assertGreaterEqual(msg.received_at, before)
        self.assertLessEqual(msg.received_at, after)

    def test_other_fields_still_projected(self) -> None:
        msg = _project(corpus.missing_date())
        self.assertEqual(msg.subject, "No date header")


class MissingMessageIdProjectionTest(unittest.TestCase):
    """No Message-ID header: projection succeeds without raising."""

    def test_projection_does_not_raise(self) -> None:
        msg = _project(corpus.missing_message_id())
        self.assertEqual(msg.subject, "No message-id header")

    def test_body_preview_present(self) -> None:
        msg = _project(corpus.missing_message_id())
        self.assertIn("message-id", msg.body_preview)


class InlineImageProjectionTest(unittest.TestCase):
    """Inline CID image must NOT be counted as an attachment."""

    def setUp(self) -> None:
        self.msg = _project(corpus.inline_image())

    def test_inline_image_not_counted_as_attachment(self) -> None:
        self.assertFalse(self.msg.has_attachments)

    def test_body_preview_from_plain_fallback(self) -> None:
        self.assertIn("Hello", self.msg.body_preview)


class QuotedPrintableBodyProjectionTest(unittest.TestCase):
    """QP-encoded body must be decoded to Unicode in the preview."""

    def setUp(self) -> None:
        self.msg = _project(corpus.quoted_printable_body())

    def test_qp_sequences_decoded(self) -> None:
        preview = self.msg.body_preview
        # =C3=A9 → é,  =C3=BC → ü,  =C3=B6 → ö
        self.assertIn("Café", preview)
        self.assertNotIn("=C3", preview)

    def test_no_attachments(self) -> None:
        self.assertFalse(self.msg.has_attachments)


class LegacyProjectionTest(unittest.TestCase):
    """Original single test kept for regression coverage."""

    def test_projection_extracts_headers_body_and_date(self) -> None:
        message = EmailMessage()
        message["From"] = "Alice <alice@example.com>"
        message["To"] = "Bob <bob@example.com>"
        message["Cc"] = "Carol <carol@example.com>"
        message["Subject"] = "Status Update"
        message["Date"] = "Fri, 10 Apr 2026 12:34:56 +0000"
        message.set_content("Line one.\n\nLine two.\n")

        indexed = project_rfc822_message(
            message_ref=MessageRef(
                account_name="personal",
                folder_name="INBOX",
                rfc5322_id="m-42",
            ),
            raw_message=message.as_bytes(),
            storage_key="m-42",
        )

        self.assertEqual(indexed.sender, "Alice <alice@example.com>")
        self.assertEqual(indexed.recipients, "Bob <bob@example.com>")
        self.assertEqual(indexed.cc, "Carol <carol@example.com>")
        self.assertEqual(indexed.subject, "Status Update")
        self.assertEqual(indexed.body_preview, "Line one. Line two.")


class Base64BodyProjectionTest(unittest.TestCase):
    """Base64-encoded body must be decoded to readable text."""

    def setUp(self) -> None:
        self.msg = _project(corpus.base64_body())

    def test_body_decoded(self) -> None:
        self.assertIn("base64-encoded", self.msg.body_preview)
        self.assertNotIn("VGhpcw", self.msg.body_preview)  # no raw b64


class NonAsciiSenderProjectionTest(unittest.TestCase):
    """CJK display name in From must be decoded to Unicode."""

    def setUp(self) -> None:
        self.msg = _project(corpus.non_ascii_sender())

    def test_sender_decoded(self) -> None:
        # 山田太郎
        self.assertIn("山田太郎", self.msg.sender)

    def test_body_present(self) -> None:
        self.assertIn("CJK sender", self.msg.body_preview)


class EmptyBodyProjectionTest(unittest.TestCase):
    """Empty body must not crash; preview should be empty."""

    def setUp(self) -> None:
        self.msg = _project(corpus.empty_body())

    def test_no_crash(self) -> None:
        self.assertEqual(self.msg.subject, "Empty body")

    def test_body_preview_empty(self) -> None:
        self.assertEqual(self.msg.body_preview, "")


class VeryLongSubjectProjectionTest(unittest.TestCase):
    """Very long subject must be preserved without truncation."""

    def setUp(self) -> None:
        self.msg = _project(corpus.very_long_subject())

    def test_subject_long(self) -> None:
        self.assertGreater(len(self.msg.subject), 100)
        self.assertIn("very long subject", self.msg.subject)


class NestedForwardProjectionTest(unittest.TestCase):
    """Forwarded message with inner attachment."""

    def setUp(self) -> None:
        self.msg = _project(corpus.nested_forward())

    def test_body_from_outer_text(self) -> None:
        self.assertIn("FYI", self.msg.body_preview)

    def test_subject(self) -> None:
        self.assertIn("Fwd:", self.msg.subject)


class ManyRecipientsProjectionTest(unittest.TestCase):
    """Many To/Cc recipients must be preserved."""

    def setUp(self) -> None:
        self.msg = _project(corpus.many_recipients())

    def test_recipients_contain_all(self) -> None:
        self.assertIn("user1@example.com", self.msg.recipients)
        self.assertIn("user20@example.com", self.msg.recipients)

    def test_cc_present(self) -> None:
        self.assertIn("cc1@example.com", self.msg.cc)


# ---------------------------------------------------------------------------
# Renderer tests (message_renderer, not projection)
# ---------------------------------------------------------------------------


class NestedEmailRendererTest(unittest.TestCase):
    """Attached emails are listed as attachments with header separators."""

    def setUp(self) -> None:
        from pony.tui.message_renderer import render_message

        self.rendered = render_message(corpus.double_attached_emails())

    def test_attached_emails_listed(self) -> None:
        eml_atts = [
            a for a in self.rendered.attachments
            if a.content_type == "message/rfc822"
        ]
        self.assertEqual(len(eml_atts), 2)
        names = {a.filename for a in eml_atts}
        self.assertIn("Contract draft.eml", names)
        self.assertIn("Budget numbers.eml", names)

    def test_inner_attachments_listed(self) -> None:
        file_atts = [
            a for a in self.rendered.attachments
            if a.content_type != "message/rfc822"
        ]
        names = {a.filename for a in file_atts}
        self.assertIn("contract.pdf", names)
        self.assertIn("budget.xlsx", names)

    def test_body_contains_separators(self) -> None:
        self.assertIn("Attached email: Contract draft", self.rendered.body)
        self.assertIn("Attached email: Budget numbers", self.rendered.body)
        self.assertIn("charlie@example.com", self.rendered.body)

    def test_body_contains_outer_text(self) -> None:
        self.assertIn("Please review both", self.rendered.body)

    def test_total_attachment_count(self) -> None:
        # 2 attached emails + 2 inner file attachments = 4
        self.assertEqual(len(self.rendered.attachments), 4)


class NestedEmailBrowserHtmlTest(unittest.TestCase):
    """Browser HTML includes nested email headers."""

    def test_nested_headers_in_html(self) -> None:
        from pony.tui.message_renderer import build_browser_html

        html = build_browser_html(corpus.double_attached_emails())
        self.assertIn("Contract draft", html)
        self.assertIn("Budget numbers", html)
        self.assertIn("charlie@example.com", html)


class HtmlOnlyRenderTest(unittest.TestCase):
    """HTML-only emails should have style/script blocks stripped."""

    def _make_html_only_message(self, html_body: str) -> bytes:
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Subject"] = "HTML only"
        msg.set_content(html_body, subtype="html")
        return msg.as_bytes()

    def test_style_block_not_in_body(self) -> None:
        from pony.tui.message_renderer import render_message

        raw = self._make_html_only_message(
            "<html><head><style>.foo { color: red; }</style></head>"
            "<body><p>Hello world</p></body></html>"
        )
        rendered = render_message(raw)
        self.assertIn("Hello world", rendered.body)
        self.assertNotIn("color", rendered.body)
        self.assertNotIn(".foo", rendered.body)

    def test_script_block_not_in_body(self) -> None:
        from pony.tui.message_renderer import render_message

        raw = self._make_html_only_message(
            "<html><body><script>var x = 1;</script>"
            "<p>Visible text</p></body></html>"
        )
        rendered = render_message(raw)
        self.assertIn("Visible text", rendered.body)
        self.assertNotIn("var x", rendered.body)

    def test_multiple_style_blocks_stripped(self) -> None:
        from pony.tui.message_renderer import render_message

        raw = self._make_html_only_message(
            "<style>body{margin:0}</style>"
            "<p>Content</p>"
            "<style>.x{font-size:12px}</style>"
        )
        rendered = render_message(raw)
        self.assertIn("Content", rendered.body)
        self.assertNotIn("margin", rendered.body)
        self.assertNotIn("font-size", rendered.body)
