"""What a saved file is called.

The proposals are `pony.message_renderer`'s rather than any screen's,
which is what lets `s` in the terminal and a download in the browser
name the same message the same way.
"""

from __future__ import annotations

import unittest

import corpus

from pony.message_renderer import (
    proposed_attachment_filename,
    proposed_filename,
    render_message,
    subject_slug,
)

# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


class SubjectSlugTest(unittest.TestCase):
    def test_spaces_become_hyphens(self) -> None:
        self.assertEqual(subject_slug("Hello World"), "hello-world")

    def test_non_alphanum_stripped(self) -> None:
        self.assertEqual(subject_slug("Re: Q3 report!"), "re-q3-report")

    def test_empty_subject_returns_fallback(self) -> None:
        self.assertEqual(subject_slug(""), "message")

    def test_long_subject_truncated(self) -> None:
        slug = subject_slug("a" * 200)
        self.assertLessEqual(len(slug), 50)


class ProposedBodyFilenameTest(unittest.TestCase):
    def test_includes_date_and_slug(self) -> None:
        raw = corpus.plain_text()
        rendered = render_message(raw)
        name = proposed_filename(rendered, suffix=".md")
        self.assertTrue(name.endswith(".md"))
        self.assertIn("2026", name)

    def test_fallback_when_no_date(self) -> None:
        from pony.message_renderer import RenderedMessage

        rendered = RenderedMessage(
            subject="Test",
            from_="a@b.com",
            to="c@d.com",
            cc="",
            date="",
            body="body",
            attachments=(),
            raw_bytes=b"",
        )
        name = proposed_filename(rendered, suffix=".md")
        self.assertTrue(name.endswith(".md"))
        self.assertNotIn("_", name[:4])  # no leading date-underscore


class ProposedAttachmentFilenameTest(unittest.TestCase):
    def test_uses_given_filename(self) -> None:
        self.assertEqual(proposed_attachment_filename("report.pdf", 1), "report.pdf")

    def test_fallback_for_empty_filename(self) -> None:
        self.assertEqual(proposed_attachment_filename("", 2), "attachment-2")
