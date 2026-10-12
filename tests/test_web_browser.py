"""The browser's own half of the front end, executed in a real browser.

Everything else about ``static/pony.js`` is checked by reading it (see
``tests/test_web_dispatch.py``), because the suite has no JavaScript
engine and a dependency on one is not wanted.  Reading catches the
mismatches; it cannot catch what the code *does*.  So this file runs the
real script against the real served pages in a headless Chromium, which
needs no package — only a browser on the machine — and is therefore
**opt-in**, exactly as the live IMAP tests are:

```
PONY_BROWSER_UI=1 uv run python -m pytest tests/test_web_browser.py
```

Without the variable, or with no Chromium on ``PATH``, every test here
skips and the default run is unaffected.

How it works.  The real application is served on a loopback port, with
one extra route added *around* it — nothing in :mod:`pony.web` changes —
that returns a page holding the application in an iframe plus a driver
script.  The driver presses keys on the page, clicks its buttons, and
writes what happened into a ``<pre>``; Chromium is run with
``--dump-dom``, which prints the DOM after the scripts have run, and the
test reads the report out of it.  One launch serves the whole file.

Only ``POST /action/<name>`` is intercepted, so what the page asks for
is observable; every other request — the keymap, the refreshed list —
goes to the real server, because those are the contracts worth
exercising rather than faking.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from functools import cache
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from corpus import plain_text
from tui_helpers import (
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
    seed_message,
)

from pony.domain import FolderRef

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

INBOX = FolderRef(account_name="acct", folder_name="INBOX")
#: Where the message that tries to phish the reader is kept, so the
#: list the rest of this file drives stays three rows long.
HOSTILE = FolderRef(account_name="acct", folder_name="Hostile")


def _phishing_message() -> bytes:
    """A message whose HTML part reaches for everything it can.

    Every reach is at ``/__sink`` on the very server that is serving the
    message, so anything that got through arrives somewhere the test can
    see rather than failing on DNS.
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    message = MIMEMultipart("alternative")
    message["From"] = "security@bank.test"
    message["To"] = "you@example.com"
    message["Subject"] = "Confirm your password"
    message["Date"] = "Mon, 6 Oct 2025 09:00:00 +0000"
    message["Message-ID"] = "<browser-phish@example.com>"
    message.attach(
        MIMEText(
            "<html><head>"
            "<base href='/__sink/'>"
            "<link rel='stylesheet' href='/__sink?via=css'>"
            "<script src='/__sink?via=script'></script>"
            "</head><body>"
            "<form id='steal' action='/__sink?via=form' method='post'>"
            "<input name='pw' type='password' value='secret'>"
            "<button type='submit'>Confirm your password</button></form>"
            "<img src='/__sink?via=img'>"
            "<script>document.title = 'script ran'</script>"
            "</body></html>",
            "html",
            "utf-8",
        ),
    )
    return message.as_bytes()


#: Anything Chromium-shaped: the flags used here are its own.
BROWSERS = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


def _browser() -> str | None:
    for name in BROWSERS:
        found = shutil.which(name)
        if found is not None:
            return found
    return None


pytestmark = pytest.mark.skipif(
    not os.environ.get("PONY_BROWSER_UI") or _browser() is None,
    reason="opt-in: set PONY_BROWSER_UI=1 and have a Chromium on PATH",
)


# ----------------------------------------------------------------------
# The driver
# ----------------------------------------------------------------------

# Written against the page rather than against the script's internals:
# it presses keys, clicks buttons and reads the DOM back, which is all a
# user can do.  `pressed()` is the one exception — it is called directly,
# because the alternative is to press all ninety keys and watch what
# happens, and what is wanted is the spelling it produces.
HARNESS = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<pre id="report">pending</pre>
<iframe id="app" src="__LIST__" width="1000" height="800"></iframe>
<iframe id="reader" src="__MESSAGE__" width="1000" height="800"></iframe>
<script>
"use strict";
const KEYMAP = __KEYMAP__;
const report = { steps: [], posts: [], unmatched: [] };
const out = () => {
  document.getElementById("report").textContent = JSON.stringify(report);
};
const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

// The page is ready once it answers keys, which is later than the
// script running: the keymap arrives in a request of its own, and until
// it has, every press matches nothing and is quietly dropped.  So the
// probe is a press — `>` goes to the last row — rather than the
// presence of a function.
async function ready(frame) {
  for (let attempt = 0; attempt < 400; attempt += 1) {
    const win = frame.contentWindow;
    const doc = frame.contentDocument;
    if (typeof win.pressed === "function" && doc.querySelector("#rows .row")) {
      press(doc.body, ">");
      if (selected(doc) === doc.querySelector("#rows .row:last-child").dataset.id) {
        press(doc.body, "<");
        return;
      }
    }
    await sleep(25);
  }
  throw new Error("the page never started answering keys");
}

// Watch what the page asks the server to do, and answer as the server
// would, so the steps after an action are the real ones.
function intercept(win, answer) {
  const real = win.fetch.bind(win);
  win.fetch = async (url, options = {}) => {
    const where = String(url);
    if (!where.includes("/action/")) return real(url, options);
    report.posts.push({
      url: where,
      method: (options.method || "GET").toUpperCase(),
      body: options.body ? String(options.body) : "",
    });
    return new win.Response(JSON.stringify(answer), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
}

function press(target, key, extra = {}) {
  const win = target.ownerDocument.defaultView;
  const event = { key, bubbles: true, cancelable: true, ...extra };
  target.dispatchEvent(new win.KeyboardEvent("keydown", event));
}

function selected(doc) {
  const row = doc.querySelector("#rows .row[aria-selected='true']");
  return row ? row.dataset.id : null;
}

function marked(doc) {
  const rows = Array.from(doc.querySelectorAll("#rows .row.marked"));
  return rows.map((row) => row.dataset.id);
}

async function run() {
  const frame = document.getElementById("app");
  await ready(frame);
  const win = frame.contentWindow;
  let doc = frame.contentDocument;

  // Every key the server served, spelled back by the page's own
  // normaliser from the event a browser would deliver for it.
  for (const [surface, entries] of Object.entries(KEYMAP)) {
    for (const entry of entries) {
      const parts = entry.key.split("+");
      const base = parts.pop();
      // A capital arrives with the shift flag set and the capital in
      // `key`, which is the whole reason `shift+<letter>` cannot match.
      const capital = base.length === 1 && base !== base.toLowerCase();
      const spelling = win.pressed({
        ctrlKey: parts.includes("ctrl"),
        altKey: parts.includes("alt"),
        shiftKey: parts.includes("shift") || capital,
        key: base,
      });
      if (spelling !== entry.key) report.unmatched.push([surface, entry.key, spelling]);
    }
  }

  // Moving the cursor, and marking as it goes.
  report.cursor = [selected(doc)];
  press(doc.body, "n");
  report.cursor.push(selected(doc));
  press(doc.body, "p");
  report.cursor.push(selected(doc));
  press(doc.body, "m");
  report.marked = marked(doc);
  report.cursor.push(selected(doc));
  report.steps.push("cursor");
  out();

  // An action: what it posts, and what the page does with the answer.
  intercept(win, {
    changed: 1,
    notices: [{ text: "Archived to Archive.", severity: "information" }],
  });
  press(doc.body, "A", { shiftKey: true });
  await sleep(400);
  report.notice = doc.getElementById("notices").textContent;
  report.markedAfter = marked(doc);
  report.rowsAfter = doc.querySelectorAll("#rows .row").length;
  report.steps.push("action");
  out();

  // The same operation by tapping, for a screen with no keyboard.
  doc.querySelector("[data-action='sync']").click();
  await sleep(200);
  report.steps.push("button");

  // A key typed into a field belongs to the field.
  const field = doc.querySelector("input[type='search']");
  field.focus();
  const sofar = report.posts.length;
  press(field, "D", { shiftKey: true });
  await sleep(100);
  report.typedIntoField = report.posts.length - sofar;
  report.steps.push("typing");
  out();

  // Enter opens what the cursor is on, which is a page of its own.
  press(doc.body, "Enter");
  await sleep(600);
  report.opened = frame.contentWindow.location.pathname;
  report.steps.push("open");
  out();

  // Quitting, in the other frame, because it stops that page for good.
  const reader = document.getElementById("reader");
  await ready(reader);
  const readerWin = reader.contentWindow;
  doc = reader.contentDocument;
  intercept(readerWin, {
    changed: 1,
    notices: [{ text: "Pony Express is stopping.", severity: "information" }],
  });
  const before = report.posts.length;
  press(doc.body, "Q", { shiftKey: true });
  await sleep(600);
  report.quitPosts = report.posts.slice(before).map((post) => post.url);
  report.stopped = doc.body.dataset.stopped || "";
  report.quitNotice = doc.getElementById("notices").textContent;
  const after = report.posts.length;
  press(doc.body, "A", { shiftKey: true });
  await sleep(200);
  report.afterQuit = report.posts.length - after;
  report.steps.push("quit");
}

run()
  .then(out)
  .catch((error) => {
    report.error = String(error);
    out();
  });
</script></body></html>"""


# ----------------------------------------------------------------------
# Running it once
# ----------------------------------------------------------------------


@cache
def _report() -> dict[str, Any]:
    """Drive the front end in a browser once, and return what happened."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Mount, Route
    from starlette.testclient import TestClient

    from pony.web import WebServices, build_web_app

    paths = make_tmp_paths("browser-ux")
    account = make_test_account(paths, archive_folder="Archive")
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    for position in range(3):
        seed_message(
            index=index,
            mirror=mirrors["acct"],
            folder=INBOX,
            raw=plain_text(),
            message_id=f"<browser-{position}@example.com>",
        )
    # In a folder of its own, so the list the rest of this file counts
    # rows in stays three rows long.
    seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=HOSTILE,
        raw=_phishing_message(),
        message_id="<browser-phish@example.com>",
    )
    services = WebServices(
        config=config,
        index=index,
        mirrors=mirrors,
        contacts=index,
    )
    app = build_web_app(services)
    first = index.list_folder_message_summaries(folder=INBOX)[0].message_ref.id
    hostile = index.list_folder_message_summaries(folder=HOSTILE)[0].message_ref.id
    with TestClient(app) as client:
        keymap = client.get("/keymap.json").json()

    page = (
        HARNESS.replace("__LIST__", "/mail?account=acct&amp;folder=INBOX")
        .replace(
            "__MESSAGE__",
            f"/message?account=acct&amp;folder=INBOX&amp;id={first}",
        )
        .replace("__KEYMAP__", json.dumps(keymap))
    )

    async def harness(request: Request) -> Response:
        del request
        return HTMLResponse(page)

    #: Anything the hostile message manages to send, which must be
    #: nothing.  A route outside `pony.web`, like the harness itself.
    sink: list[str] = []

    async def collect(request: Request) -> Response:
        sink.append(request.query_params.get("via", request.method))
        return HTMLResponse("<p id='collected'>collected</p>")

    wrapper = Starlette(
        routes=[
            Route("/__harness", harness),
            Route("/__sink", collect, methods=["GET", "POST"]),
            Mount("/", app=app),
        ],
    )
    server = uvicorn.Server(
        uvicorn.Config(wrapper, host="127.0.0.1", port=0, log_level="warning"),
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    profile = Path(tempfile.mkdtemp(prefix="pony-browser-"))
    try:
        deadline = time.monotonic() + 30
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "the test server never came up"
        port = server.servers[0].sockets[0].getsockname()[1]
        dom = _dump_dom(f"http://127.0.0.1:{port}/__harness", profile)
        # The hostile message's own HTML, opened the way `w` opens it:
        # a top-level page of its own, not a frame.  It cannot be framed
        # — `frame-ancestors 'none'` says so — so this is a second run
        # against the same server rather than a third iframe above.
        hostile_dom = _dump_dom(
            f"http://127.0.0.1:{port}/message/body"
            f"?account=acct&folder=Hostile&id={hostile}",
            profile,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)

    found = re.search(r'<pre id="report">(.*?)</pre>', dom, re.DOTALL)
    assert found is not None, f"the harness reported nothing:\n{dom[:2000]}"
    report: dict[str, Any] = json.loads(unescape(found.group(1)))
    assert "error" not in report, report
    report["hostileDom"] = hostile_dom
    report["sink"] = sorted(set(sink))
    return report


def _dump_dom(url: str, profile: Path) -> str:
    """The page's DOM once its scripts have run."""
    browser = _browser()
    assert browser is not None
    finished = subprocess.run(  # noqa: S603 - a path from `shutil.which`
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            # A small /dev/shm is the usual reason headless Chromium
            # dies on a constrained machine.
            "--disable-dev-shm-usage",
            f"--user-data-dir={profile}",
            # Timers run on a budget rather than in real time, so the
            # page's own waits cost nothing and the run is bounded.
            "--virtual-time-budget=8000",
            "--dump-dom",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr[-2000:]
    return finished.stdout


def _body(post: dict[str, str]) -> dict[str, list[str]]:
    """One posted body as fields, in the order they were appended."""
    from urllib.parse import parse_qs

    return parse_qs(post["body"], keep_blank_values=True)


# ----------------------------------------------------------------------
# What the browser did
# ----------------------------------------------------------------------


def test_the_harness_got_all_the_way_through() -> None:
    """Every step ran, so the assertions below are about a real run."""
    report = _report()

    assert report["steps"] == [
        "cursor",
        "action",
        "button",
        "typing",
        "open",
        "quit",
    ]


def test_every_key_the_server_serves_is_one_the_page_can_match() -> None:
    """The matchability of the whole keymap, by executing the matcher.

    ``onKeyDown`` looks the press up by the exact string ``pressed()``
    builds, so a served key that the function cannot produce from any
    press is dead.  Here every served key is handed the event a browser
    would deliver for it, and has to come back spelled the same.
    """
    assert _report()["unmatched"] == []


def test_the_cursor_moves_and_marking_takes_the_row_with_it() -> None:
    """``n``, ``p`` and ``m`` over a real list of rows."""
    report = _report()

    first, down, up, after_mark = report["cursor"]
    assert down != first
    assert up == first
    # `m` marks the row under the cursor and moves on, as in the reader.
    assert report["marked"] == [first]
    assert after_mark == down


def test_a_key_acts_on_the_marks_and_then_clears_them() -> None:
    """The whole of one action, as the page performs it.

    What it posts, over what, and what it does with the answer: the
    notice on screen, the marks dropped, and the list refetched from the
    real server rather than the page reloading.
    """
    report = _report()
    archive = next(post for post in report["posts"] if "archive" in post["url"])

    assert archive["method"] == "POST"
    fields = _body(archive)
    assert fields["account"] == ["acct"]
    assert fields["folder"] == ["INBOX"]
    # The marked row, not the one the cursor moved on to.
    assert fields["id"] == [report["marked"][0]]
    assert "Archived to Archive." in report["notice"]
    assert report["markedAfter"] == []
    # The list came back — it was refreshed, not reloaded away.
    assert report["rowsAfter"] == 3


def test_a_button_runs_the_action_a_key_would() -> None:
    """A tap on Sync, with no keyboard involved."""
    report = _report()

    assert any(post["url"].endswith("/action/sync") for post in report["posts"])


def test_a_press_into_the_search_field_is_left_to_the_field() -> None:
    """``D`` typed into a search box must not trash anything."""
    assert _report()["typedIntoField"] == 0


def test_enter_opens_the_row_the_cursor_is_on() -> None:
    assert _report()["opened"] == "/message"


def test_quitting_stops_the_page_as_well_as_the_server() -> None:
    """The client half of ``Q``, which no amount of reading can prove.

    The page asks the server to stop, then stops being a page: it says
    so where the tab could not be closed, and it answers no further
    keys, rather than posting into a socket that has gone.
    """
    report = _report()

    assert report["quitPosts"] == ["/action/quit"]
    assert report["stopped"] == "1"
    assert "Pony Express has stopped" in report["quitNotice"]
    assert report["afterQuit"] == 0


def test_a_hostile_message_reaches_nothing_from_ponys_own_origin() -> None:
    """The policy ``/message/body`` is served under, enforced.

    The message in the fixture reaches for a script, a stylesheet, a
    remote image and a ``<base href>``, each aimed at a collector on the
    very server that served it, and carries a password form.  Reading
    the header proves what it says; this proves what Chromium does with
    it.  Weaken ``default-src`` and this test fails on the script alone.

    ``form-action`` is checked by the header assertion in
    ``tests/test_web_app.py`` instead: a form cannot be submitted
    without a click, and this page is opened as a top-level tab —
    nothing in this program ever frames it — so there is nothing above
    it to press the button.
    """
    report = _report()
    dom = report["hostileDom"]

    # Still on the message, not on the collector.
    assert "Confirm your password" in dom
    assert 'id="collected"' not in dom
    # The script did not run: the title is still the message's own.
    assert "<title>script ran</title>" not in dom
    # And nothing the message asked for ever arrived.
    assert report["sink"] == []
