"""Files written only so an external program can open them.

Opening an attachment means handing a real file to whatever the system
opens that type with: a viewer cannot be passed bytes.  Those files used
to be ``NamedTemporaryFile(delete=False)`` in the system temporary
directory, and nothing ever unlinked one, so a reader accumulated a
plaintext copy of every attachment it had ever opened — outside
``data_dir``, so ``pony reset`` did not reach them, and on a machine
whose ``/tmp`` is a share of RAM, resident there until the next reboot.

They go under the run's own data directory instead, in a directory that
is emptied when it is first used and removed when the process ends.
Emptying on first use is what covers the run that crashed: an ``atexit``
hook does not fire when the process is killed, and Pony does not expect
a second process sharing the directory.

The host says where, as early as it knows, the same way it does for the
calendar's files — a module that resolved its own directory would write
somewhere the host never asked for.  Nothing here imports Textual: the
browser front end stages its uploads the same way, and
:func:`pony.web.compose.staging_dir` is the same idea for files that
have to outlive a request.
"""

from __future__ import annotations

import atexit
import re
import secrets
import shutil
from pathlib import Path

from .paths import default_data_dir

__all__ = ["activate", "handoff_path"]

_root: Path | None = None
_prepared: set[Path] = set()

#: A suffix the system may be asked to pick a viewer by: a dot and a
#: short run of ordinary filename characters, and nothing else.
_SUFFIX = re.compile(r"\A\.[A-Za-z0-9][A-Za-z0-9._-]{0,15}\Z")


def activate(data_dir: Path) -> Path:
    """Put hand-off files under *data_dir* for this process.

    Forgets that the directory was prepared, so the next file written
    empties it again: being told where to write is also the moment to
    discard whatever a previous run left behind.
    """
    global _root  # noqa: PLW0603
    _root = data_dir / "handoff"
    _prepared.discard(_root)
    return _root


def _ready() -> Path:
    """The hand-off directory, emptied once per process and swept at exit."""
    # Only reachable when nothing activated one, which outside the
    # application means a library caller; the default data directory is
    # still inside `PONY_ROOT`, so a test cannot escape through it.
    root = default_data_dir() / "handoff" if _root is None else _root
    if root not in _prepared:
        _prepared.add(root)
        shutil.rmtree(root, ignore_errors=True)
        atexit.register(shutil.rmtree, root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def handoff_path(suffix: str = "") -> Path:
    """A fresh path for a file an external program is about to open.

    The name is this module's own, not the sender's: only *suffix* comes
    from the message, because the viewer is chosen by extension.  A
    filename out of a message would be a path for an attacker to
    choose.
    """
    clean = suffix if _SUFFIX.fullmatch(suffix) else ""
    return _ready() / f"{secrets.token_hex(8)}{clean}"
