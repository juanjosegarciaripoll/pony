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

import ast
import subprocess
import sys
import unittest
from pathlib import Path

# Modules that make up the headless backend surface.  `pony.calendar.scheduler`
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
    "pony.calendar.scheduler",
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
    "pony.calendar.keymap",
    "pony.folder_utils",
    "pony.calendar.outcome",
    "pony.calendar.event_actions",
    "pony.calendar.views",
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
    # `pony.web.syncing`, which imports the calendar's sync runner at
    # call time.
    "pony.calendar.sync",
    "pony.calendar.services",
    "pony.calendar.ingest",
    "pony.calendar.mutations",
    "pony.calendar.ical_parser",
    "pony.calendar.recurrence",
    "pony.calendar.storage",
    "pony.calendar.index_store",
    "pony.calendar.config",
    "pony.calendar.credentials",
    "pony.calendar.locking",
    "pony.calendar.paths",
    "pony.calendar_runtime",
    "pony.notifications",
    "pony.invitation",
    "pony.domain",
    "pony.paths",
    # The terminal's `w` serves its page from here instead of writing a
    # file, and it has to do that without the `web` extra installed:
    # `http.server` only, no Starlette, and no widget tree either.
    "pony.browser_preview",
    "pony.html_policy",
    "pony.handoff",
    "pony.authentication",
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


#: The program is five components, and only the direction of the
#: imports between them keeps them apart: the two models (the mail
#: stores plus IMAP, the calendar stores plus CalDAV), and the three
#: front ends over them (terminal, browser, command line).  A front end
#: may use either model; a model may use neither front end; and the
#: command line is a front end like the other two, not a library for
#: them to call into.
#:
#: That last one is what this grew out of: the calendar's sync runner
#: sat in a command-line module, so the terminal UI and the browser both
#: imported one to run a sync, and the host's own calendar glue imported
#: one to find out where a configuration lives.
_FRONT_END_PREFIXES = ("pony.tui", "pony.web")
#: The command line is Pony's, both halves of it: `pony.cli` parses and
#: `pony.cli_calendar` holds the calendar's commands. Neither model has
#: one of its own, which is how a front end came to import a command
#: line.
_COMMAND_LINE_MODULES = ("pony.cli", "pony.cli_calendar")
#: The program's entry point, which is above all five: starting the
#: command line is what it is for.
_ENTRY_POINTS = ("pony.__main__",)


def _module_name(path: Path, root: Path) -> str:
    """Dotted name of the module at *path*, relative to *root*."""
    relative = path.relative_to(root).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _imported_modules(path: Path, root: Path) -> set[str]:
    """Every module *path* imports, relative imports resolved.

    Read rather than imported: a late import inside a function is still
    one component reaching into another, and it is exactly how these
    crossings hid -- `pony.web.syncing` imported the calendar's CLI from
    inside a method.
    """
    own = _module_name(path, root)
    package = own.rsplit(".", 1)[0] if "." in own else own
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                upward = package.split(".")
                trimmed = upward[: len(upward) - node.level + 1]
                base = ".".join([*trimmed, node.module] if node.module else trimmed)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def _source_files() -> list[tuple[str, Path]]:
    root = Path(__file__).resolve().parent.parent / "src"
    return [
        (_module_name(path, root), path)
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def _belongs_to(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
    )


class ComponentsStayApartTest(unittest.TestCase):
    """The five components, and who is allowed to know about whom."""

    def test_no_model_module_imports_a_front_end(self) -> None:
        root = Path(__file__).resolve().parent.parent / "src"
        offenders: list[str] = []
        for module, path in _source_files():
            if (
                _belongs_to(module, _FRONT_END_PREFIXES)
                or module in _COMMAND_LINE_MODULES
                or module in _ENTRY_POINTS
            ):
                continue
            for imported in _imported_modules(path, root):
                if _belongs_to(
                    imported, (*_FRONT_END_PREFIXES, *_COMMAND_LINE_MODULES)
                ):
                    offenders.append(f"{module} -> {imported}")

        self.assertEqual(
            sorted(offenders),
            [],
            "a model module reaches into a front end: " + ", ".join(sorted(offenders)),
        )

    def test_no_front_end_imports_the_command_line(self) -> None:
        """The terminal and the browser go through the models, not the CLI."""
        root = Path(__file__).resolve().parent.parent / "src"
        offenders: list[str] = []
        for module, path in _source_files():
            if not _belongs_to(module, _FRONT_END_PREFIXES):
                continue
            for imported in _imported_modules(path, root):
                if _belongs_to(imported, _COMMAND_LINE_MODULES):
                    offenders.append(f"{module} -> {imported}")

        self.assertEqual(
            sorted(offenders),
            [],
            "a front end reaches into the command line: "
            + ", ".join(sorted(offenders)),
        )

    def test_the_scan_sees_a_late_import(self) -> None:
        """Guards the test itself: a function-level import counts."""
        root = Path(__file__).resolve().parent.parent / "src"
        found = _imported_modules(root / "pony" / "web" / "syncing.py", root)

        # This module imports the calendar's sync runner inside a method.
        self.assertIn("pony.calendar.services", found)


class ArchitectureListingTest(unittest.TestCase):
    """Rule 6: `docs/architecture.md` is the published package layout.

    It lists every module with a line saying what it is for, which is
    the first thing anyone reads to find where something lives -- and a
    new module simply did not appear in it (`pony.contact_merge`, the
    whole of the merge policy, and `pony.calendar.bootstrap`).  Nothing failed,
    because no test read the page.
    """

    def _listed(self) -> str:
        from pathlib import Path

        page = Path(__file__).resolve().parents[1] / "docs" / "architecture.md"
        return page.read_text(encoding="utf-8")

    def test_every_module_in_both_packages_is_named(self) -> None:
        from pathlib import Path

        page = self._listed()
        root = Path(__file__).resolve().parents[1] / "src"
        missing: list[str] = []
        for package in ("pony", "pony/calendar"):
            for module in sorted((root / package).glob("*.py")):
                if module.name in {"__init__.py", "version.py"}:
                    continue
                if module.name not in page:
                    missing.append(f"{package}/{module.name}")

        self.assertEqual(
            missing,
            [],
            "docs/architecture.md does not mention: " + ", ".join(missing),
        )
