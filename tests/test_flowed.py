"""RFC 3676 ``format=flowed``, the parameter most plain-text mail carries.

Thunderbird, Apple Mail and Roundcube wrap their own paragraphs and mark
each break with a trailing space.  Ignoring that shows the sender's
line width instead of the reader's, broken mid-sentence, and makes a
quoted block look like one hard-wrapped slab.

The rules under test are §4.1 (flowed and fixed lines), §4.3 (the
signature separator), §4.4 (space-stuffing) and §4.5 (quote depth).
"""

from __future__ import annotations

import unittest

from pony.mail.flowed import unflow
from pony.mail.message_renderer import render_message


def _message(body: str, *, params: str) -> bytes:
    return (
        b"From: sender@example.test\r\n"
        b"To: you@example.test\r\n"
        b"Subject: Flowed\r\n"
        b'Content-Type: text/plain; charset="utf-8"' + params.encode() + b"\r\n"
        b"\r\n" + body.encode()
    )


class FlowedParagraphTests(unittest.TestCase):
    def test_a_flowed_line_joins_the_one_after_it(self) -> None:
        text = "Hay un error a las 12:00 (por \nestar la sala ocupada), \naunque sí.\n"
        self.assertEqual(
            unflow(text, delsp=False),
            "Hay un error a las 12:00 (por estar la sala ocupada), aunque sí.\n",
        )

    def test_delsp_decides_whether_the_marker_space_is_content(self) -> None:
        """Getting this backwards runs words together or doubles a space.

        With ``DelSp=Yes`` the trailing space is the flow marker and
        nothing else; without it the space is also a word separator.
        """
        text = "una palabra \ny otra"

        self.assertEqual(unflow(text, delsp=False), "una palabra y otra")
        self.assertEqual(unflow(text, delsp=True), "una palabray otra")

    def test_a_sender_that_wrapped_mid_word_is_rejoined_without_a_gap(self) -> None:
        """Which is what ``DelSp=Yes`` exists for."""
        text = "inter \nnacional"
        self.assertEqual(unflow(text, delsp=True), "internacional")

    def test_a_fixed_line_ends_the_paragraph(self) -> None:
        text = "primera\nsegunda\n"
        self.assertEqual(unflow(text, delsp=True), "primera\nsegunda\n")

    def test_a_blank_line_survives_as_a_blank_line(self) -> None:
        self.assertEqual(unflow("uno\n\ndos\n", delsp=True), "uno\n\ndos\n")


class SignatureSeparatorTests(unittest.TestCase):
    def test_the_separator_is_fixed_even_though_it_ends_in_a_space(self) -> None:
        """§4.3.  Joining it would hide where the message ends."""
        text = "El cuerpo.\n-- \nBeatriz\n"
        self.assertEqual(unflow(text, delsp=True), "El cuerpo.\n-- \nBeatriz\n")


class SpaceStuffingTests(unittest.TestCase):
    def test_exactly_one_leading_space_is_removed(self) -> None:
        """§4.4, and the count matters.

        A sender stuffs one space in front of a line that would
        otherwise start with a space, a ``>`` or ``From ``.  Leaving it
        indents the line for ever; removing more eats the sender's own
        alignment, which is how a right-aligned header block loses a
        column.
        """
        text = "       Fecha: hoy\nResponder-A: nadie\n"
        self.assertEqual(
            unflow(text, delsp=True), "      Fecha: hoy\nResponder-A: nadie\n"
        )

    def test_a_stuffed_quote_character_is_text_and_not_a_quote(self) -> None:
        """The stuffing is what keeps the two apart."""
        self.assertEqual(
            unflow(" > no es una cita\n", delsp=True), "> no es una cita\n"
        )


class QuoteDepthTests(unittest.TestCase):
    def test_quoted_lines_rejoin_and_keep_their_marks(self) -> None:
        text = "> Seguimos compartiendo las actividades \n> el IFF, IEM e IO.\n"
        self.assertEqual(
            unflow(text, delsp=False),
            "> Seguimos compartiendo las actividades el IFF, IEM e IO.\n",
        )

    def test_an_empty_quoted_line_keeps_its_mark_and_nothing_else(self) -> None:
        self.assertEqual(unflow("> uno\n>\n> dos\n", delsp=True), "> uno\n>\n> dos\n")

    def test_a_paragraph_never_flows_into_another_quote_depth(self) -> None:
        """A reply's own text must not merge into the text it quotes.

        The dangling marker space stays, because with ``DelSp=No`` §4.1
        makes that space content: the flow it was marking was cut short
        by the depth change, which is no reason to edit the sender's
        words.
        """
        text = "mi respuesta \n> lo que citaba\n"
        self.assertEqual(unflow(text, delsp=False), "mi respuesta \n> lo que citaba\n")

    def test_depth_is_the_run_of_marks_the_line_opens_with(self) -> None:
        text = ">> dos niveles \n>> siguen juntos\n> uno solo\n"
        self.assertEqual(
            unflow(text, delsp=False),
            ">> dos niveles siguen juntos\n> uno solo\n",
        )


class FlowedThroughTheRendererTests(unittest.TestCase):
    """The parameter is read, and nothing is guessed without it."""

    def test_a_flowed_message_is_rejoined_when_it_says_it_is_flowed(self) -> None:
        raw = _message(
            # Two spaces per break, which is what `DelSp=Yes` requires of
            # a sender that means a word gap: the last space is the flow
            # marker and is deleted, the one before it is content.  The
            # mail this was written for does the same on all 74 of its
            # flowed lines.
            "Hay un error a las 12:00 (por  \nestar la sala ocupada),  \naunque sí.\n",
            params='; format="flowed"; DelSp="Yes"',
        )
        rendered = render_message(raw)

        self.assertIn(
            "Hay un error a las 12:00 (por estar la sala ocupada), aunque sí.",
            rendered.body,
        )

    def test_a_message_that_says_nothing_is_left_exactly_as_it_came(self) -> None:
        """No parameter, no guess: hard-wrapped plain text stays wrapped.

        A sender who wrapped deliberately — a table, a code block, a
        poem — said so by not declaring ``format=flowed``.
        """
        body = "una línea que acaba en espacio \ny la siguiente\n"
        raw = _message(body, params="")
        rendered = render_message(raw)

        self.assertIn("una línea que acaba en espacio", rendered.body)
        self.assertIn("\ny la siguiente", rendered.body)

    def test_the_parameter_is_read_case_insensitively(self) -> None:
        """`Format=FLOWED; DELSP=yes` is the same declaration."""
        raw = _message("inter \nnacional\n", params='; Format="FLOWED"; DELSP="YES"')
        self.assertIn("internacional", render_message(raw).body)

    def test_delsp_absent_means_no(self) -> None:
        raw = _message("una palabra \ny otra\n", params='; format="flowed"')
        self.assertIn("una palabra y otra", render_message(raw).body)
