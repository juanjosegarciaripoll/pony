"""Tests for the shared HTML sanitization helpers."""

from __future__ import annotations

import unittest

from pony.html_sanitize import html_to_preview_text, strip_invisible_blocks


class StripInvisibleBlocksTest(unittest.TestCase):
    """``strip_invisible_blocks`` removes non-visible elements."""

    def test_removes_style_block(self) -> None:
        html = "<p>Hi</p><style>.x{color:red}</style><p>Bye</p>"
        result = strip_invisible_blocks(html)
        self.assertNotIn("color:red", result)
        self.assertNotIn(".x{", result)
        self.assertIn("Hi", result)
        self.assertIn("Bye", result)

    def test_removes_script_block(self) -> None:
        html = "<p>Hi</p><script>var x = 1;</script>"
        self.assertNotIn("var x", strip_invisible_blocks(html))

    def test_removes_noscript_block(self) -> None:
        html = "<noscript>Please enable JavaScript</noscript><p>Hi</p>"
        result = strip_invisible_blocks(html)
        self.assertNotIn("JavaScript", result)
        self.assertIn("Hi", result)

    def test_removes_head_block(self) -> None:
        html = (
            "<html><head><title>X</title><meta charset='utf-8'></head>"
            "<body><p>Body</p></body></html>"
        )
        result = strip_invisible_blocks(html)
        self.assertNotIn("<title>", result)
        self.assertNotIn("charset", result)
        self.assertIn("Body", result)

    def test_removes_html_comment(self) -> None:
        html = "<p>Hi</p><!-- secret note --><p>Bye</p>"
        result = strip_invisible_blocks(html)
        self.assertNotIn("secret", result)

    def test_removes_outlook_conditional_comment(self) -> None:
        # Conditional comments embed > inside the opening delimiter;
        # a naive tag regex would leak their CSS payload as visible text.
        html = (
            "<p>Before</p>"
            "<!--[if mso]>"
            "<style>.mso-class{color:red}</style>"
            "<![endif]-->"
            "<p>After</p>"
        )
        result = strip_invisible_blocks(html)
        self.assertNotIn("mso-class", result)
        self.assertNotIn("color:red", result)
        self.assertIn("Before", result)
        self.assertIn("After", result)

    def test_multiline_style_block(self) -> None:
        html = "<style>\n.a { color: red }\n.b { color: blue }\n</style><p>x</p>"
        result = strip_invisible_blocks(html)
        self.assertNotIn("color", result)
        self.assertIn("x", result)

    def test_case_insensitive_tags(self) -> None:
        html = "<STYLE>.x{color:red}</STYLE><P>Hi</P>"
        result = strip_invisible_blocks(html)
        self.assertNotIn("color", result)

    def test_preserves_visible_tags(self) -> None:
        # Non-invisible tags (p, div, a, etc.) must be kept for downstream
        # parsers that rely on structural markup.
        html = "<p>Hi</p><div>there</div>"
        result = strip_invisible_blocks(html)
        self.assertIn("<p>", result)
        self.assertIn("<div>", result)


class HtmlToPreviewTextTest(unittest.TestCase):
    """``html_to_preview_text`` produces a flat plain-text preview."""

    def test_basic_paragraph(self) -> None:
        self.assertEqual(
            html_to_preview_text("<p>Hello world</p>"),
            "Hello world",
        )

    def test_style_content_stripped(self) -> None:
        out = html_to_preview_text(
            "<style>.x{color:red}</style><p>Hi</p>"
        )
        self.assertEqual(out, "Hi")

    def test_conditional_comment_stripped(self) -> None:
        out = html_to_preview_text(
            "<!--[if mso]><style>.x{color:red}</style><![endif]--><p>Hi</p>"
        )
        self.assertEqual(out, "Hi")

    def test_entities_decoded(self) -> None:
        out = html_to_preview_text("<p>AT&amp;T &mdash; it&#8217;s &nbsp;fine</p>")
        self.assertIn("AT&T", out)
        self.assertIn("—", out)
        self.assertIn("it\u2019s", out)
        self.assertNotIn("&amp;", out)
        self.assertNotIn("&#8217;", out)

    def test_whitespace_collapsed(self) -> None:
        out = html_to_preview_text(
            "<p>one</p>   <p>two</p>\n\n<p>three</p>"
        )
        self.assertEqual(out, "one two three")

    def test_empty_input(self) -> None:
        self.assertEqual(html_to_preview_text(""), "")

    def test_tag_only_input(self) -> None:
        self.assertEqual(html_to_preview_text("<br><hr/>"), "")


if __name__ == "__main__":
    unittest.main()
