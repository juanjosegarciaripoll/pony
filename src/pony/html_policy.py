"""The policy a message's own HTML is shown under.

Both front ends show the HTML part of a message as a page of its own —
the browser at ``GET /message/body``, the terminal through
:mod:`pony.browser_preview` — and the restrictions have to be the same
in both, so they live here instead of in either transport.  A constant
in one of them is a constant the other does not have: that is how the
terminal came to open the identical bytes at a ``file://`` URL, where no
header applies at all.

Nothing here imports Starlette or Textual.  See
:func:`pony.message_renderer.build_browser_html` for what produces the
document these headers accompany.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

# ``default-src 'none'`` covers everything that falls back to it:
# scripts, frames, plugins, fetches, fonts, remote images.  Three
# directives do **not** fall back to it in CSP3 and so have to be named,
# which is how a ``<form action=…><input name=pw>`` in a `text/html`
# message came to submit successfully from Pony's own origin:
#
# * ``form-action`` — nothing a message renders may post anywhere, so a
#   "re-enter your password" form has nowhere to send it.
# * ``base-uri`` — a ``<base href>`` would otherwise re-point every
#   relative URL on the page at the sender's server.
# * ``frame-ancestors`` — no page may embed this one.
#
# ``sandbox`` is what stops a ``<meta http-equiv="refresh">``.  No other
# directive covers one: it is not a fetch, so ``default-src`` does not
# apply, and CSP3 dropped ``navigate-to``.  Two tokens are given back,
# and measuring Chromium 154 says exactly these two and no more:
#
# * ``allow-popups`` — without it a ``target="_blank"`` link does
#   nothing at all, and in a newsletter that is most of the links.
# * ``allow-popups-to-escape-sandbox`` — without it the site the reader
#   clicked through to opens *inside* the sandbox: its own scripts do
#   not run and its storage raises ``SecurityError``, so a real page
#   arrives broken.
#
# ``allow-top-navigation-by-user-activation`` is **not** here.  It
# governs whether a nested context may navigate its ancestor, and this
# is a top-level page with no ancestor; ordinary same-tab links work
# without it, and adding it changed nothing that could be measured.
#
# What the sandbox costs is nothing visible: inline styles still apply
# and ``data:`` images still load, both measured.
MESSAGE_HTML_CSP: Final = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "sandbox allow-popups allow-popups-to-escape-sandbox"
)

# Sent with the document by both transports.
#
# ``no-store`` is not decoration.  A message body is plaintext mail, and
# an ordinary HTTP cache writes what it caches to disk — in the
# browser's profile, where neither `pony reset` nor the data directory
# reaches.  Serving the page instead of writing a temporary file only
# keeps mail off the disk if the response also declines to be stored.
MESSAGE_HTML_HEADERS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "Content-Security-Policy": MESSAGE_HTML_CSP,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
)
