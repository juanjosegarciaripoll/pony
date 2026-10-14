"""Replacing a file without ever leaving half of one on disk.

Write to a temporary name in the target directory, flush, ``fsync``,
then rename: a reader sees either the old file or the new one, and a
crash or a full disk leaves the old one intact rather than a truncated
config, a half-written token or an ``.ics`` that will not parse.

This is shared vocabulary, like :mod:`pony.outcome`:
anything both halves share has to sit on this side of it.  The dance was
written out five times inside this package and not at all in the mail
half, which wrote its profile stamp and its ``config.toml`` with
``write_text``.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

#: Prefix every temporary file gets.  Load-bearing:
#: :class:`pony.calendar.storage.VdirMirrorRepository` skips names starting
#: with it when it lists a mirror directory, so a write in flight is
#: never mistaken for a resource.
TEMP_PREFIX = ".tmp-"


def write_atomically(
    path: Path,
    payload: bytes,
    *,
    suffix: str = "",
    owner_only: bool = False,
) -> None:
    """Replace *path* with *payload*, or leave *path* as it was.

    *suffix* names the temporary file's extension, which matters only
    for a reader that filters by one.  With *owner_only* the mode is set
    to ``0o600`` before the rename, so the content is never readable by
    anyone else, not even for the instant between create and chmod --
    which is what a file holding a password or a refresh token needs.
    Mode is not enforced on Windows, where it means nothing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=suffix, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if owner_only and os.name != "nt":
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


__all__ = ["TEMP_PREFIX", "write_atomically"]
