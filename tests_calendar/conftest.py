"""Shared calendar-test configuration.

The calendar is hosted: its files live under a root the host hands it
(Pony's ``AppPaths.calendar_dir``).  The suite keeps that root in a
temporary directory, so no test can reach a real user directory.

Each test gets a root of its own, because some of the calendar's state
is meant to outlive a process: `MainScreen` writes the view it was last
on to ``tui_state`` and reads it back on startup, so every Pilot test
inherited whichever view the previous one left behind, and in what
order they ran was down to ``-n auto``.

The root arrives as a value rather than as a process-wide setting: ask
for the ``calendar_paths`` fixture, or read ``self.calendar_paths``
inside a ``unittest.TestCase`` (which cannot take fixture arguments, so the
autouse fixture puts it on the class).  It used to be installed into a
module global by ``pony.calendar.paths.activate``, which both this conftest
and ``tests/conftest.py`` did at import time with different roots --
whichever imported second won for that worker.
"""

import atexit
import re
import tempfile
from pathlib import Path, PurePosixPath

import pytest

from pony.calendar.paths import CalendarPaths

_tmp_dir = tempfile.TemporaryDirectory(
    prefix="chronos-tests-", ignore_cleanup_errors=True
)

#: Root the calendar's test files live under for this session.
TMP_ROOT = Path(_tmp_dir.name)

atexit.register(_tmp_dir.cleanup)


#: The anchor a rooted-but-driveless path resolves against: ``/`` on
#: POSIX, the current drive on Windows.  ``Path.cwd()`` and not the
#: temporary directory, because that is what ``Path.resolve()`` itself
#: prepends, and these paths are compared against resolved ones.
FS_ANCHOR = Path.cwd().anchor


def absolute(posix_path: str) -> str:
    """*posix_path* spelled as an absolute path on this platform.

    ``"/tmp/m"`` has a root but no drive on Windows, so it is neither
    relative nor fully qualified and `pony.paths.is_half_qualified`
    refuses it -- correctly.  A fixture that wants a complete path has
    to say which drive, or it tests the refusal instead of the feature.
    """
    return str(Path(FS_ANCHOR, *PurePosixPath(posix_path).parts[1:]))


_UNPORTABLE_IN_A_NAME = re.compile(r"[^A-Za-z0-9._-]")


def safe_dir_name(nodeid: str) -> str:
    """A pytest node id as one directory name, on any filesystem.

    A node id is ``file.py::Class::test_name``, and Windows forbids
    ``:`` in a filename outright -- the ``mkdir`` answers ``WinError
    123``, "the filename, directory name, or volume label syntax is
    incorrect", which took out every test that writes under a root of
    its own there.  Replacing only ``/``, as this did, is enough for
    POSIX and nowhere else.  The tail is what is kept, because that is
    the part that names the test.
    """
    return _UNPORTABLE_IN_A_NAME.sub("_", nodeid)[-120:]


def paths_for(name: str) -> CalendarPaths:
    """A calendar root of its own for *name*."""
    return CalendarPaths(root=TMP_ROOT / "per-test" / safe_dir_name(name))


@pytest.fixture(autouse=True)
def calendar_paths(request: pytest.FixtureRequest) -> CalendarPaths:
    """A calendar root nothing else has written to."""
    paths = paths_for(request.node.nodeid)
    if request.cls is not None:
        # A `unittest.TestCase` method cannot be handed a fixture, so
        # the class carries it: `self.calendar_paths`.
        request.cls.calendar_paths = paths
    return paths
