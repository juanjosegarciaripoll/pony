"""Where the calendar's files live.

The calendar is hosted: it does not resolve platform directories of its
own.  Its root is handed to it by the application that embeds it --
Pony's ``AppPaths.calendar_dir``, inside the shared ``data_dir`` -- and
everything here is relative to that root.

A :class:`CalendarPaths` travels as an argument.  It used to be set
once into a module global and read from everywhere through
``activate()`` / ``active()``, which is the hidden dependency
``docs/architecture.md`` tells both halves not to have: twenty call
sites across eleven modules depended on which root had been activated
last, including the configuration parser, so what a file *meant*
depended on process state.  Two test conftests set different roots at
import time and a worker running files from both got whichever
imported second.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
