"""Render documentation screenshots of the Pony Express TUI.

Drives the real Textual screens headlessly over the synthetic store from
``scripts/demo_seed.py`` (no network, no real account, no real calendar),
exports each screen as SVG via Textual, then rasterises to PNG with Inkscape.
Output lands in ``docs/assets/``.

    uv run python scripts/capture_screenshots.py

Requires ``inkscape`` on PATH for the SVG→PNG step.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from demo_seed import NOW, build_demo  # noqa: E402

from pony.tui.app import ComposeApp, ContactsApp, PonyApp  # noqa: E402
from pony.tui.widgets.message_list import MessageListPanel  # noqa: E402

ASSETS = REPO_ROOT / "docs" / "assets"
SIZE = (120, 34)
PNG_WIDTH = 1640


class _CaptureApp(PonyApp):
    """PonyApp with the embedded MCP TCP server disabled.

    The real server binds a loopback port and writes the user's actual MCP
    state file; neither is wanted for an offline screenshot run.
    """

    async def _start_mcp_tcp_server(self) -> None:  # noqa: D401
        return


def _write_svg(svg: str, name: str, svg_dir: Path) -> Path:
    path = svg_dir / f"{name}.svg"
    path.write_text(svg, encoding="utf-8")
    return path


def _to_png(svg_path: Path, name: str) -> None:
    out = ASSETS / f"{name}.png"
    subprocess.run(
        [
            "inkscape",
            str(svg_path),
            "--export-type=png",
            f"--export-filename={out}",
            f"--export-width={PNG_WIDTH}",
        ],
        check=True,
        capture_output=True,
    )
    print(f"  wrote {out.relative_to(REPO_ROOT)}")


def _mail_app(demo) -> _CaptureApp:
    """A capture app with the demo calendar attached.

    The calendar is passed even to the mail captures: F2 belongs in the
    footer and the next event belongs in the header, and a screenshot
    taken without one would show neither.
    """
    return _CaptureApp(
        config=demo.config,
        index=demo.index,
        mirrors=demo.mirrors,
        credentials=demo.credentials,
        contacts=demo.index,
        calendar=demo.calendar,
        # Pin the clock to the demo's, so the agenda opens on the day the
        # events are on and the next-event line has something to name.
        # Without this the screenshots would drift with the wall clock.
        now=lambda: NOW,
    )


async def _capture_main(demo, svg_dir: Path) -> None:
    app = _mail_app(demo)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        # Move to the message with an attachment so both the list marker and
        # the preview pane are populated with something interesting.
        app.screen.query_one(MessageListPanel).focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        _write_svg(app.export_screenshot(), "main-screen", svg_dir)


async def _capture_search(demo, svg_dir: Path) -> None:
    app = _mail_app(demo)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.screen._run_search("review")  # type: ignore[attr-defined]
        await pilot.pause()
        await pilot.pause()
        _write_svg(app.export_screenshot(), "search", svg_dir)


async def _capture_compose(demo, svg_dir: Path) -> None:
    app = ComposeApp(
        config=demo.config,
        account=demo.account,
        index=demo.index,
        mirrors=demo.mirrors,
        contacts=demo.index,
        to="Katherine Johnson <k.johnson@nasa.gov>",
        cc="Grace Hopper <grace.hopper@navy.mil>",
        subject="Re: Trajectory figures for the review",
        body=(
            "Katherine,\n\n"
            "The figures check out — the re-entry corridor matches our hand "
            "calculations exactly. I'll fold them into the review packet.\n\n"
            "One question on figure 3: should the corridor band use the "
            "conservative drag estimate? Happy to defer to you.\n\n"
            "Thanks for the quick turnaround.\n"
        ),
        markdown_mode=True,
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.pause()
        _write_svg(app.export_screenshot(), "compose", svg_dir)


async def _capture_contacts(demo, svg_dir: Path) -> None:
    app = ContactsApp(contacts=demo.index)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.pause()
        _write_svg(app.export_screenshot(), "contacts", svg_dir)


async def _capture_calendar(demo, svg_dir: Path) -> None:
    """The agenda, reached the way a user reaches it."""
    app = _mail_app(demo)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        # Choose the view explicitly rather than inheriting a remembered
        # one: the multi-day grid is the view that most obviously reads
        # as a calendar.
        await pilot.press("4")
        await pilot.pause()
        await pilot.pause()
        _write_svg(app.export_screenshot(), "calendar", svg_dir)


async def _capture_invitation(demo, svg_dir: Path) -> None:
    """A message carrying an invitation, and the dialog that answers it."""
    app = _mail_app(demo)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.screen.query_one(MessageListPanel).focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        _write_svg(app.export_screenshot(), "invitation", svg_dir)
        await pilot.press("i")
        await pilot.pause()
        _write_svg(app.export_screenshot(), "invitation-dialog", svg_dir)


def _isolate_calendar_state(root: Path) -> None:
    """Point the calendar's data directory at *root* for this process.

    The calendar remembers its last view in a file under that directory
    and reads it back on mount. Left alone, a capture would open on
    whichever view the person running it happens to prefer — and could
    overwrite their choice — so the screenshots would not be reproducible
    and would carry a trace of a real install. Patching the resolver
    covers every platform branch, which setting `XDG_DATA_HOME` would
    not.
    """
    import chronos.paths

    root.mkdir(parents=True, exist_ok=True)
    chronos.paths.user_data_dir = lambda: root  # type: ignore[assignment]


async def _run() -> list[tuple[str, Path]]:
    tmp = Path(tempfile.mkdtemp(prefix="pony-shots-"))
    _isolate_calendar_state(tmp / "calendar-state")
    svg_dir = tmp / "svg"
    svg_dir.mkdir(parents=True, exist_ok=True)
    demo = build_demo(tmp / "store")
    print(f"Seeded demo store; capturing SVG into {svg_dir}")
    await _capture_main(demo, svg_dir)
    await _capture_calendar(demo, svg_dir)
    await _capture_invitation(demo, svg_dir)
    await _capture_search(demo, svg_dir)
    await _capture_compose(demo, svg_dir)
    await _capture_contacts(demo, svg_dir)
    return [(p.stem, p) for p in sorted(svg_dir.glob("*.svg"))]


def main() -> int:
    if shutil.which("inkscape") is None:
        print("error: inkscape not found on PATH", file=sys.stderr)
        return 1
    ASSETS.mkdir(parents=True, exist_ok=True)
    svgs = asyncio.run(_run())
    print("Rasterising to PNG:")
    for name, svg_path in svgs:
        _to_png(svg_path, name)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
