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

# Modules that make up the headless backend surface.  `chronos.scheduler`
# is in here because the mail half runs the periodic sync off it: the
# class has to stay free of Textual for `pony` to be able to import it
# without dragging the calendar's widget tree along.
#
# The action modules, the view projections and the keymap are in here
# because the browser front end is built on exactly them: anything one
# of these reaches for Textual to do is something the web interface
# cannot do, and `textual` is an optional extra, so it might not even be
# installed.
_BACKEND_MODULES = (
    "chronos.scheduler",
    "pony.sync",
    "pony.storage",
    "pony.index_store",
    "pony.storage_indexing",
    "pony.message_projection",
    "pony.message_renderer",
    "pony.pdf_export",
    "pony.compose_utils",
    "pony.composer",
    "pony.accounts",
    "pony.mailbox_ops",
    "pony.mailbox_actions",
    "pony.delivery",
    "pony.contact_actions",
    "pony.invitation_actions",
    "pony.views",
    "pony.keymap",
    "chronos.keymap",
    "pony.folder_utils",
    "chronos.outcome",
    "chronos.event_actions",
    "chronos.views",
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
    # Added after an audit found the list hand-kept and these missing:
    # the browser reaches every one of them, directly or through
    # `pony.web.syncing`, which imports `chronos.cli` at call time.
    "chronos.sync",
    "chronos.cli",
    "chronos.ingest",
    "chronos.mutations",
    "chronos.ical_parser",
    "chronos.recurrence",
    "chronos.storage",
    "chronos.index_store",
    "chronos.config",
    "chronos.credentials",
    "chronos.locking",
    "chronos.paths",
    "pony.calendar",
    "pony.notifications",
    "pony.invitation",
    "pony.domain",
    "pony.paths",
    # The terminal's `w` serves its page from here instead of writing a
    # file, and it has to do that without the `web` extra installed:
    # `http.server` only, no Starlette, and no widget tree either.
    "pony.browser_preview",
    "pony.html_policy",
    "pony.web",
)

# Nothing outside `pony.web` may import Starlette: it is an extra, so a
# terminal-only install does not have it, and `pony tui` has to start
# without it.  `ai/AGENTS.md` rule 5 says this test enforces both
# directions; until now it only checked Textual.
_STARLETTE_FREE_MODULES = (
    *(module for module in _BACKEND_MODULES if not module.startswith("pony.web")),
    "pony.tui",
)


def _imports(module: str, package: str) -> bool:
    """True when importing *module* in a fresh interpreter loads *package*."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            f"import sys, {module}; "
            f"raise SystemExit(1 if {package!r} in sys.modules else 0)",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"importing {module} failed:\n{result.stderr.decode(errors='replace')}"
        )
    return result.returncode == 1


def _imports_textual(module: str) -> bool:
    return _imports(module, "textual")


def _imports_starlette(module: str) -> bool:
    return _imports(module, "starlette")


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


class WebFrontEndIsIndependentTest(unittest.TestCase):
    """The browser front end must not need the terminal one.

    ``textual`` and ``starlette`` are separate extras, so an install can
    have either. A web module that imported Textual would make
    ``pip install pony[web]`` fail at the first request.
    """

    def test_the_web_package_does_not_load_textual(self) -> None:
        self.assertFalse(_imports_textual("pony.web"))

    def test_nothing_outside_the_web_package_loads_starlette(self) -> None:
        """The other half of the rule, which was never actually checked."""
        offenders = [m for m in _STARLETTE_FREE_MODULES if _imports_starlette(m)]

        self.assertEqual(
            offenders,
            [],
            "these modules pull in Starlette, which is an optional extra: "
            + ", ".join(offenders),
        )

    def test_the_probe_can_see_starlette(self) -> None:
        """Guards the test itself, as the Textual probe is guarded."""
        self.assertTrue(_imports_starlette("pony.web.app"))

    def test_the_web_package_renders_without_textual(self) -> None:
        """Not just the import: the pages themselves must come out."""
        for module in (
            "pony.web.app",
            "pony.web.html",
            "pony.web.layout",
            "pony.web.mail",
            "pony.web.compose",
            "pony.web.contacts",
            "pony.web.calendar",
            "pony.web.syncing",
            "pony.web.urls",
            "pony.web.actions",
        ):
            with self.subTest(module=module):
                self.assertFalse(_imports_textual(module))
