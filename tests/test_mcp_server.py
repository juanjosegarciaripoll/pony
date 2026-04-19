"""Tests for ``pony.mcp_server`` helpers.

The MCP tools themselves are closures built inside ``build_mcp_server``
and are covered indirectly by the CLI tests that exercise the shared
``extract_attachment`` / ``render_message`` paths.  This file pins the
MCP-specific serialisation invariants — namely the text-vs-base64 split
in ``_attachment_to_dict``, which is what makes ``get_attachment``
usable by AI agents without client-side decoding.
"""

from __future__ import annotations

import base64
import unittest

from pony.mcp_server import _attachment_to_dict
from pony.tui.message_renderer import AttachmentPayload


class AttachmentToDictTest(unittest.TestCase):
    """Per-attachment dict shape returned by the MCP ``get_attachment`` tool."""

    def test_binary_attachment_has_base64_only(self) -> None:
        payload = AttachmentPayload(
            filename="report.pdf",
            content_type="application/pdf",
            size_bytes=5,
            data=b"%PDF-",
        )
        result = _attachment_to_dict(payload)
        self.assertEqual(result["filename"], "report.pdf")
        self.assertEqual(result["content_type"], "application/pdf")
        self.assertEqual(result["size_bytes"], 5)
        self.assertEqual(
            base64.b64decode(result["data_base64"]), b"%PDF-",
        )
        # No `text` field for non-text content types — clients must
        # decode data_base64 themselves (or use a suitable reader).
        self.assertNotIn("text", result)

    def test_text_plain_attachment_includes_decoded_text(self) -> None:
        payload = AttachmentPayload(
            filename="notes.txt",
            content_type="text/plain",
            size_bytes=11,
            data=b"hello world",
        )
        result = _attachment_to_dict(payload)
        self.assertEqual(result["text"], "hello world")
        # Base64 is still present as the canonical, lossless form.
        self.assertEqual(
            base64.b64decode(result["data_base64"]), b"hello world",
        )

    def test_text_attachment_decodes_utf8(self) -> None:
        data = "Café — résumé".encode()
        payload = AttachmentPayload(
            filename="notes.txt",
            content_type="text/plain",
            size_bytes=len(data),
            data=data,
        )
        result = _attachment_to_dict(payload)
        self.assertEqual(result["text"], "Café — résumé")

    def test_text_attachment_falls_back_to_latin1_on_bad_utf8(self) -> None:
        """If the bytes aren't valid UTF-8 but the content type claims
        text/*, we still surface *something* readable rather than
        dropping the attachment on the floor."""
        # 0xe9 is 'é' in latin-1 but is an invalid UTF-8 lead byte on its own.
        data = b"\xe9claire"
        payload = AttachmentPayload(
            filename="bad.txt",
            content_type="text/plain",
            size_bytes=len(data),
            data=data,
        )
        result = _attachment_to_dict(payload)
        self.assertIn("text", result)
        self.assertEqual(result["text"], "éclaire")

    def test_text_html_attachment_also_gets_text(self) -> None:
        payload = AttachmentPayload(
            filename="page.html",
            content_type="text/html",
            size_bytes=13,
            data=b"<p>hi</p>",
        )
        result = _attachment_to_dict(payload)
        self.assertEqual(result["text"], "<p>hi</p>")
