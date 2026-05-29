"""Tests for attachment save helpers: ``_unique_path`` and ``save_attachment``."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import corpus

from pony.tui.message_renderer import render_message
from pony.tui.widgets.message_view import MessageViewPanel, _unique_path


class _FakePanel:
    """Minimal stub that satisfies ``save_attachment``'s ``self`` requirements."""


class UniquePathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_no_collision_returns_original(self) -> None:
        self.assertEqual(_unique_path(self.tmp, "report.pdf"), self.tmp / "report.pdf")

    def test_one_collision_appends_dash_1(self) -> None:
        (self.tmp / "report.pdf").write_bytes(b"x")
        self.assertEqual(
            _unique_path(self.tmp, "report.pdf"),
            self.tmp / "report-1.pdf",
        )

    def test_multiple_collisions_increment(self) -> None:
        for name in ("report.pdf", "report-1.pdf", "report-2.pdf"):
            (self.tmp / name).write_bytes(b"x")
        self.assertEqual(
            _unique_path(self.tmp, "report.pdf"),
            self.tmp / "report-3.pdf",
        )

    def test_no_extension(self) -> None:
        (self.tmp / "attachment").write_bytes(b"x")
        self.assertEqual(
            _unique_path(self.tmp, "attachment"),
            self.tmp / "attachment-1",
        )

    def test_double_extension_suffixes_before_last(self) -> None:
        (self.tmp / "archive.tar.gz").write_bytes(b"x")
        result = _unique_path(self.tmp, "archive.tar.gz")
        self.assertEqual(result, self.tmp / "archive.tar-1.gz")


class SaveAttachmentTest(unittest.TestCase):
    """``MessageViewPanel.save_attachment`` collision and error handling."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        raw = corpus.multipart_mixed_attachment()
        panel = _FakePanel()
        panel._rendered = render_message(raw)  # type: ignore[attr-defined]
        self._panel = panel

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _save(self, idx: int) -> str | None:
        return MessageViewPanel.save_attachment(self._panel, idx, self.tmp)  # type: ignore[arg-type]

    def test_saves_file_and_returns_filename(self) -> None:
        name = self._save(1)
        self.assertIsNotNone(name)
        assert name is not None
        self.assertTrue((self.tmp / name).exists())

    def test_second_save_of_same_attachment_gets_unique_name(self) -> None:
        name1 = self._save(1)
        name2 = self._save(1)
        self.assertIsNotNone(name1)
        self.assertIsNotNone(name2)
        self.assertNotEqual(name1, name2)
        assert name1 is not None and name2 is not None
        self.assertTrue((self.tmp / name1).exists())
        self.assertTrue((self.tmp / name2).exists())

    def test_out_of_range_index_returns_none(self) -> None:
        self.assertIsNone(self._save(99))

    def test_oserror_from_write_propagates(self) -> None:
        mock_path = MagicMock(spec=Path)
        mock_path.name = "q1-report.pdf"
        mock_path.write_bytes.side_effect = OSError("disk full")
        with (
            patch("pony.tui.widgets.message_view._unique_path", return_value=mock_path),
            self.assertRaises(OSError, msg="disk full"),
        ):
            self._save(1)


if __name__ == "__main__":
    unittest.main()
