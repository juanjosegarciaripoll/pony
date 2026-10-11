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


def _sandbox_app_dirs(root: Path) -> None:
    """Point every Pony directory inside *root*, for the whole session.

    Without this the default data directory is the one the user keeps
    mail in, and a test that builds ``AppPaths.default()`` writes there.
    Set before any test imports, so no test has to remember.
    """
    for key, name in (
        ("PONY_CONFIG_DIR", "config"),
        ("PONY_DATA_DIR", "data"),
        ("PONY_STATE_DIR", "state"),
        ("PONY_CACHE_DIR", "cache"),
    ):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)


_tmp_dir = tempfile.TemporaryDirectory(prefix="pony-tests-", ignore_cleanup_errors=True)

#: Import this in test files instead of constructing a .tmp-tests path.
TMP_ROOT = Path(_tmp_dir.name)

atexit.register(_tmp_dir.cleanup)

_sandbox_app_dirs(TMP_ROOT / "session-dirs")

#: Data directory the innermost ``isolated_app_env()`` is using.  Config
#: fixtures interpolate it so tests declare ``data_dir`` the way a real
#: config does, instead of relying on an override that only tests have.
ISOLATED_DATA_DIR: list[Path] = []


def isolated_data_dir() -> Path:
    """Data directory for config fixtures to declare."""
    return ISOLATED_DATA_DIR[-1] if ISOLATED_DATA_DIR else TMP_ROOT / "data"


def resolve_fixture(toml: str) -> str:
    """Substitute ``{DATA_DIR}`` in a config fixture.

    The placeholder must sit inside a single-quoted TOML literal: a
    Windows path in a double-quoted string makes ``\\U``/``\\t``
    escapes, which would make every fixture invalid TOML there.
    """
    return toml.replace("{DATA_DIR}", str(isolated_data_dir()))
