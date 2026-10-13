"""Shared calendar-test configuration.

The calendar is hosted: its files live under a root the host activates
(Pony's ``AppPaths.calendar_dir``).  The suite activates a temporary one
for the whole session, so no test can reach a real user directory and no
test has to remember to set it.

Each test then gets a root of its own, because some of the calendar's
state is meant to outlive a process: `MainScreen` writes the view it
was last on to ``tui_state`` and reads it back on startup, so every
Pilot test inherited whichever view the previous one left behind, and
in what order they ran was down to ``-n auto``.  Re-activating per test
is the only lever there is -- the persistence is the feature, not a
defect in the source.
"""

import atexit
import tempfile
from pathlib import Path

import pytest

from chronos.paths import activate

_tmp_dir = tempfile.TemporaryDirectory(
    prefix="chronos-tests-", ignore_cleanup_errors=True
)

#: Root the calendar resolves its files under for this session.
TMP_ROOT = Path(_tmp_dir.name)

atexit.register(_tmp_dir.cleanup)

activate(TMP_ROOT / "calendar")


@pytest.fixture(autouse=True)
def _calendar_root(request: pytest.FixtureRequest) -> None:
    """Give this test a calendar root nothing else has written to."""
    activate(TMP_ROOT / "per-test" / request.node.nodeid.replace("/", "_")[-120:])
