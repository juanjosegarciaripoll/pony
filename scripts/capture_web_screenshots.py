"""Render documentation screenshots of the browser front end.

Serves the synthetic store from ``scripts/demo_seed.py`` with the real
``pony.web`` application on a loopback port, then photographs each page
with headless Chromium.  No network, no real account, no real calendar:
the store is built under a temp directory and deleted again, and the
browser runs against a profile of its own there, so nothing a real
install owns is read or written.

    uv run python scripts/capture_web_screenshots.py

``scripts/capture_screenshots.py`` calls this after the terminal
captures, so one command regenerates every image in ``docs/assets/``.

Chromium's own ``--screenshot`` flag is the whole reason the script is
shaped this way.  Driving a browser over DevTools would mean a test
framework, a driver and a Node toolchain for the sake of six PNGs; a
headless browser already knows how to photograph a page.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from demo_seed import NOW, DemoData, build_demo  # noqa: E402

from pony.domain import FolderRef  # noqa: E402
from pony.web import WebServices, build_web_app  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.applications import Starlette
    from uvicorn import Server

ASSETS = REPO_ROOT / "docs" / "assets"

# Wide enough for the three-pane layout to read as three panes, and
# close to the width the terminal captures are rasterised at.
DESKTOP = (1600, 1000)
# Narrower than the 52rem breakpoint, so the responsive layout is what
# gets photographed: one pane at a time, two-line rows, no footer.
# Not narrower than 500, though: headless Chromium clamps the window it
# lays out to that, while still cropping the image to whatever
# `--window-size` asked for, so a phone-sized number here would
# photograph the left 430 points of a 500-point page and cut the Sync
# button off the right-hand side.
PHONE = (520, 880)
# A phone shot at one device pixel per CSS pixel would be unreadably
# small beside the others, so it is taken at twice the density.
PHONE_SCALE = 2

# Every browser Chromium is called by on the platforms anyone builds
# the docs on.
BROWSERS = ("chromium", "chromium-browser", "google-chrome", "chrome")


@dataclass(frozen=True, slots=True)
class Shot:
    """One page, at one size, under one name."""

    name: str
    path: str
    size: tuple[int, int] = DESKTOP
    scale: int = 1


def find_browser() -> str | None:
    """Chromium, or whatever this machine calls it."""
    for name in BROWSERS:
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def _free_port() -> int:
    """A loopback port nothing is listening on.

    Asked for rather than fixed, so a capture cannot collide with a
    ``pony web`` the user is actually using.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve(app: Starlette, port: int) -> tuple[Server, threading.Thread]:
    """Run *app* on a background thread until it is answering."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"),
    )
    thread = threading.Thread(target=server.run, name="demo-web", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            message = "the demo server did not start"
            raise RuntimeError(message)
        time.sleep(0.05)
    return server, thread


def _shoot(
    browser: str,
    url: str,
    out: Path,
    *,
    size: tuple[int, int],
    scale: int,
    profile: Path,
) -> None:
    width, height = size
    subprocess.run(
        [
            browser,
            "--headless",
            # Never the user's own profile: a capture must not read or
            # write anything a real browsing session owns.
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            f"--window-size={width},{height}",
            f"--force-device-scale-factor={scale}",
            # Long enough for the page to fetch /keymap.json and settle.
            "--virtual-time-budget=4000",
            f"--screenshot={out}",
            url,
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )
    if not out.exists():
        message = f"{browser} wrote no file for {url}"
        raise RuntimeError(message)


def _shots(demo: DemoData) -> list[Shot]:
    """The pages worth photographing, with the ids they need resolved.

    A message is addressed by its index row id, so the list cannot be
    written down in advance: it is read back out of the store that was
    just seeded.
    """
    folder = FolderRef(account_name="Personal", folder_name="INBOX")
    summaries = demo.index.list_folder_message_summaries(folder=folder)
    by_subject = {summary.subject: summary.message_ref.id for summary in summaries}
    attachment = by_subject["Trajectory figures for the review"]
    invitation = by_subject["Invitation: Flight software design review"]
    inbox = urlencode({"account": folder.account_name, "folder": folder.folder_name})
    return [
        Shot(
            "web-mail",
            f"/message?{inbox}&{urlencode({'id': attachment})}",
        ),
        Shot(
            "web-invitation",
            f"/message?{inbox}&{urlencode({'id': invitation})}",
        ),
        Shot(
            "web-compose",
            f"/compose?{urlencode({'mode': 'reply'})}&{inbox}"
            f"&{urlencode({'id': attachment})}",
        ),
        Shot("web-contacts", "/contacts"),
        Shot(
            "web-calendar",
            # The demo's clock is fixed, so the month has to be named:
            # the live one is empty.
            f"/calendar?{urlencode({'view': 'month', 'date': NOW.date().isoformat()})}",
        ),
        Shot("web-phone", f"/mail?{inbox}", size=PHONE, scale=PHONE_SCALE),
    ]


def capture(assets: Path = ASSETS) -> Sequence[Path]:
    """Photograph every page of the browser front end into *assets*."""
    browser = find_browser()
    if browser is None:
        message = f"no browser found; install one of {', '.join(BROWSERS)}"
        raise RuntimeError(message)
    assets.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="pony-web-shots-"))
    try:
        demo = build_demo(root / "store")
        port = _free_port()
        services = WebServices(
            config=demo.config,
            index=demo.index,
            mirrors=demo.mirrors,
            contacts=demo.index,
            calendar=demo.calendar,
            # No credentials and no scheduler: nothing in a capture run
            # may reach for a server, and the Sync button is only ever
            # photographed, never pressed.
            token=None,
        )
        server, thread = _serve(build_web_app(services), port)
        print(f"Serving the demo store on http://127.0.0.1:{port}")
        written: list[Path] = []
        try:
            for shot in _shots(demo):
                out = assets / f"{shot.name}.png"
                _shoot(
                    browser,
                    f"http://127.0.0.1:{port}{shot.path}",
                    out,
                    size=shot.size,
                    scale=shot.scale,
                    profile=root / "browser",
                )
                print(f"  wrote {out.relative_to(REPO_ROOT)}")
                written.append(out)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        return written
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    if find_browser() is None:
        print(
            f"error: none of {', '.join(BROWSERS)} found on PATH",
            file=sys.stderr,
        )
        return 1
    capture()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
