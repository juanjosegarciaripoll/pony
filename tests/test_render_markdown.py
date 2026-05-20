"""Tests for ``pony.tui.message_renderer.render_message_markdown``."""

from __future__ import annotations

import unittest

import corpus

from pony.tui.message_renderer import render_message, render_message_markdown


class RenderMessageMarkdownTest(unittest.TestCase):
    def test_header_block_appears_at_top(self) -> None:
        raw = corpus.multipart_mixed_attachment()
        rendered = render_message(raw)
        md = render_message_markdown(rendered)
        self.assertTrue(md.startswith("**From:**"))
        self.assertIn("**To:**", md)
        self.assertIn("**Subject:**", md)
        self.assertIn("**Date:**", md)

    def test_body_follows_separator(self) -> None:
        raw = corpus.plain_text()
        rendered = render_message(raw)
        md = render_message_markdown(rendered)
        self.assertIn("---", md)
        self.assertIn(rendered.body.strip()[:20], md)

    def test_attachments_section_present(self) -> None:
        raw = corpus.multipart_mixed_attachment()
        rendered = render_message(raw)
        md = render_message_markdown(rendered)
        self.assertIn("## Attachments", md)
        self.assertIn("q1-report.pdf", md)

    def test_no_attachments_section_absent(self) -> None:
        raw = corpus.plain_text()
        rendered = render_message(raw)
        md = render_message_markdown(rendered)
        self.assertNotIn("## Attachments", md)

    def test_html_only_body_stripped_to_plain_text(self) -> None:
        raw = corpus.html_only()
        rendered = render_message(raw)
        md = render_message_markdown(rendered)
        self.assertNotIn("<html", md)
        self.assertNotIn("<body", md)
        self.assertNotIn("<p>", md)
        self.assertGreater(len(rendered.body.strip()), 0)

    def test_cc_included_when_present(self) -> None:
        raw = corpus.multipart_mixed_attachment()
        rendered = render_message(raw)
        md = render_message_markdown(rendered)
        if rendered.cc:
            self.assertIn("**Cc:**", md)

    def test_multiple_attachments_all_listed(self) -> None:
        raw = corpus.multipart_mixed_multi()
        rendered = render_message(raw)
        md = render_message_markdown(rendered)
        self.assertEqual(len(rendered.attachments), 2)
        for att in rendered.attachments:
            self.assertIn(att.filename, md)
