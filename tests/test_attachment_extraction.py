"""Tests for ``pony.tui.message_renderer.extract_attachment``."""

from __future__ import annotations

import base64
import unittest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import corpus

from pony.tui.message_renderer import (
    build_browser_html,
    extract_attachment,
    render_message,
)

# ---------------------------------------------------------------------------
# Shared inline-part message fixtures
# ---------------------------------------------------------------------------

_ICAL_CONTENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//Test//EN\r\n"
    "BEGIN:VEVENT\r\nSUMMARY:Test Event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)
_VCARD_CONTENT = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Alice Smith\r\n"
    "EMAIL:alice@example.com\r\nEND:VCARD\r\n"
)
_ENRICHED_CONTENT = "<bold>Hello world</bold>"


def _calendar_message() -> bytes:
    """multipart/alternative: text/plain (2 B), text/html (0 B), text/calendar."""
    msg = MIMEMultipart("alternative")
    msg["From"] = corpus.FROM_ADDR
    msg["To"] = corpus.TO_ADDR
    msg["Subject"] = "Meeting invite"
    msg["Date"] = corpus.DATE
    msg["Message-ID"] = "<cal-fixture@example.com>"
    msg.attach(MIMEText("Hi", "plain", "utf-8"))
    msg.attach(MIMEText("", "html", "utf-8"))
    cal_part = MIMEText(_ICAL_CONTENT, "calendar", "utf-8")
    cal_part["Content-Transfer-Encoding"] = "base64"
    # Re-set the payload as base64-encoded to match the branch under test.
    encoded = base64.b64encode(_ICAL_CONTENT.encode()).decode()
    cal_part.set_payload(encoded, charset=None)
    msg.attach(cal_part)
    return msg.as_bytes()


def _vcard_message() -> bytes:
    """multipart/mixed: text/plain body + inline text/vcard, no filename."""
    msg = MIMEMultipart("mixed")
    msg["From"] = corpus.FROM_ADDR
    msg["To"] = corpus.TO_ADDR
    msg["Subject"] = "Shared contact"
    msg["Date"] = corpus.DATE
    msg["Message-ID"] = "<vcard-fixture@example.com>"
    msg.attach(MIMEText("See the attached contact.\n", "plain", "utf-8"))
    vcard_part = MIMEText(_VCARD_CONTENT, "vcard", "utf-8")
    # Ensure no filename and no Content-Disposition header.
    if "Content-Disposition" in vcard_part:
        del vcard_part["Content-Disposition"]
    msg.attach(vcard_part)
    return msg.as_bytes()


def _enriched_message() -> bytes:
    """Single inline text/enriched part — no filename, no disposition."""
    msg = MIMEMultipart("mixed")
    msg["From"] = corpus.FROM_ADDR
    msg["To"] = corpus.TO_ADDR
    msg["Subject"] = "Enriched text"
    msg["Date"] = corpus.DATE
    msg["Message-ID"] = "<enriched-fixture@example.com>"
    enriched_part = MIMEText(_ENRICHED_CONTENT, "enriched", "utf-8")
    if "Content-Disposition" in enriched_part:
        del enriched_part["Content-Disposition"]
    msg.attach(enriched_part)
    return msg.as_bytes()


class ExtractAttachmentTest(unittest.TestCase):
    """Indexing + bytes contract for the shared extractor."""

    def test_extract_single_attachment_returns_bytes_and_metadata(self) -> None:
        raw = corpus.multipart_mixed_attachment()
        payload = extract_attachment(raw, 1)
        self.assertIsNotNone(payload)
        assert payload is not None  # narrow for type checker
        self.assertEqual(payload.filename, "q1-report.pdf")
        # Fixture builds via MIMEApplication with no subtype → octet-stream.
        self.assertEqual(payload.content_type, "application/octet-stream")
        self.assertTrue(payload.data.startswith(b"%PDF"))
        self.assertEqual(payload.size_bytes, len(payload.data))

    def test_indexing_matches_rendered_attachment_list(self) -> None:
        """The extractor must return the bytes for the same part the
        renderer labels with a given index — without this invariant, the
        TUI "save attachment N" action would write the wrong file."""
        raw = corpus.multipart_mixed_multi()
        rendered = render_message(raw)
        for listed in rendered.attachments:
            payload = extract_attachment(raw, listed.index)
            assert payload is not None
            self.assertEqual(payload.filename, listed.filename)
            self.assertEqual(payload.content_type, listed.content_type)
            self.assertEqual(payload.size_bytes, listed.size_bytes)

    def test_out_of_range_indices_return_none(self) -> None:
        raw = corpus.multipart_mixed_attachment()
        self.assertIsNone(extract_attachment(raw, 0))
        self.assertIsNone(extract_attachment(raw, 2))
        self.assertIsNone(extract_attachment(raw, 99))

    def test_message_with_no_attachments_returns_none(self) -> None:
        raw = corpus.plain_text()
        self.assertIsNone(extract_attachment(raw, 1))

    def test_extracts_nested_rfc822_as_eml_bytes(self) -> None:
        """message/rfc822 parts are counted as attachments and their
        bytes are the inner message serialised as ``.eml`` — the same
        contract the TUI's save-attachment action relies on."""
        raw = corpus.double_attached_emails()
        rendered = render_message(raw)
        eml_indices = [
            a.index for a in rendered.attachments if a.content_type == "message/rfc822"
        ]
        self.assertTrue(eml_indices, "fixture should contain attached emails")
        for idx in eml_indices:
            payload = extract_attachment(raw, idx)
            assert payload is not None
            self.assertEqual(payload.content_type, "message/rfc822")
            self.assertTrue(payload.filename.endswith(".eml"))
            # Serialised inner message: must have some headers at the top.
            self.assertIn(b"From:", payload.data)
            self.assertIn(b"Subject:", payload.data)


class InlinePartAttachmentTest(unittest.TestCase):
    """Inline parts with unrecognised content-types are exposed as attachments."""

    # ------------------------------------------------------------------
    # Test 1: text/calendar inline → "invite.ics"
    # ------------------------------------------------------------------

    def test_calendar_part_rendered_as_invite_ics(self) -> None:
        raw = _calendar_message()
        rendered = render_message(raw)
        names = [a.filename for a in rendered.attachments]
        self.assertEqual(
            len(rendered.attachments), 1, f"expected 1 attachment, got: {names}"
        )
        self.assertEqual(rendered.attachments[0].filename, "invite.ics")
        self.assertEqual(rendered.attachments[0].content_type, "text/calendar")

    def test_calendar_body_is_not_no_readable_content(self) -> None:
        raw = _calendar_message()
        rendered = render_message(raw)
        self.assertNotEqual(rendered.body, "(no readable content)")

    # ------------------------------------------------------------------
    # Test 2: text/vcard inline → "contact.vcf"
    # ------------------------------------------------------------------

    def test_vcard_part_rendered_as_contact_vcf(self) -> None:
        raw = _vcard_message()
        rendered = render_message(raw)
        vcf_attachments = [
            a for a in rendered.attachments if a.filename == "contact.vcf"
        ]
        self.assertTrue(vcf_attachments, "expected a 'contact.vcf' attachment")
        self.assertEqual(vcf_attachments[0].content_type, "text/vcard")

    # ------------------------------------------------------------------
    # Test 3: unknown inline type → "attachment.<subtype>"
    # ------------------------------------------------------------------

    def test_unknown_inline_type_gets_synthesised_filename(self) -> None:
        raw = _enriched_message()
        rendered = render_message(raw)
        names = [a.filename for a in rendered.attachments]
        self.assertIn("attachment.enriched", names, f"attachment list: {names}")

    # ------------------------------------------------------------------
    # Test 4: text/html does NOT become attachment when text/plain exists
    # ------------------------------------------------------------------

    def test_alternative_with_plain_and_html_has_no_attachments(self) -> None:
        raw = corpus.multipart_alternative()
        rendered = render_message(raw)
        self.assertEqual(
            len(rendered.attachments),
            0,
            "expected 0 attachments for plain+html alternative, "
            f"got: {[a.filename for a in rendered.attachments]}",
        )

    # ------------------------------------------------------------------
    # Test 5: extract_attachment for text/calendar
    # ------------------------------------------------------------------

    def test_extract_attachment_returns_calendar_payload(self) -> None:
        raw = _calendar_message()
        payload = extract_attachment(raw, 1)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.filename, "invite.ics")
        self.assertGreater(len(payload.data), 0)

    # ------------------------------------------------------------------
    # Test 6: build_browser_html lists text/calendar as "invite.ics"
    # ------------------------------------------------------------------

    def test_build_browser_html_lists_calendar_as_invite_ics(self) -> None:
        raw = _calendar_message()
        html = build_browser_html(raw)
        self.assertIn("invite.ics", html)
