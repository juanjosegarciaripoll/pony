"""Where the calendar's files live.

The calendar is hosted: it does not resolve platform directories of its
own.  Its root is handed to it by the application that embeds it --
Pony's ``AppPaths.calendar_dir``, inside the shared ``data_dir`` -- and
everything here is relative to that root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePath


def expand_path(raw: str | Path) -> Path:
    """Expand ``~``, ``$VAR`` and ``%VAR%`` in a configured path."""
    text = str(raw)
    text = os.path.expandvars(text)
    text = os.path.expanduser(text)  # noqa: PTH111
    return Path(text)


def is_half_qualified(raw: str, *, flavour: type[PurePath] = PurePath) -> bool:
    """Is *raw* neither relative nor fully qualified?

    On Windows ``"/mail"`` has a root but no drive and ``"C:mail"`` a
    drive but no root; neither is absolute, so joining it against a base
    silently drops the base -- ``data_dir / "/mail"`` is ``C:\\mail``.
    POSIX has no such shape, which is why this takes the flavour: the
    branch it guards is only reachable on Windows, and only a
    ``PureWindowsPath`` can exercise it elsewhere.

    It lives here rather than in :mod:`pony.config` because both halves
    resolve a configured mirror path against a base directory and both
    have to refuse the same shape; the dependency arrow points this way.
    """
    path = flavour(raw)
    return not path.is_absolute() and bool(path.drive or path.root)


@dataclass(frozen=True, slots=True)
class CalendarPaths:
    """Every calendar file, derived from one root.

    The host owns the root; this type owns the layout inside it.  Each
    path is computed, so none of them can point at a different root than
    the others.
    """

    root: Path

    @property
    def config_file(self) -> Path:
        """Only for a calendar running unhosted; Pony passes its own."""
        return self.root / "config.toml"

    @property
    def index_file(self) -> Path:
        return self.root / "index.sqlite3"

    @property
    def mirror_dir(self) -> Path:
        return self.root / "mirror"

    def mirror_path(self, account_name: str) -> Path:
        """Per-account mirror root, the default for ``mirror_path``.

        Matches the runtime layout ``<mirror_dir>/<account>/...``, so an
        account that configures nothing produces no on-disk surprise.
        """
        return self.mirror_dir / account_name

    @property
    def token_dir(self) -> Path:
        """OAuth access/refresh tokens, one file per account."""
        return self.root / "tokens"

    def token_path(self, account_name: str) -> Path:
        return self.token_dir / f"{account_name}.json"

    @property
    def mcp_state_file(self) -> Path:
        """Port and auth token written by a running MCP TCP server.

        The stdio bridge reads this to find and delegate to the
        long-running instance.
        """
        return self.root / "mcp_server.json"

    @property
    def lock_file(self) -> Path:
        """Gates concurrent syncs; kept across reboots, scoped per-user."""
        return self.root / "sync.lock"

    @property
    def tui_state_file(self) -> Path:
        """Persists the last view across sessions."""
        return self.root / "tui_state"

    @property
    def log_file(self) -> Path:
        return self.root / "tui.log"


_active: CalendarPaths | None = None


def activate(root: Path) -> CalendarPaths:
    """Put the calendar's files under *root* for this process.

    The host calls this once, as early as it knows its data directory.
    """
    global _active  # noqa: PLW0603
    _active = CalendarPaths(root=root)
    return _active


def active() -> CalendarPaths:
    """The paths the host activated.

    Raises rather than guessing: a calendar that resolved its own
    directory would write somewhere the host never asked for.
    """
    if _active is None:
        raise RuntimeError(
            "calendar paths are not set: the host application must call "
            "chronos.paths.activate(root) before using the calendar"
        )
    return _active
