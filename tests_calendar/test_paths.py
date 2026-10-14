from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from pony.calendar.paths import CalendarPaths, expand_path


class ExpandPathTest(unittest.TestCase):
    def test_tilde_expands_to_home(self) -> None:
        expanded = expand_path("~/calendar/mirror")
        self.assertTrue(expanded.is_absolute())
        self.assertIn("calendar", str(expanded))
        self.assertNotIn("~", str(expanded))

    def test_env_var_dollar_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CHRONOS_TEST_BASE"] = tmp
            try:
                expanded = expand_path("$CHRONOS_TEST_BASE/sub")
                self.assertEqual(expanded, Path(tmp) / "sub")
            finally:
                del os.environ["CHRONOS_TEST_BASE"]

    def test_env_var_percent_expansion_on_windows(self) -> None:
        if sys.platform != "win32":
            self.skipTest("percent-style vars are only expanded on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CHRONOS_TEST_BASE"] = tmp
            try:
                expanded = expand_path("%CHRONOS_TEST_BASE%/sub")
                self.assertEqual(expanded, Path(tmp) / "sub")
            finally:
                del os.environ["CHRONOS_TEST_BASE"]

    def test_literal_path_passes_through(self) -> None:
        self.assertEqual(expand_path("/no/expansion/here"), Path("/no/expansion/here"))


class CalendarPathsTest(unittest.TestCase):
    """The layout under the root the host hands over."""

    ROOT = Path("/data/calendar")

    def setUp(self) -> None:
        self.paths = CalendarPaths(root=self.ROOT)

    def test_every_file_sits_under_the_root(self) -> None:
        for path in (
            self.paths.config_file,
            self.paths.index_file,
            self.paths.mirror_dir,
            self.paths.token_dir,
            self.paths.mcp_state_file,
            self.paths.lock_file,
            self.paths.tui_state_file,
            self.paths.log_file,
            self.paths.mirror_path("personal"),
            self.paths.token_path("personal"),
        ):
            with self.subTest(path=str(path)):
                self.assertTrue(path.is_relative_to(self.ROOT))

    def test_the_layout_is_the_documented_one(self) -> None:
        self.assertEqual(self.paths.index_file, self.ROOT / "index.sqlite3")
        self.assertEqual(self.paths.mirror_dir, self.ROOT / "mirror")
        self.assertEqual(
            self.paths.mirror_path("personal"), self.ROOT / "mirror" / "personal"
        )
        self.assertEqual(
            self.paths.token_path("personal"), self.ROOT / "tokens" / "personal.json"
        )
        self.assertEqual(self.paths.lock_file, self.ROOT / "sync.lock")

    def test_no_two_accounts_share_a_mirror(self) -> None:
        self.assertNotEqual(
            self.paths.mirror_path("work"), self.paths.mirror_path("home")
        )


class HandedOverRootTest(unittest.TestCase):
    """The host says where the calendar's data lives; nothing else does.

    There is nothing to activate: a `CalendarPaths` is a value, and two
    of them coexist. It used to be installed into a module global, so
    the answer to "where does the calendar keep its files" depended on
    who had called `activate` last -- in this suite, on which conftest
    imported second.
    """

    def test_two_layouts_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = CalendarPaths(root=Path(one))
            second = CalendarPaths(root=Path(two))

            self.assertEqual(first.index_file, Path(one) / "index.sqlite3")
            self.assertEqual(second.index_file, Path(two) / "index.sqlite3")
            self.assertNotEqual(first.root, second.root)

    def test_the_module_offers_no_process_wide_root(self) -> None:
        """What the audit asked for: no `activate`, no `active`."""
        import pony.calendar.paths as module

        self.assertFalse(hasattr(module, "activate"))
        self.assertFalse(hasattr(module, "active"))
        self.assertFalse(hasattr(module, "_active"))


class PerTestRootTest(unittest.TestCase):
    """Each test gets a calendar root nothing else has written to.

    `MainScreen` persists the view it was last on and reads it back at
    startup, which is the feature; under one shared root it also meant
    every Pilot test inherited the previous one's view, and which test
    ran first was down to the scheduler.  A test cannot prove this on
    its own -- it would pass alone and fail only after another file ran
    -- so what is checked is the lever: the root is this test's.
    """

    def test_the_root_is_this_test_s_own(self) -> None:
        from tests_calendar.conftest import TMP_ROOT

        root = self.calendar_paths.root
        self.assertNotEqual(root, TMP_ROOT / "calendar")
        self.assertTrue(root.is_relative_to(TMP_ROOT))

    def test_no_view_is_inherited_from_another_test(self) -> None:
        self.assertFalse(self.calendar_paths.tui_state_file.exists())
