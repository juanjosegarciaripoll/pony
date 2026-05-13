"""Tests for link extraction and sentinel injection in ``pony.tui.message_renderer``."""

from __future__ import annotations

import unittest
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from textual.markup import _to_content

from pony.tui.message_renderer import _inject_plaintext_links, render_message
from pony.tui.widgets.message_view import _render_body


def _plain_msg(body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "test"
    msg["Date"] = "Fri, 11 Apr 2026 10:00:00 +0000"
    msg.set_content(body)
    return msg.as_bytes()


def _html_msg(html: str, plain: str = "") -> bytes:
    outer = MIMEMultipart("alternative")
    outer["From"] = "alice@example.com"
    outer["To"] = "bob@example.com"
    outer["Subject"] = "test"
    outer["Date"] = "Fri, 11 Apr 2026 10:00:00 +0000"
    if plain:
        outer.attach(MIMEText(plain, "plain"))
    outer.attach(MIMEText(html, "html"))
    return outer.as_bytes()


class HTMLAnchorExtractionTest(unittest.TestCase):
    def test_human_anchor_text_preserved(self) -> None:
        html = "<p>Click <a href='https://example.com'>here</a> for info.</p>"
        r = render_message(_html_msg(html))
        self.assertEqual(r.links, (("web", "https://example.com"),))
        self.assertIn("here", r.body)
        self.assertIn("\x00LINK:0\x00", r.body)
        # Anchor text should precede the sentinel
        pos_text = r.body.index("here")
        pos_sentinel = r.body.index("\x00LINK:0\x00")
        self.assertLess(pos_text, pos_sentinel)

    def test_url_as_anchor_text_replaced(self) -> None:
        html = "<p><a href='https://example.com'>https://example.com</a></p>"
        r = render_message(_html_msg(html))
        self.assertEqual(r.links, (("web", "https://example.com"),))
        # URL text should be replaced, only sentinel present
        self.assertNotIn("https://example.com", r.body)
        self.assertIn("\x00LINK:0\x00", r.body)

    def test_empty_anchor_text(self) -> None:
        html = "<p><a href='https://x.org'></a></p>"
        r = render_message(_html_msg(html))
        self.assertEqual(r.links, (("web", "https://x.org"),))
        self.assertIn("\x00LINK:0\x00", r.body)

    def test_mailto_anchor_human_text(self) -> None:
        html = "<p>Mail <a href='mailto:foo@bar.com'>Foo</a>.</p>"
        r = render_message(_html_msg(html))
        self.assertEqual(r.links, (("mail", "foo@bar.com"),))
        self.assertIn("Foo", r.body)
        self.assertIn("\x00LINK:0\x00", r.body)

    def test_mailto_anchor_address_as_text(self) -> None:
        html = "<p><a href='mailto:foo@bar.com'>foo@bar.com</a></p>"
        r = render_message(_html_msg(html))
        self.assertEqual(r.links, (("mail", "foo@bar.com"),))
        # Anchor text equals the target address — treated as redundant, sentinel only
        self.assertNotIn("foo@bar.com", r.body)
        self.assertIn("\x00LINK:0\x00", r.body)

    def test_multiple_links(self) -> None:
        html = "<p><a href='https://a.com'>A</a> and <a href='https://b.com'>B</a></p>"
        r = render_message(_html_msg(html))
        self.assertEqual(len(r.links), 2)
        self.assertEqual(r.links[0], ("web", "https://a.com"))
        self.assertEqual(r.links[1], ("web", "https://b.com"))

    def test_non_http_anchor_ignored(self) -> None:
        html = "<p><a href='ftp://old.server/file'>download</a></p>"
        r = render_message(_html_msg(html))
        self.assertEqual(r.links, ())
        self.assertIn("download", r.body)

    def test_mailto_query_params_stripped(self) -> None:
        html = "<p><a href='mailto:foo@bar.com?subject=Hi'>Mail</a></p>"
        r = render_message(_html_msg(html))
        self.assertEqual(r.links, (("mail", "foo@bar.com"),))


class PlainTextLinkInjectionTest(unittest.TestCase):
    def test_bare_https_url(self) -> None:
        links: list[tuple[str, str]] = []
        result = _inject_plaintext_links("Visit https://example.com today.", links)
        self.assertEqual(links, [("web", "https://example.com")])
        self.assertIn("\x00LINK:0\x00", result)
        self.assertNotIn("https://example.com", result)

    def test_bare_http_url(self) -> None:
        links: list[tuple[str, str]] = []
        result = _inject_plaintext_links("See http://old.site/page for more.", links)
        self.assertEqual(links, [("web", "http://old.site/page")])
        self.assertIn("\x00LINK:0\x00", result)

    def test_angle_bracketed_https(self) -> None:
        links: list[tuple[str, str]] = []
        result = _inject_plaintext_links("Ref: <https://example.com>.", links)
        self.assertEqual(links, [("web", "https://example.com")])
        self.assertIn("\x00LINK:0\x00", result)
        self.assertNotIn("<https://example.com>", result)

    def test_angle_bracketed_mailto(self) -> None:
        links: list[tuple[str, str]] = []
        result = _inject_plaintext_links("Write to <mailto:foo@bar.com>.", links)
        self.assertEqual(links, [("mail", "foo@bar.com")])
        self.assertIn("\x00LINK:0\x00", result)

    def test_bare_mailto(self) -> None:
        links: list[tuple[str, str]] = []
        result = _inject_plaintext_links("Email mailto:foo@bar.com here.", links)
        self.assertEqual(links, [("mail", "foo@bar.com")])
        self.assertIn("\x00LINK:0\x00", result)

    def test_no_links_unchanged(self) -> None:
        links: list[tuple[str, str]] = []
        result = _inject_plaintext_links("Plain body with no links.", links)
        self.assertEqual(links, [])
        self.assertEqual(result, "Plain body with no links.")

    def test_multiple_urls(self) -> None:
        links: list[tuple[str, str]] = []
        text = "See https://a.com and https://b.com."
        result = _inject_plaintext_links(text, links)
        self.assertEqual(len(links), 2)
        self.assertIn("\x00LINK:0\x00", result)
        self.assertIn("\x00LINK:1\x00", result)

    def test_preexisting_sentinels_not_mangled(self) -> None:
        links: list[tuple[str, str]] = [("web", "https://existing.com")]
        text = "Pre: \x00LINK:0\x00 then https://new.com end."
        result = _inject_plaintext_links(text, links)
        self.assertEqual(len(links), 2)
        self.assertIn("\x00LINK:0\x00", result)
        self.assertIn("\x00LINK:1\x00", result)


class RenderMessageLinksTest(unittest.TestCase):
    def test_plain_text_message_captures_links(self) -> None:
        body = "Go to https://docs.example.com for details."
        r = render_message(_plain_msg(body))
        self.assertEqual(r.links, (("web", "https://docs.example.com"),))
        self.assertIn("\x00LINK:0\x00", r.body)

    def test_plain_text_no_links(self) -> None:
        r = render_message(_plain_msg("Just text, no links."))
        self.assertEqual(r.links, ())
        self.assertNotIn("\x00", r.body)

    def test_html_only_message(self) -> None:
        html = "<p><a href='https://x.com'>X</a></p>"
        r = render_message(_html_msg(html))
        self.assertIn(("web", "https://x.com"), r.links)


class RenderBodyMarkupTest(unittest.TestCase):
    """Verify that _render_body produces valid Textual markup in edge cases."""

    def _assert_valid_markup(self, body: str, links: tuple[tuple[str, str], ...]) -> str:
        markup = _render_body(body, links)
        # _to_content raises MarkupError if the markup is unbalanced
        _to_content(markup)
        return markup

    def test_plain_text_no_links(self) -> None:
        self._assert_valid_markup("Hello world.", ())

    def test_web_link(self) -> None:
        body = "Visit \x00LINK:0\x00 for info."
        markup = self._assert_valid_markup(body, (("web", "https://x.com"),))
        self.assertIn("activate_link", markup)

    def test_mail_link(self) -> None:
        body = "Contact \x00LINK:0\x00 for help."
        markup = self._assert_valid_markup(body, (("mail", "user@example.com"),))
        self.assertIn("compose_link", markup)

    def test_bracket_before_link_sentinel(self) -> None:
        # Regression: HTML email with [<a href="mailto:...">addr</a>] produces
        # a '[' segment immediately before the link markup.  Rich 14.x does not
        # escape a bare '[' that isn't followed by a tag-like pattern, so the
        # markup parser used to see '[[@click=...]...[/]]' — the outer '[' ate
        # the [@click] tag, leaving [/] with nothing to close.
        body = "Reply to [\x00LINK:0\x00]"
        self._assert_valid_markup(body, (("mail", "user@example.com"),))

    def test_bracket_after_link_sentinel(self) -> None:
        body = "\x00LINK:0\x00] trailing bracket"
        self._assert_valid_markup(body, (("web", "https://x.com"),))

    def test_brackets_around_multiple_links(self) -> None:
        body = "See [\x00LINK:0\x00] and [\x00LINK:1\x00]."
        self._assert_valid_markup(
            body, (("web", "https://a.com"), ("mail", "b@example.com"))
        )

    def test_square_brackets_in_plain_text_rendered_as_literal(self) -> None:
        body = "Reference [1] and [SPAM] tag."
        markup = self._assert_valid_markup(body, ())
        content = str(_to_content(markup))
        self.assertIn("[1]", content)
        self.assertIn("[SPAM]", content)
