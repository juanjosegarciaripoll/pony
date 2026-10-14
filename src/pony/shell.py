"""Splitting a command line the way the running platform quotes them."""

from __future__ import annotations

import shlex
import sys


def split_command(value: str, *, platform: str | None = None) -> list[str]:
    """Split *value* into argv, using the platform's quoting rules.

    ``shlex`` defaults to POSIX rules, where a backslash is an escape:
    ``shlex.split(r"C:\\Windows\\notepad.exe")`` is
    ``["C:Windowsnotepad.exe"]``, so every real Windows editor path
    failed to launch.

    Two callers want it -- the editor a configuration is edited in, and
    the ``$BROWSER`` an OAuth flow decides whether it can open a window
    on -- and they sit on opposite sides of the model/view line, which
    is why it is neither's.
    """
    real_platform = platform if platform is not None else sys.platform
    return shlex.split(value, posix=not real_platform.startswith("win"))


__all__ = ["split_command"]
