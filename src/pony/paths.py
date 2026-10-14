"""Application path resolution.

Everything Pony writes lives under one directory: the index, the logs,
the scan state, the calendar's own data.  A config file says where that
directory is (`data_dir`), and `PONY_ROOT` relocates it -- which is how
a test session or a sandboxed install runs without reaching a real user
directory.
"""

from __future__ import annotations

import os
import sys as _sys
from dataclasses import dataclass
from pathlib import Path, PurePath
from urllib.parse import quote

from pony.calendar.atomic import write_atomically
from pony.calendar.paths import expand_path

APP_NAME = "pony"
ROOT_ENV = "PONY_ROOT"
PROFILE_STAMP = "profile.toml"


def sandbox_root() -> Path | None:
    """The directory ``PONY_ROOT`` confines this process to, if any."""
    raw = os.environ.get(ROOT_ENV)
    if not raw:
        return None
    return expand_path(raw)


def _lexically_normal[AnyPath: PurePath](path: AnyPath) -> AnyPath:
    """*path* with its ``.`` and ``..`` segments folded away.

    Lexical, not filesystem-truthful: nothing is read from disk and
    symlinks are not followed.  It exists because containment is decided
    by comparing spellings, and ``/box/../../etc`` is spelled as though
    it were inside ``/box``.
    """
    kept: list[str] = []
    for part in path.parts[1:] if path.anchor else path.parts:
        if part == "..":
            if kept:
                kept.pop()
        elif part != ".":
            kept.append(part)
    return type(path)(path.anchor, *kept)


def graft[AnyPath: PurePath](root: AnyPath, path: AnyPath) -> AnyPath:
    """Return *path* relocated inside *root*.

    ``root / path`` is not relocation: a join discards its left side
    when the right side carries an anchor, so ``Path("/box") / "/etc"``
    is ``/etc`` and ``PureWindowsPath("X:/box") / "C:/data"`` is
    ``C:\\data``.  The anchor therefore comes off and goes back on as an
    ordinary component, percent-encoded so that it stays one component
    and stays distinct: ``/`` becomes ``%2F``, ``C:\\`` becomes
    ``C%3A%5C``, and two drives cannot land on the same directory.

    Both sides are folded first, so a path that merely *reads* as though
    it were inside the root -- ``/box/../../etc`` -- is relocated like
    any other outsider instead of being let through.

    Grafting an already-grafted path returns it unchanged, so resolving
    twice is harmless.
    """
    base = _lexically_normal(root)
    target = _lexically_normal(path)
    if target.is_relative_to(base):
        return target
    anchor = target.anchor
    if not anchor:
        return base / target
    return base / quote(anchor, safe="") / target.relative_to(anchor)


def contain(path: Path) -> Path:
    """*path*, or its image under ``PONY_ROOT`` when that is set."""
    root = sandbox_root()
    return path if root is None else graft(root, path)


def _home_dir(windows: str, xdg: str, fallback: Path) -> Path:
    """The platform's base directory for one kind of user data."""
    raw = os.environ.get(windows) or os.environ.get(xdg)
    return Path(raw) if raw else fallback


def default_config_file() -> Path:
    """Where ``config.toml`` lives when ``--config`` does not say."""
    home = _home_dir("APPDATA", "XDG_CONFIG_HOME", Path.home() / ".config")
    return home / APP_NAME / "config.toml"


def default_data_dir() -> Path:
    """Where data lives when no config declares a ``data_dir``."""
    home = _home_dir(
        "LOCALAPPDATA",
        "XDG_DATA_HOME",
        Path.home() / ".local" / "share",
    )
    return home / APP_NAME


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Where this run reads and writes.

    Two paths decide every other: the config file, and the data
    directory that config declares.  The rest are derived, so a data
    directory cannot disagree with its own index.
    """

    config_file: Path
    data_dir: Path

    @property
    def index_db_file(self) -> Path:
        return self.data_dir / "index.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def mcp_state_file(self) -> Path:
        return self.data_dir / "mcp.json"

    @property
    def scan_state_file(self) -> Path:
        return self.data_dir / "local_scan_state.json"

    @property
    def calendar_dir(self) -> Path:
        """Root of the calendar's data, inside the shared data directory."""
        return self.data_dir / "calendar"

    def ensure_runtime_dirs(self) -> None:
        """Create expected runtime directories if missing."""
        for directory in (self.config_file.parent, self.data_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


def resolve(
    config_arg: Path | None = None,
    *,
    data_dir: Path | None = None,
) -> AppPaths:
    """The paths this run uses.

    *config_arg* is ``--config``, taken as given: reading a file where
    the user pointed is not a write.  *data_dir* is what that config
    declares.  Both directories Pony writes into -- the data directory,
    and the one holding a *default* config file -- are contained by
    ``PONY_ROOT``.
    """
    default_config = contain(default_config_file())
    config_file = config_arg if config_arg is not None else default_config
    declared = data_dir if data_dir is not None else default_data_dir()
    return AppPaths(config_file=config_file, data_dir=contain(declared))


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
        # The stamp is what stops a second config from pointing at this
        # directory, so a half-written one -- an interrupted first run,
        # a full disk -- would leave a directory nothing owns.
        write_atomically(
            data_dir / PROFILE_STAMP,
            f'profile = "{profile}"\n'.encode(),
            suffix=".toml",
        )
        return
    if found != profile:
        raise ProfileMismatchError(
            data_dir=data_dir,
            found=found,
            declared=profile,
        )


#: What `pony.spec` bundles beside the executable, and what the repo
#: keeps at its root: the annotated configuration the docs point at.
SAMPLE_CONFIG = "config-sample.toml"


def sample_config_path() -> Path | None:
    """Where the annotated sample configuration is, if it is here.

    Two places, because there are two kinds of install: a PyInstaller
    bundle carries it beside the executable (`pony.spec` adds it), and a
    source checkout keeps it at the repository root.  An ordinary wheel
    has neither, which is why this answers ``None`` rather than a path
    that does not exist -- `pony config edit` used to look only at
    ``<package>/../../config-sample.toml`` and silently take its
    fallback branch everywhere else.
    """
    meipass: str | None = getattr(_sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if getattr(_sys, "frozen", False) and meipass is not None:
        candidates.append(Path(meipass) / SAMPLE_CONFIG)
    candidates.append(Path(__file__).resolve().parents[2] / SAMPLE_CONFIG)
    return next((path for path in candidates if path.is_file()), None)


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
