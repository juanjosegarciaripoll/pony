"""The backend must not drag the terminal UI in behind it.

Pony's mail, storage and indexing layers are meant to be usable without
a terminal — by the CLI, by the MCP server, and by anything else built
on them later.  That property is invisible in normal use and easy to
lose: one top-level ``from .tui.something import …`` in a backend module
re-exports the whole widget tree, because ``pony/tui/__init__.py``
imports the apps.

Each check runs in a fresh interpreter.  Testing this in-process would
prove nothing — by the time these tests run, other tests have already
imported Textual.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

# Modules that make up the headless backend surface.
_BACKEND_MODULES = (
    "pony.sync",
    "pony.storage",
    "pony.index_store",
    "pony.storage_indexing",
    "pony.message_projection",
    "pony.message_renderer",
    "pony.compose_utils",
    "pony.search_parser",
    "pony.contact_naming",
    "pony.config",
    "pony.imap_client",
    "pony.smtp_sender",
    "pony.credentials",
    "pony.services",
    "pony.bbdb",
    "pony.mcp_server",
    "pony.cli",
)


def _imports_textual(module: str) -> bool:
    """True when importing *module* in a fresh interpreter loads Textual."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            f"import sys, {module}; "
            "raise SystemExit(1 if 'textual' in sys.modules else 0)",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"importing {module} failed:\n{result.stderr.decode(errors='replace')}"
        )
    return result.returncode == 1


class BackendIsHeadlessTest(unittest.TestCase):
    def test_no_backend_module_loads_textual(self) -> None:
        offenders = [m for m in _BACKEND_MODULES if _imports_textual(m)]

        self.assertEqual(
            offenders,
            [],
            "these modules pull in Textual, so the backend can no longer be "
            "used headlessly: " + ", ".join(offenders),
        )

    def test_the_tui_package_does_load_textual(self) -> None:
        """Guards the test itself: prove the probe can detect a real import."""
        self.assertTrue(_imports_textual("pony.tui"))
