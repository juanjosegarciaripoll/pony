"""The parts of a message, and what saving them reports.

One numbering serves both front ends: ``0`` is the message as it
arrived, ``T`` is its body as text, and ``1..N`` are its attachments.
The reader's ``s`` is part 0, its ``S`` prompt takes any of them, and
the browser offers the same set — so the rules belong to
:mod:`pony.mail.message_export` rather than to either screen.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import corpus

from pony.mail.message_export import (
    TEXT_BODY,
    WHOLE_MESSAGE,
    listed_parts,
    message_parts,
    part_key,
    part_payload,
    save_parts,
)
from pony.mail.message_renderer import render_message
from pony.paths import contain


class PartKeyTest(unittest.TestCase):
    """The one parser the prompt and the accelerators share."""

    def test_a_digit_is_its_number(self) -> None:
        self.assertEqual(part_key("0"), WHOLE_MESSAGE)
        self.assertEqual(part_key("3"), 3)

    def test_the_text_body_is_named_in_either_case(self) -> None:
        self.assertEqual(part_key("T"), TEXT_BODY)
        self.assertEqual(part_key("t"), TEXT_BODY)

    def test_anything_else_names_nothing(self) -> None:
        self.assertIsNone(part_key("x"))
        self.assertIsNone(part_key(""))


class MessagePartsTest(unittest.TestCase):
    def test_a_plain_message_still_has_two_parts(self) -> None:
        """Itself and its text: ``S`` has something to offer on anything."""
        parts = message_parts(render_message(corpus.plain_text()))

        self.assertEqual([part.key for part in parts], [WHOLE_MESSAGE, TEXT_BODY])
        self.assertTrue(parts[0].filename.endswith(".eml"))
        self.assertTrue(parts[1].filename.endswith(".txt"))
        self.assertEqual(parts[1].content_type, "text/plain")

    def test_attachments_keep_the_numbers_the_reader_shows(self) -> None:
        parts = message_parts(render_message(corpus.multipart_mixed_attachment()))

        self.assertEqual([part.key for part in parts], [WHOLE_MESSAGE, TEXT_BODY, 1])
        self.assertEqual(parts[2].filename, "q1-report.pdf")

    def test_only_the_attachments_are_listed(self) -> None:
        """What a reader sees: its files, not the message it has open.

        0 and T stay in ``message_parts`` -- the prompt, the digit
        accelerators and ``n=0`` all name them -- and out of the list,
        where they said nothing on every message that had a file.
        """
        rendered = render_message(corpus.multipart_mixed_attachment())

        listed = listed_parts(rendered)

        self.assertEqual([part.key for part in listed], [1])
        self.assertEqual(listed[0].filename, "q1-report.pdf")

    def test_a_message_with_no_attachments_lists_nothing(self) -> None:
        rendered = render_message(corpus.plain_text())

        self.assertEqual(listed_parts(rendered), ())
        self.assertEqual(
            [part.key for part in message_parts(rendered)],
            [WHOLE_MESSAGE, TEXT_BODY],
        )


class PartPayloadTest(unittest.TestCase):
    def test_part_zero_is_the_bytes_that_arrived(self) -> None:
        raw = corpus.plain_text()
        payload = part_payload(render_message(raw), WHOLE_MESSAGE)

        assert payload is not None
        self.assertEqual(payload.data, raw)
        self.assertEqual(payload.content_type, "message/rfc822")

    def test_part_t_carries_no_html(self) -> None:
        """An HTML-only message still yields text, which is the point."""
        rendered = render_message(corpus.html_only())
        payload = part_payload(rendered, TEXT_BODY)

        assert payload is not None
        text = payload.data.decode("utf-8")
        self.assertNotIn("<", text)
        self.assertEqual(text, rendered.body)

    def test_a_part_that_is_not_there(self) -> None:
        rendered = render_message(corpus.plain_text())

        self.assertIsNone(part_payload(rendered, 1))
        self.assertIsNone(part_payload(rendered, "nonsense"))


class SavePartsTest(unittest.TestCase):
    def test_it_names_every_file_it_wrote(self) -> None:
        rendered = render_message(corpus.multipart_mixed_attachment())
        with TemporaryDirectory() as tmp:
            dest = Path(tmp)
            outcome = save_parts(rendered, [WHOLE_MESSAGE, TEXT_BODY, 1], dest)

        self.assertEqual(outcome.changed, 3)
        said = " ".join(notice.text for notice in outcome.notices)
        self.assertIn("q1-report.pdf", said)
        self.assertIn(".eml", said)
        self.assertIn(".txt", said)
        self.assertIn(str(dest), said)

    def test_a_second_save_does_not_overwrite_the_first(self) -> None:
        rendered = render_message(corpus.multipart_mixed_attachment())
        with TemporaryDirectory() as tmp:
            dest = Path(tmp)
            save_parts(rendered, [1], dest)
            outcome = save_parts(rendered, [1], dest)
            written = sorted(path.name for path in dest.iterdir())

        self.assertEqual(written, ["q1-report-1.pdf", "q1-report.pdf"])
        self.assertIn("q1-report-1.pdf", outcome.notices[0].text)

    def test_a_missing_part_is_a_warning_and_not_a_failure(self) -> None:
        rendered = render_message(corpus.plain_text())
        with TemporaryDirectory() as tmp:
            outcome = save_parts(rendered, [WHOLE_MESSAGE, 9], Path(tmp))

        self.assertEqual(outcome.changed, 1)
        self.assertFalse(outcome.failed)
        self.assertEqual(outcome.notices[-1].severity, "warning")
        self.assertIn("No part 9", outcome.notices[-1].text)

    def test_choosing_nothing_is_refused(self) -> None:
        rendered = render_message(corpus.plain_text())
        with TemporaryDirectory() as tmp:
            outcome = save_parts(rendered, [], Path(tmp))

        self.assertFalse(outcome.acted)
        self.assertEqual(outcome.notices[0].severity, "warning")

    def test_an_unusable_destination_is_reported_once(self) -> None:
        """A file where the directory should be: one error, nothing written."""
        rendered = render_message(corpus.plain_text())
        with TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "not-a-directory"
            blocked.write_bytes(b"")
            outcome = save_parts(rendered, [WHOLE_MESSAGE], blocked)

        self.assertTrue(outcome.failed)
        self.assertEqual(len(outcome.notices), 1)

    def test_a_part_that_cannot_be_written_leaves_the_others_alone(self) -> None:
        rendered = render_message(corpus.multipart_mixed_attachment())
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "closed"
            dest.mkdir()
            dest.chmod(0o500)
            try:
                outcome = save_parts(rendered, [WHOLE_MESSAGE, 1], dest)
            finally:
                dest.chmod(0o700)

        self.assertEqual(outcome.changed, 0)
        self.assertTrue(outcome.failed)
        self.assertEqual(len(outcome.notices), 2)


class OpeningPartsTest(unittest.TestCase):
    """Opening a part is the same operation as saving one, plus a viewer.

    Where the bytes go, which viewer a content type gets and that a
    message goes to the reader's own viewer rather than to whatever the
    desktop opens an ``.eml`` with were all decided inside the reader
    screen, so a second front end had nothing to reuse.  What stays with
    a front end is the launching: an external viewer writes to the same
    terminal, so the terminal has to suspend itself around it.
    """

    def test_an_attachment_is_written_and_given_its_viewer(self) -> None:
        from pony.mail.domain import ViewerRule
        from pony.mail.message_export import parts_to_open

        rendered = render_message(corpus.multipart_mixed_attachment())
        with TemporaryDirectory() as raw:
            dest = Path(raw) / "downloads"
            parts, outcome = parts_to_open(
                rendered,
                [1],
                dest,
                viewers=(
                    ViewerRule(
                        content_type="application/octet-stream", command=("less",)
                    ),
                ),
            )

            self.assertEqual(outcome.changed, 1)
            (part,) = parts
            assert part.path is not None
            self.assertTrue(part.path.is_file())
            self.assertEqual(part.path.parent, dest)
            self.assertEqual(part.command, ("less",))

    def test_a_message_is_not_written_out_at_all(self) -> None:
        from pony.mail.message_export import parts_to_open

        rendered = render_message(corpus.plain_text())
        with TemporaryDirectory() as raw:
            dest = Path(raw) / "downloads"
            parts, outcome = parts_to_open(rendered, [WHOLE_MESSAGE], dest)

            (part,) = parts
            self.assertIsNone(part.path, "a message goes to the reader's own viewer")
            self.assertEqual(part.content_type, "message/rfc822")
            self.assertTrue(part.data)
            self.assertEqual(list(dest.iterdir()), [])
            self.assertEqual(outcome.changed, 1)

    def test_a_part_that_is_not_there_is_a_warning(self) -> None:
        from pony.mail.message_export import parts_to_open

        rendered = render_message(corpus.plain_text())
        with TemporaryDirectory() as raw:
            parts, outcome = parts_to_open(rendered, [9], Path(raw))

            self.assertEqual(parts, ())
            self.assertEqual(
                [(n.severity, n.text) for n in outcome.notices],
                [("warning", "No part 9 on this message.")],
            )

    def test_the_downloads_directory_falls_back_to_the_platform_one(self) -> None:
        from pony.mail.message_export import downloads_dir

        self.assertEqual(downloads_dir(Path("/elsewhere")), Path("/elsewhere"))
        # The fallback is sandboxed under `PONY_ROOT`, which the whole
        # suite runs with: a test that wrote or read the real
        # `~/Downloads` would be touching the user's own files, and the
        # save-folder picker opens where this says.
        default = downloads_dir(None)
        self.assertEqual(default.name, "Downloads")
        self.assertEqual(default, contain(Path.home() / "Downloads"))
        self.assertFalse(default.is_relative_to(Path.home()))
