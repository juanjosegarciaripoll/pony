"""The policy a message's own HTML is shown under.

The first class asserts the policy itself and runs always.  The second
loads a hostile page in a real browser and is **opt-in**, under the same
switch as the other browser tests:

```
PONY_BROWSER_UI=1 uv run python -m pytest tests/test_html_policy.py
```

It adds no dependency and uses no iframe: Chromium is run with
``--dump-dom``, which prints the DOM once the page has settled, so a
navigation that happened is visible as the other document's markup.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import unittest
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pony.browser_preview import close_preview, serve_html
from pony.html_policy import MESSAGE_HTML_CSP, MESSAGE_HTML_HEADERS

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


def _directives() -> dict[str, Sequence[str]]:
    """The policy as ``{directive: tokens}``, so a test reads one of them."""
    out: dict[str, Sequence[str]] = {}
    for directive in MESSAGE_HTML_CSP.split(";"):
        name, _, rest = directive.strip().partition(" ")
        out[name] = rest.split()
    return out


class PolicyTest(unittest.TestCase):
    def test_nothing_loads_and_nothing_executes(self) -> None:
        directives = _directives()

        self.assertEqual(directives["default-src"], ["'none'"])
        self.assertEqual(directives["img-src"], ["data:"])

    def test_the_directives_that_do_not_inherit_are_named(self) -> None:
        """CSP3 falls none of these back to ``default-src`` (finding 9)."""
        directives = _directives()

        self.assertEqual(directives["form-action"], ["'none'"])
        self.assertEqual(directives["base-uri"], ["'none'"])
        self.assertEqual(directives["frame-ancestors"], ["'none'"])

    def test_the_sandbox_gives_back_only_what_a_link_needs(self) -> None:
        """No directive covers ``<meta http-equiv="refresh">``; the sandbox does.

        Measured on Chromium 154: without ``allow-popups`` a
        ``target="_blank"`` link does nothing, and without
        ``allow-popups-to-escape-sandbox`` the site the reader clicked
        through to opens inside the sandbox, where its own scripts do
        not run and its storage raises ``SecurityError``.
        ``allow-top-navigation-by-user-activation`` governs a nested
        context navigating its ancestor, so on a top-level page it
        changes nothing and is not here.
        """
        self.assertEqual(
            _directives()["sandbox"],
            ["allow-popups", "allow-popups-to-escape-sandbox"],
        )

    def test_the_response_declines_to_be_stored(self) -> None:
        """A cached message body is a message body written to disk."""
        self.assertEqual(MESSAGE_HTML_HEADERS["Cache-Control"], "no-store")
        self.assertEqual(MESSAGE_HTML_HEADERS["Referrer-Policy"], "no-referrer")
        self.assertEqual(MESSAGE_HTML_HEADERS["X-Content-Type-Options"], "nosniff")


_LANDED = b"<!doctype html><html><body><p>PONY-LANDED</p></body></html>"


class _LandingServer(ThreadingHTTPServer):
    """Serves the landing page, and the message without the policy."""

    def __init__(self, message: bytes) -> None:
        self.message = message
        super().__init__(("127.0.0.1", 0), _Landing)


class _Landing(BaseHTTPRequestHandler):
    """Somewhere for a hijacked navigation to arrive, and be noticed."""

    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802  (http.server's spelling)
        server = self.server
        assert isinstance(server, _LandingServer)
        body = _LANDED if self.path == "/landed" else server.message
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Quiet: the test reads the DOM, not the log."""


@unittest.skipUnless(
    os.environ.get("PONY_BROWSER_UI") and _browser() is not None,
    "opt-in: set PONY_BROWSER_UI=1 and have a Chromium on PATH",
)
class MetaRefreshInARealBrowserTest(unittest.TestCase):
    """`<meta http-equiv="refresh">` is the hijack no CSP directive covers."""

    def setUp(self) -> None:
        self.addCleanup(close_preview)
        # The port has to be known before the page that refreshes to it
        # can be written, so the server is built empty and then told.
        self.server = _LandingServer(b"")
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        self.message = (
            "<!doctype html><html><head><meta http-equiv='refresh' "
            f"content='0;url=http://127.0.0.1:{port}/landed'>"
            "</head><body><p>PONY-MESSAGE</p></body></html>"
        )
        # The same bytes, reachable without the policy, for the control.
        self.server.message = self.message.encode()
        self.unguarded = f"http://127.0.0.1:{port}/unguarded"

    def _dom(self, url: str) -> str:
        browser = _browser()
        assert browser is not None
        result = subprocess.run(  # noqa: S603
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--dump-dom",
                "--virtual-time-budget=4000",
                url,
            ],
            capture_output=True,
            timeout=180,
            check=False,
        )
        return result.stdout.decode("utf-8", "replace")

    def test_the_served_message_cannot_navigate_itself_away(self) -> None:
        dom = self._dom(serve_html(self.message))

        self.assertIn("PONY-MESSAGE", dom)
        self.assertNotIn("PONY-LANDED", dom)

    def test_the_same_page_does_navigate_without_the_policy(self) -> None:
        """Guards the test itself: prove the probe can see a real hijack."""
        dom = self._dom(self.unguarded)

        self.assertIn("PONY-LANDED", dom)
        self.assertNotIn("PONY-MESSAGE", dom)
