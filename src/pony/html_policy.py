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
# Deliberately **no** ``sandbox``: this is a top-level page the reader
# opened in a tab of its own, and a sandbox that withholds
# ``allow-top-navigation`` kills every ordinary link in the message —
# which is most of what a reader wants from the HTML version — while
# granting it back reinstates the one thing a sandbox was wanted for.
# So a ``<meta http-equiv="refresh">`` in a message can still take that
# tab somewhere else; it is the tab the user opened for the message, it
# carries no referrer, and nothing of Pony's is in it.
MESSAGE_HTML_CSP: Final = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
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
