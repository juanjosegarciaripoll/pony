"""Projection of RFC 5322 messages into indexed metadata.

Uses fast regex-based header extraction instead of the full email parser
for performance.  The full parser is only used when the message is
actually displayed in the TUI (via ``message_renderer.py``).
"""

from __future__ import annotations

import base64
import quopri
import re
from datetime import UTC, datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime

from .domain import IndexedMessage, MessageRef, MessageStatus

# Regex to split headers from body at the first blank line.
_HEADER_BODY_RE = re.compile(rb"\r?\n\r?\n", re.MULTILINE)

# Header extraction: handles continuation lines (leading whitespace).
_HEADER_RE = re.compile(
    rb"^([\w-]+):\s*(.*(?:\r?\n[ \t]+.*)*)",
    re.MULTILINE | re.IGNORECASE,
)

# Detect attachments: Content-Disposition: attachment or filename=
_ATTACHMENT_RE = re.compile(
    rb"Content-Disposition:\s*attachment|filename\s*=",
    re.IGNORECASE,
)

# Detect Content-Type: text/plain parts.
_TEXT_PLAIN_RE = re.compile(
    rb"Content-Type:\s*text/plain",
    re.IGNORECASE,
)

# Unfold RFC 5322 continuation lines (leading whitespace after CRLF).
_CONTINUATION_RE = re.compile(rb"\r?\n[ \t]+")

# MIME boundary separator (splits multipart messages into parts).
_BOUNDARY_RE = re.compile(rb"\r?\n--[^\r\n]+\r?\n?")


def project_rfc822_message(
    *,
    message_ref: MessageRef,
    raw_message: bytes,
    storage_key: str,
) -> IndexedMessage:
    """Extract indexable metadata from raw RFC 5322 bytes.

    This is a fast path that avoids the full email parser.  It extracts
    headers via regex and does a minimal scan for attachments and body
    preview text.
    """
    headers, body = _split_headers_body(raw_message)
    header_map = _parse_headers(headers)

    sender = _decode_header(header_map.get(b"from", b""))
    recipients = _decode_header(header_map.get(b"to", b""))
    cc = _decode_header(header_map.get(b"cc", b""))
    subject = _decode_header(header_map.get(b"subject", b""))
    received_at = _parse_date(header_map.get(b"date", b""))
    has_attachments = bool(_ATTACHMENT_RE.search(raw_message))
    body_preview = _extract_body_preview(body, raw_message)

    return IndexedMessage(
        message_ref=message_ref,
        sender=sender,
        recipients=recipients,
        cc=cc,
        subject=subject,
        body_preview=body_preview,
        storage_key=storage_key,
        local_flags=frozenset(),
        base_flags=frozenset(),
        local_status=MessageStatus.ACTIVE,
        received_at=received_at,
        has_attachments=has_attachments,
    )


def _split_headers_body(raw: bytes) -> tuple[bytes, bytes]:
    """Split raw message into headers and body at the first blank line."""
    match = _HEADER_BODY_RE.search(raw)
    if match is None:
        return raw, b""
    return raw[: match.start()], raw[match.end() :]


def _parse_headers(header_block: bytes) -> dict[bytes, bytes]:
    """Extract headers into a dict (lowercase key -> raw value bytes).

    Only the first occurrence of each header is kept.
    """
    result: dict[bytes, bytes] = {}
    for match in _HEADER_RE.finditer(header_block):
        name = match.group(1).lower()
        if name not in result:
            # Unfold continuation lines.
            value = _CONTINUATION_RE.sub(b" ", match.group(2)).strip()
            result[name] = value
    return result


def _decode_header(raw: bytes) -> str:
    """Decode a header value, handling RFC 2047 encoded words."""
    if not raw:
        return ""
    text = raw.decode("ascii", errors="replace")
    # Fast path: no encoded words.
    if "=?" not in text:
        return _collapse_whitespace(text)
    # Use the stdlib decoder for RFC 2047.
    parts: list[str] = []
    for fragment, charset in decode_header(text):
        if isinstance(fragment, bytes):
            enc = charset or "utf-8"
            parts.append(fragment.decode(enc, errors="replace"))
        else:
            parts.append(fragment)
    return _collapse_whitespace(" ".join(parts))


def _parse_date(raw: bytes) -> datetime:
    """Parse a Date header value to a UTC datetime."""
    if not raw:
        return datetime.now(tz=UTC)
    try:
        text = raw.decode("ascii", errors="replace")
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return datetime.now(tz=UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_CTE_RE = re.compile(
    rb"Content-Transfer-Encoding:\s*(\S+)", re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _decode_part_body(part_headers: bytes, raw_body: bytes) -> str:
    """Decode a MIME part body based on its Content-Transfer-Encoding."""
    cte_match = _CTE_RE.search(part_headers)
    encoding = cte_match.group(1).lower() if cte_match else b"7bit"

    if encoding == b"base64":
        try:
            decoded = base64.b64decode(raw_body)
        except Exception:  # noqa: BLE001
            decoded = raw_body
    elif encoding == b"quoted-printable":
        decoded = quopri.decodestring(raw_body)
    else:
        decoded = raw_body

    return decoded[:4000].decode("utf-8", errors="replace")


def _extract_body_preview(body: bytes, raw_message: bytes) -> str:
    """Extract a short plain-text preview for indexing.

    For simple text/plain messages, decodes the body directly.
    For multipart messages, scans for the first text/plain part.
    Falls back to HTML with tags stripped, then empty string.
    """
    if not body:
        return ""

    # Simple non-multipart: check top-level Content-Type in headers.
    hdr_end = _HEADER_BODY_RE.search(raw_message)
    header_block = raw_message[: hdr_end.start()] if hdr_end else raw_message
    is_multipart = b"multipart" in header_block.lower()

    if not is_multipart:
        text = _decode_part_body(header_block, body)
        # Strip HTML tags if it's an HTML-only message.
        if b"text/html" in header_block.lower():
            text = _HTML_TAG_RE.sub(" ", text)
        return _collapse_whitespace(text)

    # Multipart: split on boundaries and find text/plain.
    parts = _BOUNDARY_RE.split(raw_message)
    text_preview = ""
    html_preview = ""
    for part in parts[1:]:  # skip preamble
        m = _HEADER_BODY_RE.search(part)
        if m is None:
            continue
        part_hdr = part[: m.start()]
        part_body = part[m.end() :]
        if _TEXT_PLAIN_RE.search(part_hdr):
            text_preview = _decode_part_body(part_hdr, part_body)
            break
        if b"text/html" in part_hdr.lower() and not html_preview:
            raw_html = _decode_part_body(part_hdr, part_body)
            html_preview = _HTML_TAG_RE.sub(" ", raw_html)

    result = text_preview or html_preview
    return _collapse_whitespace(result)


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())
