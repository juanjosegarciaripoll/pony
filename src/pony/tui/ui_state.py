"""Persisted layout state for the TUI.

Pane sizes are a per-machine preference, not configuration: the user
adjusts them by dragging the boundary with a keybinding, and expects the
result to still be there next time.  Putting them in ``config.toml``
would mean rewriting a hand-edited file on every keypress, so they live
in their own JSON file in the data directory, alongside
``local_scan_state.json``.

The file is advisory.  Anything unreadable, malformed or out of range
falls back to the defaults — a bad state file must never stop the TUI
from opening.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Percentage of the screen width given to the folder panel.
DEFAULT_FOLDER_WIDTH_PCT = 25
MIN_FOLDER_WIDTH_PCT = 10
MAX_FOLDER_WIDTH_PCT = 60

# Share of the right-hand pane given to the message list, as a fraction
# of 100.  The reader takes the remainder.  Expressed in ``fr`` units so
# the list still fills the pane while the reader is hidden.
DEFAULT_LIST_SHARE = 34
MIN_LIST_SHARE = 15
MAX_LIST_SHARE = 85

# How much one keypress moves a boundary.
RESIZE_STEP = 5


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class PaneSizes:
    """Where the two resizable boundaries sit."""

    folder_width_pct: int = DEFAULT_FOLDER_WIDTH_PCT
    list_share: int = DEFAULT_LIST_SHARE

    def clamped(self) -> PaneSizes:
        """Return a copy with both values forced into their valid range."""
        return PaneSizes(
            folder_width_pct=_clamp(
                self.folder_width_pct, MIN_FOLDER_WIDTH_PCT, MAX_FOLDER_WIDTH_PCT
            ),
            list_share=_clamp(self.list_share, MIN_LIST_SHARE, MAX_LIST_SHARE),
        )


def load_pane_sizes(path: Path | None) -> PaneSizes:
    """Read pane sizes from *path*, falling back to the defaults."""
    if path is None or not path.is_file():
        return PaneSizes()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("Ignoring unreadable UI state file %s", path)
        return PaneSizes()
    if not isinstance(raw, dict):
        return PaneSizes()
    defaults = PaneSizes()
    folder = raw.get("folder_width_pct", defaults.folder_width_pct)
    listed = raw.get("list_share", defaults.list_share)
    if not isinstance(folder, int) or not isinstance(listed, int):
        return defaults
    return PaneSizes(folder_width_pct=folder, list_share=listed).clamped()


def save_pane_sizes(path: Path | None, sizes: PaneSizes) -> None:
    """Write *sizes* to *path*, ignoring any failure.

    Losing a layout preference is not worth interrupting the user over,
    so a read-only or full data directory is logged and shrugged off.
    """
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "folder_width_pct": sizes.folder_width_pct,
                    "list_share": sizes.list_share,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.debug("Could not write UI state file %s", path)
