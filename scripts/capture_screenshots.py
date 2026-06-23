"""Render documentation screenshots of the Pony Express TUI.

Drives the real Textual screens headlessly over the synthetic store from
``scripts/demo_seed.py`` (no network, no real account), exports each screen
as SVG via Textual, then rasterises to PNG with Inkscape.  Output lands in
``docs/assets/``.

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

from demo_seed import build_demo  # noqa: E402

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


async def _capture_main(demo, svg_dir: Path) -> None:
    app = _CaptureApp(
        config=demo.config,
        index=demo.index,
        mirrors=demo.mirrors,
        credentials=demo.credentials,
        contacts=demo.index,
    )
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
    app = _CaptureApp(
        config=demo.config,
        index=demo.index,
        mirrors=demo.mirrors,
        credentials=demo.credentials,
        contacts=demo.index,
    )
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


async def _run() -> list[tuple[str, Path]]:
    tmp = Path(tempfile.mkdtemp(prefix="pony-shots-"))
    svg_dir = tmp / "svg"
    svg_dir.mkdir(parents=True, exist_ok=True)
    demo = build_demo(tmp / "store")
    print(f"Seeded demo store; capturing SVG into {svg_dir}")
    await _capture_main(demo, svg_dir)
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
