"""Duplicate RFC 5322 bytes, optionally assigning a fresh Message-ID.

The TUI copy action uses this when the user asks Pony to place a copy
of an existing message in a different folder.  Two regimes are
supported:

- ``rewrite_message_id=True`` (same-account copies): the original
  ``Message-ID`` header is replaced with a synthetic
  ``<pony-copy-*@pony.local>`` id.  This is required because the sync
  planner keys cross-folder identity on ``Message-ID`` — without the
  rewrite, the new ``uid=NULL`` row in the target folder would be
  interpreted as a *move* (source → target) instead of a copy, and the
  source would vanish from the server.

- ``rewrite_message_id=False`` (cross-account copies): the original
  ``Message-ID`` is preserved.  Accounts are independent identity
  namespaces — the planner never compares MIDs across accounts — so a
  true copy is safe and keeps IMAP thread integrity intact.

Byte-level, regex-based rewrite.  We deliberately do not round-trip the
message through :mod:`email` because that would re-encode headers and
bodies (folding, charset normalisation, transfer-encoding) — we want a
faithful byte-for-byte copy with exactly one header swapped.
"""

from __future__ import annotations

import re
from uuid import uuid4

# Match a full Message-ID header line, including any RFC 5322 continuation
# lines (those starting with whitespace).  Capture group 1 is the final EOL
# so we can preserve whatever line-ending style the source uses (\n or \r\n).
_MESSAGE_ID_LINE_RE = re.compile(
    rb"(?im)^Message-ID:[^\r\n]*(?:\r?\n[ \t][^\r\n]*)*(\r?\n)",
)


def _detect_eol(raw: bytes) -> bytes:
    """Return ``\\r\\n`` when the message uses CRLF headers, else ``\\n``."""
    # A quick peek at the start is enough — every RFC 5322 message has at
    # least a couple of header lines before the body, and they all use the
    # same EOL style.
    return b"\r\n" if b"\r\n" in raw[:200] else b"\n"


def copy_message_bytes(raw: bytes, *, rewrite_message_id: bool) -> tuple[bytes, str]:
    """Return ``(new_raw, message_id)``.

    When *rewrite_message_id* is ``True`` the existing ``Message-ID``
    header is replaced with a fresh ``<pony-copy-*@pony.local>`` id.

    When *rewrite_message_id* is ``False`` the bytes are returned
    unchanged and the existing ``Message-ID`` is extracted.  If the
    source has no ``Message-ID`` we fall back to the rewrite path and
    generate one anyway — the index requires a non-empty id.
    """
    match = _MESSAGE_ID_LINE_RE.search(raw)

    if rewrite_message_id or match is None:
        new_mid = f"<pony-copy-{uuid4().hex}@pony.local>"
        replacement = f"Message-ID: {new_mid}".encode("ascii")
        if match is not None:
            eol = match.group(1)
            new_raw = raw[: match.start()] + replacement + eol + raw[match.end() :]
        else:
            eol = _detect_eol(raw)
            new_raw = replacement + eol + raw
        return new_raw, new_mid

    # Preserve path: pull the MID out of the matched line verbatim.
    line = match.group(0).decode("ascii", errors="replace")
    mid = line.split(":", 1)[1].strip()
    return raw, mid
