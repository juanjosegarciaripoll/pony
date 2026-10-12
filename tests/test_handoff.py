"""Files written only so an external program can open them.

The attachment viewer needs a real file; what it must not do is leave
one. Every path here is inside the run's data directory, so
``PONY_ROOT`` contains it and ``pony reset`` reaches it.
"""

from __future__ import annotations

import unittest

from pony.handoff import activate, handoff_path
from pony.paths import default_data_dir


class HandoffPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = default_data_dir() / f"handoff-test-{id(self)}"
        activate(self.data_dir)

    def test_the_file_lands_under_the_data_directory(self) -> None:
        path = handoff_path(".pdf")
        path.write_bytes(b"%PDF-1.4\n")

        self.assertTrue(path.is_relative_to(self.data_dir))
        self.assertEqual(path.suffix, ".pdf")
        self.assertTrue(path.exists())

    def test_two_files_do_not_collide(self) -> None:
        self.assertNotEqual(handoff_path(".txt"), handoff_path(".txt"))

    def test_the_name_comes_from_here_not_from_the_message(self) -> None:
        """Only the extension is the sender's, because a viewer needs it."""
        for hostile in (
            "/../../etc/passwd",
            "../../../.bashrc",
            ".tar.gz/../../x",
            "." + "a" * 200,
            ".exe\x00.txt",
            ".pdf ",
            "..",
            "",
        ):
            with self.subTest(suffix=hostile):
                path = handoff_path(hostile)
                self.assertTrue(path.is_relative_to(self.data_dir))
                self.assertEqual(path.parent, self.data_dir / "handoff")

    def test_an_ordinary_extension_survives(self) -> None:
        for good in (".pdf", ".tar.gz", ".JPEG", ".docx", ".x"):
            with self.subTest(suffix=good):
                self.assertTrue(handoff_path(good).name.endswith(good))

    def test_being_told_where_discards_what_a_previous_run_left(self) -> None:
        """An atexit hook does not fire when the process is killed."""
        stale = handoff_path(".txt")
        stale.write_text("a message from a run that crashed", encoding="utf-8")
        self.assertTrue(stale.exists())

        activate(self.data_dir)
        handoff_path(".txt")  # first use after activation empties it

        self.assertFalse(stale.exists())
        self.assertTrue(stale.parent.is_dir())
