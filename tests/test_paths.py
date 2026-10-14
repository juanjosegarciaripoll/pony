"""Tests for pony.paths — PONY_ROOT, AppPaths and the bundled files."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch

from pony.paths import (
    SAMPLE_CONFIG,
    AppPaths,
    bundled_docs_path,
    contain,
    graft,
    resolve,
    sample_config_path,
    sandbox_root,
)


class BundledDocsPathTest(unittest.TestCase):
    def test_returns_none_when_not_frozen(self) -> None:
        result = bundled_docs_path()
        self.assertIsNone(result)

    def test_returns_none_when_frozen_but_no_meipass(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", None, create=True),
        ):
            result = bundled_docs_path()
        self.assertIsNone(result)

    def test_returns_path_when_frozen_and_site_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            site_dir = Path(tmpdir) / "site"
            site_dir.mkdir()
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", tmpdir, create=True),
            ):
                result = bundled_docs_path()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result, site_dir)

    def test_returns_none_when_frozen_but_site_missing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", tmpdir, create=True),
        ):
            result = bundled_docs_path()
        self.assertIsNone(result)


class SampleConfigPathTest(unittest.TestCase):
    """Where the annotated sample is, in each kind of install.

    ``pony config edit`` copies it into place for a first run, and it
    used to look in one place only -- ``<package>/../../`` -- which is
    the repository root and nothing else. In a bundle it sits beside the
    executable (`pony.spec` puts it there); in an ordinary wheel it is
    not shipped at all, and the answer has to be ``None`` rather than a
    path that does not exist.
    """

    def test_a_checkout_finds_it_at_the_repository_root(self) -> None:
        found = sample_config_path()

        assert found is not None
        self.assertEqual(found.name, SAMPLE_CONFIG)
        self.assertTrue(found.is_file())

    def test_a_bundle_finds_it_beside_the_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundled = Path(tmpdir) / SAMPLE_CONFIG
            bundled.write_text("config_version = 3\n", encoding="utf-8")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", tmpdir, create=True),
            ):
                found = sample_config_path()

        self.assertEqual(found, bundled)

    def test_a_wheel_with_neither_says_so(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", tmpdir, create=True),
            patch("pony.paths.Path.is_file", return_value=False),
        ):
            self.assertIsNone(sample_config_path())


class AppPathsTest(unittest.TestCase):
    def test_every_path_follows_the_data_directory(self) -> None:
        """One field moves them all: an index cannot outlive its data dir."""
        paths = AppPaths(
            config_file=Path("/a/config.toml"),
            data_dir=Path("/a/data"),
        )

        self.assertEqual(paths.index_db_file, Path("/a/data/index.sqlite3"))
        self.assertEqual(paths.log_dir, Path("/a/data/logs"))
        self.assertEqual(paths.mcp_state_file, Path("/a/data/mcp.json"))
        self.assertEqual(paths.scan_state_file, Path("/a/data/local_scan_state.json"))
        self.assertEqual(paths.calendar_dir, Path("/a/data/calendar"))

    def test_ensure_runtime_dirs_creates_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = AppPaths(
                config_file=root / "cfg" / "config.toml",
                data_dir=root / "data",
            )

            paths.ensure_runtime_dirs()

            self.assertTrue((root / "cfg").is_dir())
            self.assertTrue((root / "data").is_dir())
            self.assertTrue((root / "data" / "logs").is_dir())


class GraftTest(unittest.TestCase):
    """Relocating a path under a root, which a join cannot do."""

    def test_a_posix_path_keeps_its_tail(self) -> None:
        self.assertEqual(
            graft(PurePosixPath("/box"), PurePosixPath("/home/me/mail")),
            PurePosixPath("/box/%2F/home/me/mail"),
        )

    def test_a_join_would_have_discarded_the_root(self) -> None:
        """The bug this function exists for."""
        self.assertEqual(
            PurePosixPath("/box") / PurePosixPath("/etc"), PurePosixPath("/etc")
        )
        self.assertEqual(
            PureWindowsPath("X:/box") / PureWindowsPath("C:/data"),
            PureWindowsPath("C:/data"),
        )

    def test_a_drive_becomes_a_component(self) -> None:
        self.assertEqual(
            graft(PureWindowsPath("X:/box"), PureWindowsPath("C:/data/mail")),
            PureWindowsPath("X:/box/C%3A%5C/data/mail"),
        )

    def test_a_drive_relative_path_keeps_its_drive(self) -> None:
        """``C:mail`` is neither absolute nor relative; it still cannot join."""
        self.assertEqual(
            graft(PureWindowsPath("X:/box"), PureWindowsPath("C:mail")),
            PureWindowsPath("X:/box/C%3A/mail"),
        )

    def test_a_unc_share_becomes_one_component(self) -> None:
        grafted = graft(
            PureWindowsPath("X:/box"), PureWindowsPath("//server/share/mail")
        )

        self.assertEqual(len(grafted.relative_to(PureWindowsPath("X:/box")).parts), 2)
        self.assertEqual(grafted.name, "mail")

    def test_distinct_drives_stay_distinct(self) -> None:
        """Encoding is injective, so two drives cannot share a directory."""
        root = PureWindowsPath("X:/box")
        seen = {
            graft(root, PureWindowsPath(f"{letter}:/data"))
            for letter in ("C", "D", "E")
        }

        self.assertEqual(len(seen), 3)

    def test_a_relative_path_is_simply_joined(self) -> None:
        self.assertEqual(
            graft(PurePosixPath("/box"), PurePosixPath("mail/cur")),
            PurePosixPath("/box/mail/cur"),
        )

    def test_grafting_twice_changes_nothing(self) -> None:
        """Resolution runs more than once per process."""
        root = PurePosixPath("/box")
        once = graft(root, PurePosixPath("/home/me/mail"))

        self.assertEqual(graft(root, once), once)

    def test_a_path_that_climbs_out_is_pulled_back_in(self) -> None:
        """``/box/../../etc`` reads as inside the box until the OS reads it."""
        root = PurePosixPath("/box")

        grafted = graft(root, PurePosixPath("/box/../../etc/passwd"))

        self.assertEqual(grafted, PurePosixPath("/box/%2F/etc/passwd"))
        self.assertTrue(grafted.is_relative_to(root))

    def test_dot_segments_are_folded_away(self) -> None:
        root = PurePosixPath("/box")

        self.assertEqual(
            graft(root, PurePosixPath("/box/./data/../data")),
            PurePosixPath("/box/data"),
        )
        self.assertEqual(
            graft(root, PurePosixPath("../../escape")), PurePosixPath("/box/escape")
        )

    def test_the_root_itself_stays_inside(self) -> None:
        grafted = graft(PurePosixPath("/box"), PurePosixPath("/"))

        self.assertTrue(grafted.is_relative_to(PurePosixPath("/box")))


class SandboxRootTest(unittest.TestCase):
    """``PONY_ROOT`` is the one variable, and it contains everything."""

    def test_unset_means_no_containment(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PONY_ROOT", None)

            self.assertIsNone(sandbox_root())
            self.assertEqual(contain(Path("/home/me/mail")), Path("/home/me/mail"))

    def test_the_declared_data_dir_is_contained(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {"PONY_ROOT": tmpdir}),
        ):
            paths = resolve(data_dir=Path("/home/me/mail"))

            self.assertTrue(paths.data_dir.is_relative_to(Path(tmpdir)))
            self.assertTrue(paths.index_db_file.is_relative_to(Path(tmpdir)))

    def test_the_default_config_file_is_contained(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {"PONY_ROOT": tmpdir}),
        ):
            paths = resolve()

            self.assertTrue(paths.config_file.is_relative_to(Path(tmpdir)))
            self.assertTrue(paths.data_dir.is_relative_to(Path(tmpdir)))

    def test_an_explicit_config_path_is_used_as_given(self) -> None:
        """``--config`` points at a file to read; only writes are contained."""
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {"PONY_ROOT": tmpdir}),
        ):
            paths = resolve(Path("/elsewhere/config.toml"))

            self.assertEqual(paths.config_file, Path("/elsewhere/config.toml"))

    def test_a_path_already_inside_the_root_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inside = Path(tmpdir) / "data"
            with patch.dict(os.environ, {"PONY_ROOT": tmpdir}):
                self.assertEqual(resolve(data_dir=inside).data_dir, inside)

    def test_the_variable_expands_a_home_reference(self) -> None:
        with patch.dict(os.environ, {"PONY_ROOT": "~/boxed"}):
            root = sandbox_root()

        self.assertEqual(root, Path.home() / "boxed")


class DefaultLocationTest(unittest.TestCase):
    """The platform defaults, when nothing declares anything."""

    def test_xdg_variables_are_honoured(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": f"{tmpdir}/cfg",
                    "XDG_DATA_HOME": f"{tmpdir}/data",
                },
            ),
        ):
            os.environ.pop("PONY_ROOT", None)
            os.environ.pop("APPDATA", None)
            os.environ.pop("LOCALAPPDATA", None)
            paths = resolve()

        self.assertEqual(paths.config_file, Path(tmpdir) / "cfg/pony/config.toml")
        self.assertEqual(paths.data_dir, Path(tmpdir) / "data/pony")


class ProfileStampTest(unittest.TestCase):
    """The stamp is what stops a second config owning a data directory.

    It was written with `write_text`, so an interrupted first run could
    leave a truncated `profile.toml` -- a directory nothing owns and no
    config can adopt.  It goes through `pony.calendar.atomic` now, like every
    other file either half replaces.
    """

    def test_an_interrupted_stamp_leaves_nothing_behind(self) -> None:
        from pony.paths import PROFILE_STAMP, claim_profile, read_profile

        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw) / "data"
            with (
                patch("pony.calendar.atomic.os.replace", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                claim_profile(data_dir, "default")

            self.assertFalse((data_dir / PROFILE_STAMP).exists())
            self.assertEqual(list(data_dir.iterdir()), [])
            self.assertIsNone(read_profile(data_dir))


class ExpandPathTest(unittest.TestCase):
    """One expansion, used wherever a configured path is read.

    `~`, `$VAR` and `%VAR%` were expanded in four places -- the sandbox
    root, `data_dir`, `mirror.path` and the calendar's own paths -- and
    only one of them carried the comment saying why it is
    `os.path.expanduser` rather than `Path.expanduser`.
    """

    def test_every_reader_of_a_configured_path_uses_the_same_function(self) -> None:
        import pony.calendar.paths
        import pony.cli
        import pony.config
        import pony.paths

        one = pony.calendar.paths.expand_path
        self.assertIs(pony.paths.expand_path, one)
        self.assertIs(pony.config.expand_path, one)
        self.assertIs(pony.cli.expand_path, one)

    def test_a_variable_and_a_home_reference_both_expand(self) -> None:
        from pony.calendar.paths import expand_path

        with patch.dict(os.environ, {"PONY_TEST_DIR": "/somewhere"}):
            self.assertEqual(
                expand_path("$PONY_TEST_DIR/mail"), Path("/somewhere/mail")
            )
        self.assertFalse(str(expand_path("~/mail")).startswith("~"))
