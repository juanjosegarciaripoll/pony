"""Shared test configuration.

Defines TMP_ROOT — a temporary directory in the system temp location that all
test files use as their scratch space.  It is cleaned up automatically when the
test process exits (atexit, LIFO after MboxMirrorRepository._close_all).

Also loads a repo-root ``.env`` if present, so opt-in switches such as
``PONY_LIVE_IMAP=1`` can be set once per checkout instead of prefixed to
every command.  A real environment variable always wins, and the file is
gitignored — it is a local preference, not project configuration.
"""

import atexit
import os
import tempfile
from pathlib import Path


def _load_dotenv() -> None:
    """Populate os.environ from a repo-root .env, without overriding it.

    Deliberately hand-rolled rather than a plugin: it is a dozen lines
    and the alternative is a new test dependency for KEY=value.
    """
    env_file = Path(__file__).resolve().parent.parent / ".env"
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

_tmp_dir = tempfile.TemporaryDirectory(prefix="pony-tests-", ignore_cleanup_errors=True)

#: Import this in test files instead of constructing a .tmp-tests path.
TMP_ROOT = Path(_tmp_dir.name)

atexit.register(_tmp_dir.cleanup)
