"""The loopback preview the terminal's ``w`` key opens.

These are real sockets on ``127.0.0.1`` — the point of the module is
that a browser can fetch the page and that the headers arrive with it,
and an in-process fake would prove neither.
"""

from __future__ import annotations

import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from pony.browser_preview import close_preview, serve_html
from pony.html_policy import MESSAGE_HTML_CSP

_HTML = "<html><head><title>hello</title></head><body><p>body</p></body></html>"


def _fetch(url: str, *, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, method=method)  # noqa: S310 (loopback)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _refuses(url: str) -> bool:
    """True when nothing is answering for *url* any more.

    A connection already open when the server closes is cut rather than
    finished — withdrawing a message should take effect at once — so a
    reset counts as a refusal just as a rejected connection does.
    """
    try:
        _fetch(url)
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return True
    return False


class ServeHtmlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(close_preview)

    def test_serves_the_document_on_loopback(self) -> None:
        url = serve_html(_HTML)

        self.assertEqual(urlsplit(url).hostname, "127.0.0.1")
        status, headers, body = _fetch(url)
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), _HTML)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")

    def test_the_page_arrives_under_the_shared_policy(self) -> None:
        """The same headers `GET /message/body` sends, from one definition."""
        _status, headers, _body = _fetch(serve_html(_HTML))

        self.assertEqual(headers["Content-Security-Policy"], MESSAGE_HTML_CSP)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        # Without this the browser's cache may write the message to disk,
        # which is the leak serving was meant to close.
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_the_path_is_unguessable(self) -> None:
        url = serve_html(_HTML)
        parts = urlsplit(url)
        self.assertGreaterEqual(len(parts.path.lstrip("/")), 32)

    def test_any_other_path_gets_nothing(self) -> None:
        """A wrong guess — or the browser's own favicon — learns nothing."""
        parts = urlsplit(serve_html(_HTML))
        root = f"http://{parts.netloc}"

        for path in ("/", "/favicon.ico", parts.path + "x", "/message/body"):
            with self.subTest(path=path):
                status, _headers, body = _fetch(root + path)
                self.assertEqual(status, 404)
                self.assertEqual(body, b"")

    def test_head_answers_without_the_body(self) -> None:
        status, headers, body = _fetch(serve_html(_HTML), method="HEAD")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(_HTML.encode())))
        self.assertEqual(body, b"")

    def test_reading_a_second_message_withdraws_the_first(self) -> None:
        first = serve_html(_HTML)
        second = serve_html("<html><body>another</body></html>")

        self.assertTrue(_refuses(first))
        self.assertEqual(_fetch(second)[2], b"<html><body>another</body></html>")

    def test_the_page_stays_fetchable_so_a_reload_works(self) -> None:
        url = serve_html(_HTML)

        self.assertEqual(_fetch(url)[0], 200)
        self.assertEqual(_fetch(url)[0], 200)

    def test_closing_stops_answering(self) -> None:
        url = serve_html(_HTML)

        close_preview()

        self.assertTrue(_refuses(url))

    def test_closing_twice_is_harmless(self) -> None:
        serve_html(_HTML)

        close_preview()
        close_preview()

    def test_the_window_expires_on_its_own(self) -> None:
        """Nothing is left answering for the life of the session."""
        url = serve_html(_HTML, window=0.1)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _refuses(url):
            time.sleep(0.05)

        self.assertTrue(_refuses(url))
