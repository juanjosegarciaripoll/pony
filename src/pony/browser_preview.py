"""Hand one rendered message to the user's browser over loopback.

The terminal's ``w`` wrote :func:`pony.message_renderer.build_browser_html`
to a ``NamedTemporaryFile(delete=False)`` and opened it as a ``file://``
URL.  Two things follow from that, and both are bad:

* **The file stays.**  Nothing unlinked it, so every press left a copy of
  a plaintext message in the system temporary directory — outside the
  data directory, so ``pony reset`` does not reach it, and on a machine
  whose ``/tmp`` is a RAM-backed tmpfs it is mail resident in memory
  until the next reboot.
* **A ``file://`` document carries no headers.**  The policy the browser
  front end serves message pages under, :data:`pony.html_policy.MESSAGE_HTML_CSP`,
  did not apply to the terminal's pages at all: same bytes, no
  restrictions, and a local origin that can read local files.

Serving the document answers both at once.  Nothing is written to disk,
and the page arrives under exactly the headers ``GET /message/body``
sends.

What this is not is the browser front end.  It holds one document in
memory, imports no Starlette, and needs no ``web`` extra installed:
``http.server`` from the standard library is enough to answer one GET.
The server binds ``127.0.0.1`` on a port the kernel picks, answers the
single unguessable path it was given and refuses every other, and closes
when its window expires.  Opening a preview closes the previous one, so
at most one message is fetchable at a time and reading a second message
withdraws the first.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import urlsplit

from .html_policy import MESSAGE_HTML_HEADERS

__all__ = [
    "PREVIEW_WINDOW_SECONDS",
    "close_preview",
    "document_server",
    "serve_html",
]

# How long a served message stays fetchable.  Long enough that the
# browser has started and the reader can reload the tab; short enough
# that a message is not left answering requests for the session.
PREVIEW_WINDOW_SECONDS: Final = 120.0

# A connection that opens and then says nothing holds a worker thread.
_REQUEST_TIMEOUT: Final = 10.0


class _PreviewServer(ThreadingHTTPServer):
    """A loopback server holding exactly one document at one path."""

    daemon_threads = True

    def __init__(self, body: bytes, token_path: bytes) -> None:
        self.body = body
        self.token_path = token_path
        super().__init__(("127.0.0.1", 0), _PreviewHandler)


class _PreviewHandler(BaseHTTPRequestHandler):
    """Answers the one path, under the shared message-HTML headers."""

    protocol_version = "HTTP/1.0"
    server_version = "pony"
    sys_version = ""
    timeout = _REQUEST_TIMEOUT

    def do_GET(self) -> None:  # noqa: N802  (http.server's spelling)
        self._respond(with_body=True)

    def do_HEAD(self) -> None:  # noqa: N802  (http.server's spelling)
        self._respond(with_body=False)

    def _respond(self, *, with_body: bool) -> None:
        server = self.server
        assert isinstance(server, _PreviewServer)
        asked = urlsplit(self.path).path.encode("utf-8", "replace")
        if not secrets.compare_digest(asked, server.token_path):
            # No body and no explanation: a wrong guess learns only that
            # it was wrong.  The browser's own /favicon.ico lands here.
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(server.body)))
        for name, value in MESSAGE_HTML_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if with_body:
            self.wfile.write(server.body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Say nothing.

        ``http.server`` logs every request to ``stderr``, which is the
        terminal Textual is drawing on.
        """


class _Preview:
    """One served document: its server, its thread and its expiry."""

    def __init__(self, html: str, window: float | None) -> None:
        token = secrets.token_urlsafe(32)
        self._server = _PreviewServer(html.encode("utf-8"), f"/{token}".encode("ascii"))
        host, port = self._server.server_address[:2]
        self.url = f"http://{host!s}:{port}/{token}"
        self._closed = False
        # `None` is for a caller that closes the server itself when it is
        # done, rather than one handing the URL to a browser and walking
        # away from it.
        self._expiry = None if window is None else threading.Timer(window, self.close)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="pony-preview",
            daemon=True,
        )
        self._thread.start()
        if self._expiry is not None:
            self._expiry.daemon = True
            self._expiry.start()

    def close(self) -> None:
        """Stop answering.  Safe to call twice, and from any thread."""
        if self._closed:
            return
        self._closed = True
        if self._expiry is not None:
            self._expiry.cancel()
        self._server.shutdown()
        self._server.server_close()


_lock = threading.Lock()
_current: _Preview | None = None


def serve_html(html: str, *, window: float = PREVIEW_WINDOW_SECONDS) -> str:
    """Serve *html* on loopback and return the URL to open.

    Closes whatever was being served before, so the previous message
    stops being fetchable the moment another is opened.  Raises
    ``OSError`` if no loopback port can be bound.
    """
    global _current
    preview = _Preview(html, window)
    with _lock:
        previous, _current = _current, preview
    if previous is not None:
        previous.close()
    return preview.url


def close_preview() -> None:
    """Stop serving.  Called when the application exits."""
    global _current
    with _lock:
        preview, _current = _current, None
    if preview is not None:
        preview.close()


@contextmanager
def document_server(html: str) -> Iterator[str]:
    """Serve *html* for the duration of the block, then stop.

    Separate from the reader's preview on purpose: printing a message
    must not withdraw the window the reader already has open.  Used by
    :func:`pony.pdf_export.html_to_pdf`, which points the converter at
    the URL so the document it renders arrives under the policy instead
    of off the disk.
    """
    preview = _Preview(html, None)
    try:
        yield preview.url
    finally:
        preview.close()
