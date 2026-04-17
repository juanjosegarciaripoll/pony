"""Shared test configuration.

Defines TMP_ROOT — a temporary directory in the system temp location that all
test files use as their scratch space.  It is cleaned up automatically when the
test process exits (atexit, LIFO after MboxMirrorRepository._close_all).
"""

import atexit
import tempfile
from pathlib import Path

_tmp_dir = tempfile.TemporaryDirectory(prefix="pony-tests-", ignore_cleanup_errors=True)

#: Import this in test files instead of constructing a .tmp-tests path.
TMP_ROOT = Path(_tmp_dir.name)

atexit.register(_tmp_dir.cleanup)
