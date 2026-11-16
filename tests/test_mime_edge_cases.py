"""Projection and rendering against deliberately malformed MIME.

Every fixture here is something a real client has been observed to emit:
truncated headers, repeated headers, dates no parser accepts, bodies that
lie about their transfer encoding.  The contract under test is always the
same — degrade, never raise.  One unparseable message in a folder must not
be able to abort a sync or stop the folder from opening.

See the "malformed / hostile input" section of ``tests/corpus.py`` for the
raw shapes.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import corpus

from pony.mail.domain import MessageRef
from pony.mail.message_projection import project_rfc822_message
from pony.mail.message_renderer import (
    build_browser_html,
    extract_attachment,
    render_message,
    render_message_markdown,
)

_REF = MessageRef(account_name="acct", folder_name="INBOX", id=0)


def _project(raw: bytes):  # type: ignore[no-untyped-def]
    return project_rfc822_message(message_ref=_REF, raw_message=raw, storage_key="key")


class DegenerateHeaderTests(unittest.TestCase):
    """Header blocks that violate RFC 5322 in ways clients actually produce."""

    def test_headers_without_a_terminating_blank_line_still_project(self) -> None:
        """A truncated message yields headers and an empty body, not an error."""
        projected = _project(corpus.no_header_body_separator())

        self.assertEqual(projected.subject, "Truncated before the body")
        self.assertIn("alice@example.com", projected.sender)
        self.assertEqual(projected.body_text, "")

    def test_the_first_occurrence_of_a_repeated_header_wins(self) -> None:
        """Duplicate headers must not let a later value override the first."""
        projected = _project(corpus.duplicate_headers())

        self.assertEqual(projected.subject, "First subject wins")
        self.assertIn("alice@example.com", projected.sender)
        self.assertNotIn("mallory@example.com", projected.sender)

    def test_an_unparseable_date_falls_back_to_now(self) -> None:
        """Rather than drop the message, an unreadable Date becomes now()."""
        before = datetime.now(tz=UTC)
        projected = _project(corpus.invalid_date())
        after = datetime.now(tz=UTC)

        self.assertGreaterEqual(projected.received_at, before)
        self.assertLessEqual(projected.received_at, after)

    def test_a_date_without_an_offset_is_read_as_utc(self) -> None:
        """A naive timestamp keeps its wall-clock value and gains UTC."""
        projected = _project(corpus.naive_date())

        self.assertEqual(projected.received_at.tzinfo, UTC)
        self.assertEqual(
            projected.received_at,
            datetime(2026, 4, 17, 12, 0, tzinfo=UTC),
        )


class DegenerateBodyTests(unittest.TestCase):
    """Bodies that misdeclare their encoding or exceed the preview budget."""

    def test_a_body_that_lies_about_being_base64_is_kept_verbatim(self) -> None:
        projected = _project(corpus.corrupt_base64_body())

        self.assertIn("not base64", projected.body_text)

    def test_an_oversized_body_is_truncated_to_the_preview_cap(self) -> None:
        """The cap is a byte budget, and the result must stay valid UTF-8."""
        raw = corpus.oversized_body()
        projected = _project(raw)

        self.assertEqual(len(projected.body_text), 256 * 1024)
        self.assertLess(len(projected.body_text), len(raw))
        projected.body_text.encode("utf-8")  # must not raise

    def test_an_html_only_multipart_previews_the_stripped_html(self) -> None:
        """With no text/plain part the preview falls back to stripped HTML."""
        projected = _project(corpus.html_first_multipart())

        self.assertEqual(projected.body_text, "Hello there")
        self.assertTrue(projected.has_attachments)


class RichHtmlRenderingTests(unittest.TestCase):
    """The HTML-to-text stripper across every block and inline construct."""

    def test_list_items_and_table_rows_each_end_a_line(self) -> None:
        rendered = render_message(corpus.html_rich_formatting())

        self.assertIn("First item\nSecond item", rendered.body)
        self.assertIn("Cell C", rendered.body)

    def test_a_paragraph_boundary_inserts_exactly_one_blank_line(self) -> None:
        """``<br><br>`` and ``</ul>`` both collapse to a single separator."""
        rendered = render_message(corpus.html_rich_formatting())

        self.assertIn("Line one\n\nLine three", rendered.body)
        self.assertNotIn("\n\n\n", rendered.body)

    def test_nested_bold_tags_produce_one_span_not_two(self) -> None:
        """Depth tracking means the inner ``<b>`` must not re-open the span."""
        rendered = render_message(corpus.html_rich_formatting())

        self.assertIn("bold deeper back", rendered.body)
        self.assertIn("italic", rendered.body)

    def test_the_markdown_export_carries_every_present_header(self) -> None:
        markdown = render_message_markdown(render_message(corpus.plain_text()))

        for label in ("**From:**", "**To:**", "**Cc:**", "**Subject:**", "**Date:**"):
            self.assertIn(label, markdown)

    def test_the_markdown_export_omits_headers_that_are_absent(self) -> None:
        """A message with no Cc must not emit an empty ``**Cc:**`` line."""
        markdown = render_message_markdown(
            render_message(corpus.html_rich_formatting())
        )

        self.assertIn("**From:**", markdown)
        self.assertNotIn("**Cc:**", markdown)


class AwkwardAttachmentTests(unittest.TestCase):
    """Attachment shapes that do not follow the common layout."""

    def test_a_base64_encoded_forward_is_offered_as_an_eml(self) -> None:
        raw = corpus.base64_rfc822_attachment()
        rendered = render_message(raw)

        self.assertEqual(len(rendered.attachments), 1)
        self.assertTrue(rendered.attachments[0].filename.endswith(".eml"))
        self.assertEqual(rendered.attachments[0].content_type, "message/rfc822")

        payload = extract_attachment(raw, 1)
        assert payload is not None
        self.assertTrue(payload.filename.endswith(".eml"))
        self.assertGreater(payload.size_bytes, 0)

    def test_a_message_with_no_text_part_still_lists_its_attachment(self) -> None:
        raw = corpus.attachment_only()
        rendered = render_message(raw)

        self.assertEqual(len(rendered.attachments), 1)
        self.assertEqual(rendered.attachments[0].filename, "thing.bin")

        html = build_browser_html(raw)
        self.assertIn("(no readable content)", html)
        self.assertIn("thing.bin", html)

    def test_an_html_first_multipart_keeps_its_attachment_indexed(self) -> None:
        raw = corpus.html_first_multipart()
        rendered = render_message(raw)

        self.assertEqual(len(rendered.attachments), 1)
        self.assertEqual(rendered.attachments[0].filename, "payload.bin")

        payload = extract_attachment(raw, 1)
        assert payload is not None
        self.assertEqual(payload.filename, "payload.bin")

    def test_indexes_past_the_end_return_nothing(self) -> None:
        for raw in (
            corpus.attachment_only(),
            corpus.base64_rfc822_attachment(),
            corpus.html_first_multipart(),
        ):
            self.assertIsNone(extract_attachment(raw, 99))
            self.assertIsNone(extract_attachment(raw, 0))


class MalformedMessagesSurviveRenderingTests(unittest.TestCase):
    """Every hostile fixture must render without raising."""

    def test_no_fixture_raises_through_the_render_pipeline(self) -> None:
        fixtures = (
            corpus.no_header_body_separator(),
            corpus.duplicate_headers(),
            corpus.invalid_date(),
            corpus.naive_date(),
            corpus.corrupt_base64_body(),
            corpus.attachment_only(),
            corpus.html_first_multipart(),
            corpus.html_rich_formatting(),
            corpus.base64_rfc822_attachment(),
        )
        for raw in fixtures:
            with self.subTest(raw=raw[:60]):
                rendered = render_message(raw)
                render_message_markdown(rendered)
                build_browser_html(raw)
                _project(raw)


_THE_ALTERNATIVE = "<p>The real HTML alternative, with a <b>bold</b> word.</p>"


def _plain_part_message(body: str, *, html: str | None = _THE_ALTERNATIVE) -> bytes:
    """A message whose `text/plain` part holds *body*.

    With *html*, a `multipart/alternative` carrying that as the other
    half; with ``html=None``, a single `text/plain` part and no
    alternative to fall back to.
    """
    headers = (
        b"From: sender@example.test\r\nTo: you@example.test\r\nSubject: Election\r\n"
    )
    if html is None:
        return (
            headers + b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n" + body.encode() + b"\r\n"
        )
    return (
        headers + b'Content-Type: multipart/alternative; boundary="bnd"\r\n'
        b"\r\n"
        b"--bnd\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n" + body.encode() + b"\r\n"
        b"--bnd\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n" + html.encode() + b"\r\n"
        b"--bnd--\r\n"
    )


class PlainTextThatIsReallyHtmlTests(unittest.TestCase):
    """A `text/plain` part whose content is HTML.

    Observed in the wild: one generator fills both alternatives of a
    `multipart/alternative` and labels the first wrongly.  Pony took the
    header at its word and showed the reader raw `<div>`s -- and because
    a plain part suppresses collecting the HTML one, there was no
    alternative left to fall back to either.  What the part *is* decides
    how it is read; the header only says what the sender believed.
    """

    def test_a_mislabelled_part_hands_over_to_the_html_alternative(self) -> None:
        """The correctly-labelled half is preferred once the lie shows.

        The plain part gives the message away, but it is the part
        labelled `text/html` that gets read: that one is HTML by
        agreement rather than by guess.
        """
        raw = _plain_part_message(
            "<div>Dear member:&nbsp;</div><div>&nbsp;</div>"
            "<div>Vote <a href='https://ballot.test/x'>here</a>.</div><br/>"
        )
        rendered = render_message(raw)

        self.assertNotIn("<div>", rendered.body)
        self.assertNotIn("&nbsp;", rendered.body)
        self.assertIn("The real HTML alternative", rendered.body)
        self.assertIn("bold", rendered.body)

    def test_a_mislabelled_part_with_no_alternative_is_read_as_html(self) -> None:
        """With nothing to hand over to, the part is read against its label.

        A single `text/plain` part carrying HTML has no second copy of
        itself, so the choice is between rendering it and showing the
        reader raw tags.
        """
        raw = _plain_part_message(
            "<div>Dear member:&nbsp;</div><div>&nbsp;</div>"
            "<div>Vote <a href='https://ballot.test/x'>here</a>.</div><br/>",
            html=None,
        )
        rendered = render_message(raw)

        self.assertNotIn("<div>", rendered.body)
        self.assertNotIn("&nbsp;", rendered.body)
        self.assertIn("Dear member:", rendered.body)
        # And its links are found, which raw tags had hidden.
        self.assertEqual(rendered.links, (("web", "https://ballot.test/x"),))

    def test_an_ordinary_plain_body_is_left_alone(self) -> None:
        raw = _plain_part_message(
            "Compare a < b and b > c.\nMy home page is <https://example.test/~me>.\n"
        )
        rendered = render_message(raw)

        self.assertIn("a < b", rendered.body)
        self.assertIn("b > c", rendered.body)
        self.assertEqual(rendered.links, (("web", "https://example.test/~me"),))

    def test_one_stray_tag_does_not_reclassify_a_message(self) -> None:
        """Two pieces of markup are wanted, not one.

        A signature with a single `<br>` in it is still a plain-text
        message, and stripping it would eat anything else in angle
        brackets.
        """
        raw = _plain_part_message("Thanks,<br>\nAlex\nThe tag <foo> stays.\n")
        rendered = render_message(raw)

        self.assertIn("<br>", rendered.body)
        self.assertIn("<foo>", rendered.body)

    def test_misjudging_a_plain_body_costs_nothing_when_an_alternative_exists(
        self,
    ) -> None:
        """The heuristic's false positives are absorbed by the hand-over.

        A plain-text message that *quotes* two tags of markup is judged
        HTML by this rule.  Where the sender supplied a `text/html`
        half, that is simply what gets read -- the same words the
        message was always going to show -- so a wrong guess has no
        victim.
        """
        raw = _plain_part_message("Try <div>one</div> then <span>two</span>.\n")
        rendered = render_message(raw)

        self.assertIn("The real HTML alternative", rendered.body)

    def test_misjudging_a_plain_body_with_no_alternative_renders_the_markup(
        self,
    ) -> None:
        """The one case the heuristic can actually get wrong.

        Someone mailing a snippet of markup as plain text, with no HTML
        half, has the snippet rendered rather than shown.  That is the
        price of rescuing the mislabelled messages, and it is the lesser
        harm: the words stay readable, where the old behaviour made
        every mislabelled message unreadable entirely.
        """
        raw = _plain_part_message(
            "Try <div>one</div> then <span>two</span>.\n", html=None
        )
        rendered = render_message(raw)

        self.assertNotIn("<div>", rendered.body)
        self.assertIn("one", rendered.body)
        self.assertIn("two", rendered.body)
