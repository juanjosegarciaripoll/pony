"""Shared calendar-test configuration.

The calendar is hosted: its files live under a root the host activates
(Pony's ``AppPaths.calendar_dir``).  The suite activates a temporary one
for the whole session, so no test can reach a real user directory and no
test has to remember to set it.
"""

import atexit
import tempfile
from pathlib import Path

from chronos.paths import activate

_tmp_dir = tempfile.TemporaryDirectory(
    prefix="chronos-tests-", ignore_cleanup_errors=True
)

#: Root the calendar resolves its files under for this session.
TMP_ROOT = Path(_tmp_dir.name)

atexit.register(_tmp_dir.cleanup)

activate(TMP_ROOT / "calendar")
