"""Tests for ``pony.tui.message_renderer.extract_attachment``."""

from __future__ import annotations

import unittest

import corpus

from pony.tui.message_renderer import extract_attachment, render_message


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
