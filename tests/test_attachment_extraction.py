"""Tests for ``pony.message_renderer.extract_attachment``."""

from __future__ import annotations

import base64
import unittest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import corpus

from pony.message_renderer import (
    build_browser_html,
    extract_attachment,
    fmt_size,
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


class BrowserHtmlBodyVariantsTest(unittest.TestCase):
    """``build_browser_html`` across the body shapes it has to handle."""

    def test_inline_image_is_inlined_as_a_data_uri(self) -> None:
        """A CID-referenced image must survive as a self-contained data: URI.

        The exported HTML is opened straight from a temp file, so a
        surviving ``cid:`` reference would render as a broken image.
        """
        html = build_browser_html(corpus.inline_image())

        self.assertIn("data:image/png;base64,", html)
        self.assertNotIn("cid:logo@example.com", html)

    def test_plain_only_message_is_wrapped_in_a_pre_block(self) -> None:
        html = build_browser_html(corpus.plain_text())

        self.assertIn("<pre", html)

    def test_message_without_any_text_part_says_so(self) -> None:
        html = build_browser_html(corpus.attachment_only())

        self.assertIn("(no readable content)", html)
        self.assertIn("thing.bin", html)


class FmtSizeTest(unittest.TestCase):
    """``fmt_size`` steps through the units until the value fits."""

    def test_bytes_are_shown_whole(self) -> None:
        self.assertEqual(fmt_size(512), "512 B")

    def test_kilobytes_are_shown_with_one_decimal(self) -> None:
        self.assertEqual(fmt_size(2048), "2.0 KB")

    def test_megabytes_are_shown_with_one_decimal(self) -> None:
        self.assertEqual(fmt_size(5 * 1024 * 1024), "5.0 MB")

    def test_anything_larger_falls_through_to_gigabytes(self) -> None:
        self.assertEqual(fmt_size(5 * 1024**3), "5.0 GB")


def _browser_attachment_names(html: str) -> list[str]:
    """Names listed in the export's attachment block."""
    import re

    return [
        re.sub(r"<span.*?</span>", "", item).strip()
        for item in re.findall(r"<li>(.*?)</li>", html, re.DOTALL)
    ]


class OneAttachmentEnumerationTest(unittest.TestCase):
    """The reader, the extractor and the export must agree, part for part.

    ``[1]`` in the reader is what ``pony message attachment 1`` writes and
    what the MCP ``get_attachment`` tool returns, so the three MIME walks
    that produce those lists have to enumerate identically.  They did not:
    the export applied its own predicate, so an inline part carrying a
    filename was renamed, dropped, or — worst — treated as the message
    body, making ``w`` and ``ctrl+p`` show different text than the pane
    they were opened from.
    """

    _FIXTURES = (
        "plain_text",
        "multipart_alternative",
        "multipart_mixed_attachment",
        "multipart_mixed_multi",
        "html_only",
        "inline_image",
        "nested_forward",
        "double_attached_emails",
        "attachment_only",
        "html_first_multipart",
        "base64_rfc822_attachment",
        "inline_image_named",
        "inline_text_named",
        "inline_html_named",
        "unnamed_attachment",
    )

    def test_all_three_walks_list_the_same_attachments(self) -> None:
        for name in self._FIXTURES:
            with self.subTest(fixture=name):
                raw = getattr(corpus, name)()
                reader = [a.filename for a in render_message(raw).attachments]
                exported = _browser_attachment_names(build_browser_html(raw))

                self.assertEqual(
                    reader, exported, f"{name}: reader and export disagree"
                )

    def test_extraction_matches_the_reader_index_for_index(self) -> None:
        for name in self._FIXTURES:
            with self.subTest(fixture=name):
                raw = getattr(corpus, name)()
                for listed in render_message(raw).attachments:
                    payload = extract_attachment(raw, listed.index)
                    self.assertIsNotNone(payload, f"{name}[{listed.index}] missing")
                    assert payload is not None
                    self.assertEqual(payload.filename, listed.filename)
                    self.assertEqual(payload.content_type, listed.content_type)

    def test_an_inline_part_with_a_filename_is_an_attachment_everywhere(self) -> None:
        """Case A: it used to be renamed to a synthesized type-based name."""
        raw = corpus.inline_image_named()

        self.assertEqual(
            [a.filename for a in render_message(raw).attachments],
            ["logo.png", "doc.pdf"],
        )
        self.assertEqual(
            _browser_attachment_names(build_browser_html(raw)),
            ["logo.png", "doc.pdf"],
        )

    def test_a_named_inline_text_part_is_not_swallowed(self) -> None:
        """Case B: it used to vanish from the export, shifting the numbering."""
        raw = corpus.inline_text_named()

        self.assertEqual(
            _browser_attachment_names(build_browser_html(raw)),
            ["notes.txt", "doc.pdf"],
        )

    def test_a_named_inline_html_part_is_never_used_as_the_body(self) -> None:
        """Case C: the export rendered it as the body, contradicting the reader."""
        raw = corpus.inline_html_named()
        html = build_browser_html(raw)

        self.assertIn("hello plain body", render_message(raw).body)
        self.assertIn("hello plain body", html)
        self.assertNotIn("REPORT HTML", html)
        self.assertEqual(_browser_attachment_names(html), ["report.html", "doc.pdf"])

    def test_an_attachment_with_no_filename_reads_the_same_everywhere(self) -> None:
        raw = corpus.unnamed_attachment()

        self.assertEqual(
            [a.filename for a in render_message(raw).attachments], ["(unnamed)"]
        )
        self.assertEqual(
            _browser_attachment_names(build_browser_html(raw)), ["(unnamed)"]
        )
        payload = extract_attachment(raw, 1)
        assert payload is not None
        self.assertEqual(payload.filename, "(unnamed)")
