"""Directory trees that survive what is in a real home directory.

``Path.is_dir()`` does not always answer.  It swallows the errors that
mean "no such thing" -- a missing file, a dangling symlink, a loop --
and propagates every other ``OSError``, which includes the one a
symlink into a network share that is down raises (``EHOSTDOWN``).
Textual's ``DirectoryTree`` calls it while sorting a directory's
children, so one unreachable entry in ``$HOME`` crashed the picker as
it opened: no save-folder dialog and no attachment dialog at all, for
as long as the mount stayed down.

Both pickers build their trees from here instead, so an entry that
cannot be looked at is left out of the listing rather than taking the
screen down with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import DirectoryTree

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def is_directory(path: Path) -> bool:
    """Whether *path* is a directory, with "cannot tell" meaning no."""
    try:
        return path.is_dir()
    except OSError:
        return False


def is_readable(path: Path) -> bool:
    """Whether *path* can be looked at at all.

    One ``stat`` answers both questions a tree asks of an entry -- is
    it there, and is it a directory -- so an entry that raises is
    dropped before Textual's own sort reaches it.
    """
    try:
        path.is_dir()
    except OSError:
        return False
    return True


class ReadableDirectoryTree(DirectoryTree):
    """Tree that leaves out the entries it cannot look at."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if is_readable(path)]


class DirOnlyTree(ReadableDirectoryTree):
    """Tree of directories, for picking somewhere to put a file."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if is_directory(path)]


__all__ = [
    "DirOnlyTree",
    "ReadableDirectoryTree",
    "is_directory",
    "is_readable",
]
