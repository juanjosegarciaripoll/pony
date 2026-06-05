"""Tests for pony.paths — AppPaths resolution and bundled_docs_path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pony.paths import AppPaths, bundled_docs_path


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
        import tempfile

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
        import tempfile

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", tmpdir, create=True),
        ):
            result = bundled_docs_path()
        self.assertIsNone(result)


class AppPathsTest(unittest.TestCase):
    def test_mcp_state_file_property(self) -> None:
        paths = AppPaths(
            config_file=Path("/a/config.toml"),
            data_dir=Path("/a/data"),
            state_dir=Path("/a/state"),
            cache_dir=Path("/a/cache"),
            log_dir=Path("/a/state/logs"),
            index_db_file=Path("/a/data/index.sqlite3"),
        )
        self.assertEqual(paths.mcp_state_file, Path("/a/state/mcp.json"))

    def test_default_uses_env_overrides(self) -> None:
        import os
        import tempfile

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(
                os.environ,
                {
                    "PONY_CONFIG_DIR": tmpdir,
                    "PONY_DATA_DIR": tmpdir,
                    "PONY_STATE_DIR": tmpdir,
                    "PONY_CACHE_DIR": tmpdir,
                },
            ),
        ):
            paths = AppPaths.default()
        self.assertEqual(paths.config_file, Path(tmpdir) / "config.toml")
        self.assertEqual(paths.data_dir, Path(tmpdir))

    def test_ensure_runtime_dirs_creates_directories(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = AppPaths(
                config_file=root / "cfg" / "config.toml",
                data_dir=root / "data",
                state_dir=root / "state" / "pony",
                cache_dir=root / "cache" / "pony",
                log_dir=root / "state" / "pony" / "logs",
                index_db_file=root / "data" / "index.sqlite3",
            )
            paths.ensure_runtime_dirs()
            self.assertTrue((root / "cfg").exists())
            self.assertTrue((root / "data").exists())
            self.assertTrue((root / "state" / "pony").exists())
            self.assertTrue((root / "cache" / "pony").exists())
            self.assertTrue((root / "state" / "pony" / "logs").exists())
