"""HTML pasted into a plain-text body, read as HTML only where it sits.

The message this was written for is 95 lines of prose and quoted reply
followed by 30 lines of institutional signature markup.  Judging the
whole message one way or the other is wrong about one half of it: as
HTML throughout, 235 newlines and 75 quote markers collapse into a
single paragraph; as text throughout, the reader gets raw tags and 26 kB
of base64.

So each island is judged on its own, and the tests below are mostly
about the one hard question — telling markup apart from prose that
*names* tags.  Counting cannot do it: ``Use the <table> element, not
<div> soup.`` carries as many tags as ``<span>Madrid</span>``.
"""

from __future__ import annotations

import unittest

from pony.mail.html_islands import island_spans, looks_like_markup, strip_html_islands
from pony.mail.message_renderer import _strip_html


def _applied(text: str) -> str:
    return strip_html_islands(text, _strip_html, [])


class MarkupIsFoundTests(unittest.TestCase):
    def test_a_closed_tag_is_markup(self) -> None:
        self.assertEqual(_applied("Office: <span>Madrid</span>"), "Office: Madrid")

    def test_a_stray_closing_tag_is_markup(self) -> None:
        """Prose has no reason to carry one."""
        self.assertTrue(looks_like_markup("<!--[if mso]></span><![endif]-->"))

    def test_a_tag_with_attributes_is_markup(self) -> None:
        """Prose says `<table>`; a generator says `<table border="0">`."""
        self.assertTrue(looks_like_markup('<img src="x.png" alt="CSIC">'))

    def test_a_comment_is_markup(self) -> None:
        self.assertTrue(looks_like_markup("<!-- a layout note -->"))

    def test_a_pasted_signature_becomes_its_text(self) -> None:
        text = (
            "Saludos,\n\n"
            '<a href="https://csic.es"><img src="l.png" alt="CSIC"></a><br>\n'
            "<span>Madrid</span>"
        )
        links: list[tuple[str, str]] = []
        result = strip_html_islands(text, _strip_html, links)

        self.assertTrue(result.startswith("Saludos,\n\n"))
        self.assertIn("Madrid", result)
        self.assertNotIn("<span>", result)
        self.assertEqual(links, [("web", "https://csic.es")])


class ProseIsNotMarkupTests(unittest.TestCase):
    """What a count-based test gets wrong, and why this one does not."""

    def test_a_sentence_naming_tags_is_left_exactly_alone(self) -> None:
        """Two tags, neither closed: someone writing about HTML.

        This is the case that defeats every threshold.  It has as many
        tags as a real fragment, so only whether the tags are *used* --
        closed, or carrying attributes -- separates them.
        """
        text = "Use the <table> element for layout, not <div> soup."
        self.assertEqual(island_spans(text), [])
        self.assertEqual(_applied(text), text)

    def test_a_lone_unclosed_tag_is_left_alone(self) -> None:
        text = "Thanks,<br>\nAlex"
        self.assertEqual(_applied(text), text)

    def test_an_angle_bracketed_address_is_not_a_tag(self) -> None:
        """The reason the scanner knows tag names at all.

        Plain mail is full of these, and a header block quoting one has
        to survive as text -- it is how the forwarded message in the
        mail this was written for announces its sender.
        """
        text = "--- Mensaje de COMUNICACIONES IQF <comunicacion@iqf.csic.es> ---"
        self.assertEqual(_applied(text), text)

    def test_arithmetic_and_bracketed_urls_survive(self) -> None:
        text = "If a < b and b > c, see <https://example.test/x>."
        self.assertEqual(_applied(text), text)

    def test_a_quoted_reply_keeps_its_markers_and_its_lines(self) -> None:
        text = "> Buenos días,\n>\n> Seguimos compartiendo\n"
        self.assertEqual(_applied(text), text)


class IslandBoundaryTests(unittest.TestCase):
    def test_a_blank_line_ends_an_island(self) -> None:
        """Otherwise a signature's island reaches back over the prose."""
        text = '<p class="x">uno</p>\n\nprosa entre islas\n\n<p class="y">dos</p>'
        self.assertEqual(len(island_spans(text)), 2)
        self.assertIn("prosa entre islas", _applied(text))

    def test_text_outside_an_island_is_not_normalised(self) -> None:
        """The whole point: no whitespace collapsing outside the markup.

        Reading the message as HTML throughout is what turned 235
        newlines into 20.
        """
        text = "uno\ndos\ntres\n\n<span>markup</span>\n\ncuatro\ncinco\n"
        result = _applied(text)

        self.assertIn("uno\ndos\ntres", result)
        self.assertIn("cuatro\ncinco", result)
        self.assertNotIn("<span>", result)

    def test_a_part_that_is_markup_throughout_is_one_island(self) -> None:
        text = "<div>uno</div><div>dos</div><div>tres</div>"
        self.assertEqual(len(island_spans(text)), 1)

    def test_links_are_numbered_across_islands(self) -> None:
        """Each island is stripped on its own and numbers from zero.

        Without renumbering, the second island's first link would claim
        the first island's target.
        """
        text = (
            '<a href="https://one.test">uno</a>\n\n'
            "prosa\n\n"
            '<a href="https://two.test">dos</a>'
        )
        links: list[tuple[str, str]] = []
        result = strip_html_islands(text, _strip_html, links)

        self.assertEqual(
            links, [("web", "https://one.test"), ("web", "https://two.test")]
        )
        self.assertIn("\x00LINK:0:uno\x00", result)
        self.assertIn("\x00LINK:1:dos\x00", result)


class ScanCostTests(unittest.TestCase):
    def test_prose_with_no_markup_is_scanned_without_backtracking(self) -> None:
        """A megabyte of ordinary prose, to pin the linear behaviour.

        The token pattern is one alternation over known tag names, so
        the engine's literal prefilter skips anything with no ``<`` in
        it; a body that is all prose costs effectively nothing.  This
        test is here to fail loudly if the pattern ever grows a
        construct that backtracks.
        """
        text = "Hay un error, no habrá actividad en el Observatorio. " * 20000
        self.assertEqual(island_spans(text), [])
