"""Tests for SaveMessageScreen and its filename-generation helpers."""

from __future__ import annotations

import unittest

import corpus

from pony.tui.message_renderer import render_message
from pony.tui.screens.save_message_screen import (
    SaveItem,
    SaveMessageScreen,
    _proposed_attachment_filename,
    _proposed_body_filename,
    _subject_slug,
)

# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


class SubjectSlugTest(unittest.TestCase):
    def test_spaces_become_hyphens(self) -> None:
        self.assertEqual(_subject_slug("Hello World"), "hello-world")

    def test_non_alphanum_stripped(self) -> None:
        self.assertEqual(_subject_slug("Re: Q3 report!"), "re-q3-report")

    def test_empty_subject_returns_fallback(self) -> None:
        self.assertEqual(_subject_slug(""), "message")

    def test_long_subject_truncated(self) -> None:
        slug = _subject_slug("a" * 200)
        self.assertLessEqual(len(slug), 50)


class ProposedBodyFilenameTest(unittest.TestCase):
    def test_includes_date_and_slug(self) -> None:
        raw = corpus.plain_text()
        rendered = render_message(raw)
        name = _proposed_body_filename(rendered)
        self.assertTrue(name.endswith(".md"))
        self.assertIn("2026", name)

    def test_fallback_when_no_date(self) -> None:
        from pony.tui.message_renderer import RenderedMessage

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
        name = _proposed_body_filename(rendered)
        self.assertTrue(name.endswith(".md"))
        self.assertNotIn("_", name[:4])  # no leading date-underscore


class ProposedAttachmentFilenameTest(unittest.TestCase):
    def test_uses_given_filename(self) -> None:
        self.assertEqual(_proposed_attachment_filename("report.pdf", 1), "report.pdf")

    def test_fallback_for_empty_filename(self) -> None:
        self.assertEqual(_proposed_attachment_filename("", 2), "attachment-2")


# ---------------------------------------------------------------------------
# Pilot (async) tests — UI interaction via textual.testing
# ---------------------------------------------------------------------------


async def test_save_message_screen_cancel_returns_none() -> None:
    """Pressing Cancel dismisses with None."""
    from textual.app import App, ComposeResult

    raw = corpus.plain_text()
    rendered = render_message(raw)

    class _TestApp(App[list[SaveItem] | None]):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(SaveMessageScreen(rendered), self.exit)

    async with _TestApp().run_test() as pilot:
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()

    assert pilot.app.return_value is None


async def test_save_message_screen_save_all_checked() -> None:
    """Save with all boxes checked yields body + all attachments."""
    from textual.app import App, ComposeResult

    raw = corpus.multipart_mixed_attachment()
    rendered = render_message(raw)

    class _TestApp(App[list[SaveItem] | None]):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(SaveMessageScreen(rendered), self.exit)

    async with _TestApp().run_test() as pilot:
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause()

    result = pilot.app.return_value
    assert result is not None
    kinds = {item.kind for item in result}
    assert "body" in kinds
    assert "attachment:1" in kinds
    assert len(result) == 2


async def test_save_message_screen_uncheck_body() -> None:
    """Unchecking the body checkbox excludes the body from the result."""
    from textual.app import App, ComposeResult

    raw = corpus.multipart_mixed_attachment()
    rendered = render_message(raw)

    class _TestApp(App[list[SaveItem] | None]):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(SaveMessageScreen(rendered), self.exit)

    async with _TestApp().run_test() as pilot:
        await pilot.pause()
        await pilot.click("#check-body")
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause()

    result = pilot.app.return_value
    assert result is not None
    kinds = {item.kind for item in result}
    assert "body" not in kinds
    assert "attachment:1" in kinds


async def test_save_message_screen_edited_filename_in_result() -> None:
    """Editing the body filename Input is reflected in the dismissed value."""
    from textual.app import App, ComposeResult
    from textual.widgets import Input

    raw = corpus.plain_text()
    rendered = render_message(raw)

    class _TestApp(App[list[SaveItem] | None]):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(SaveMessageScreen(rendered), self.exit)

    async with _TestApp().run_test() as pilot:
        await pilot.pause()
        inp = pilot.app.screen.query_one("#name-body", Input)
        inp.value = "custom-name.md"
        await pilot.click("#save")
        await pilot.pause()

    result = pilot.app.return_value
    assert result is not None
    body_item = next(i for i in result if i.kind == "body")
    assert body_item.filename == "custom-name.md"


async def test_save_message_screen_no_attachments() -> None:
    """A plain-text message shows only the body row."""
    from textual.app import App, ComposeResult
    from textual.widgets import Checkbox

    raw = corpus.plain_text()
    rendered = render_message(raw)
    assert len(rendered.attachments) == 0

    class _TestApp(App[list[SaveItem] | None]):
        def compose(self) -> ComposeResult:
            return iter([])

        def on_mount(self) -> None:
            self.push_screen(SaveMessageScreen(rendered), self.exit)

    async with _TestApp().run_test() as pilot:
        await pilot.pause()
        boxes = pilot.app.screen.query(Checkbox)
        assert len(list(boxes)) == 1
        await pilot.click("#cancel")
