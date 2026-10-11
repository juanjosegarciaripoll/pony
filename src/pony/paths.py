"""Application path resolution."""

from __future__ import annotations

import dataclasses
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

        config_dir = (
            config_root if os.environ.get("PONY_CONFIG_DIR") else config_root / app_name
        )
        data_dir = (
            data_root if os.environ.get("PONY_DATA_DIR") else data_root / app_name
        )
        state_dir = state_root / app_name

        return cls(
            config_file=config_dir / "config.toml",
            data_dir=data_dir,
            state_dir=state_dir,
            cache_dir=cache_root / app_name,
            log_dir=state_dir / "logs",
            index_db_file=data_dir / "index.sqlite3",
        )

    def with_data_dir(self, data_dir: Path) -> AppPaths:
        """Return a copy whose data directory (and index) is *data_dir*."""
        resolved = data_dir.expanduser()
        return dataclasses.replace(
            self,
            data_dir=resolved,
            index_db_file=resolved / "index.sqlite3",
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


PROFILE_STAMP = "profile.toml"


class ProfileMismatchError(Exception):
    """The data directory is stamped with a different profile name."""

    def __init__(self, *, data_dir: Path, found: str, declared: str) -> None:
        self.data_dir = data_dir
        self.found = found
        self.declared = declared
        super().__init__(
            f"data directory {data_dir} belongs to profile {found!r}, "
            f"but this config declares profile {declared!r}"
        )


def read_profile(data_dir: Path) -> str | None:
    """Return the profile name stamped on *data_dir*, or None."""
    try:
        text = (data_dir / PROFILE_STAMP).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "profile":
            return value.strip().strip('"').strip("'") or None
    return None


def claim_profile(data_dir: Path, profile: str) -> None:
    """Stamp *profile* on *data_dir*, or raise if another owns it.

    An unstamped directory is adopted: that covers first run and any
    directory predating the stamp.
    """
    found = read_profile(data_dir)
    if found is None:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / PROFILE_STAMP).write_text(
            f'profile = "{profile}"\n',
            encoding="utf-8",
        )
        return
    if found != profile:
        raise ProfileMismatchError(
            data_dir=data_dir,
            found=found,
            declared=profile,
        )


def resolve_data_dir(declared: Path | None) -> Path | None:
    """Effective data directory: ``PONY_DATA_DIR`` wins, else *declared*.

    The config's ``data_dir`` is how a user says where data lives.  The
    environment variable relocates the *default* -- something no config
    can do, because it has to apply before and without one.  The test
    suite sets it for the whole session so no test can reach the real
    directory, and packagers use it for sandboxed installs.
    """
    override = os.environ.get("PONY_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return declared


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
