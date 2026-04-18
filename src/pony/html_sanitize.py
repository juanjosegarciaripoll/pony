"""HTML sanitization helpers for text extraction.

Shared by body-preview indexing (``message_projection``) and the TUI
display renderer (``tui.message_renderer``).  Regex-based on purpose:
projection is a hot path and must stay dependency-free.
"""

from __future__ import annotations

import html as _html
import re

# Comments must be stripped *before* any tag-level processing because
# Outlook conditional comments (``<!--[if mso]>...<![endif]-->``) embed
# a ``>`` inside the opening delimiter.  A naive ``<[^>]+>`` tag regex
# would close on that ``>`` and leak the conditional body as visible
# text.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEAD_RE = re.compile(
    r"<head[^>]*>.*?</head>", re.IGNORECASE | re.DOTALL,
)
_STYLE_RE = re.compile(
    r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL,
)
_SCRIPT_RE = re.compile(
    r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL,
)
_NOSCRIPT_RE = re.compile(
    r"<noscript[^>]*>.*?</noscript>", re.IGNORECASE | re.DOTALL,
)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_invisible_blocks(html: str) -> str:
    """Remove elements that carry no visible text.

    Covers comments, ``<head>``, ``<style>``, ``<script>`` and
    ``<noscript>``.  Leaves the rest of the markup untouched so a
    downstream parser can still see structural tags.
    """
    html = _COMMENT_RE.sub(" ", html)
    html = _HEAD_RE.sub(" ", html)
    html = _STYLE_RE.sub(" ", html)
    html = _SCRIPT_RE.sub(" ", html)
    html = _NOSCRIPT_RE.sub(" ", html)
    return html


def html_to_preview_text(html: str) -> str:
    """Convert an HTML fragment to a plain-text preview string.

    Not a structural renderer — block boundaries are collapsed to a
    single space.  Use the ``HTMLParser``-based path in
    ``tui.message_renderer`` when line structure must be preserved.
    """
    text = strip_invisible_blocks(html)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    return " ".join(text.split())
