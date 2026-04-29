"""Application path resolution."""

from __future__ import annotations

import os
import sys as _sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved application directories for Pony Express."""

    config_file: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    log_dir: Path
    index_db_file: Path

    @classmethod
    def default(cls) -> AppPaths:
        """Resolve default application paths using environment overrides."""
        app_name = "pony"

        home = Path.home()
        config_root = Path(
            os.environ.get("PONY_CONFIG_DIR")
            or os.environ.get("APPDATA")
            or os.environ.get("XDG_CONFIG_HOME")
            or (home / ".config"),
        )
        data_root = Path(
            os.environ.get("PONY_DATA_DIR")
            or os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_DATA_HOME")
            or (home / ".local" / "share"),
        )
        state_root = Path(
            os.environ.get("PONY_STATE_DIR")
            or os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_STATE_HOME")
            or (home / ".local" / "state"),
        )
        cache_root = Path(
            os.environ.get("PONY_CACHE_DIR")
            or os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_CACHE_HOME")
            or (home / ".cache"),
        )

        config_dir = config_root / app_name
        data_dir = data_root / app_name
        state_dir = state_root / app_name

        return cls(
            config_file=config_dir / "config.toml",
            data_dir=data_dir,
            state_dir=state_dir,
            cache_dir=cache_root / app_name,
            log_dir=state_dir / "logs",
            index_db_file=data_dir / "index.sqlite3",
        )

    @property
    def mcp_state_file(self) -> Path:
        return self.state_dir / "mcp.json"

    def ensure_runtime_dirs(self) -> None:
        """Create expected runtime directories if missing."""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def bundled_docs_path() -> Path | None:
    """Return path to bundled HTML docs when running as a PyInstaller binary.

    Returns None when running from source (docs are on GitHub Pages instead).
    """
    meipass: str | None = getattr(_sys, "_MEIPASS", None)
    if getattr(_sys, "frozen", False) and meipass is not None:
        candidate = Path(meipass) / "site"
        if candidate.exists():
            return candidate
    return None
