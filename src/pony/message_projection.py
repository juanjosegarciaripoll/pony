"""Projection of RFC 5322 messages into indexed metadata.

Uses fast regex-based header extraction instead of the full email parser
for performance.  The full parser is only used when the message is
actually displayed in the TUI (via ``message_renderer.py``).
"""

from __future__ import annotations

import base64
import email
import email.policy
import quopri
import re
from datetime import UTC, datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime

from .domain import IndexedMessage, MessageRef, MessageStatus
from .html_sanitize import html_to_preview_text

# Regex to split headers from body at the first blank line.
_HEADER_BODY_RE = re.compile(rb"\r?\n\r?\n", re.MULTILINE)

# Header extraction: handles continuation lines (leading whitespace).
# Use ``[ \t]*`` (not ``\s*``) after the colon so an empty header value
# like ``Subject:\r\n`` does not slurp the next line — otherwise ``Subject``
# would capture the following ``Date:`` line as its value.
_HEADER_RE = re.compile(
    rb"^([\w-]+):[ \t]*(.*(?:\r?\n[ \t]+.*)*)",
    re.MULTILINE | re.IGNORECASE,
)

# One MIME part's Content-Type and Content-Disposition, read off its own
# unfolded header block rather than from anywhere in the message.
_CONTENT_TYPE_HEADER_RE = re.compile(
    rb"^Content-Type:[ \t]*([^\r\n]*)", re.MULTILINE | re.IGNORECASE
)
_CONTENT_DISPOSITION_HEADER_RE = re.compile(
    rb"^Content-Disposition:[ \t]*([^\r\n]*)", re.MULTILINE | re.IGNORECASE
)

# A ``filename`` parameter of a Content-Disposition, or the older
# ``name`` of a Content-Type: between them they are what
# ``EmailMessage.get_filename`` reads.
_FILENAME_PARAM_RE = re.compile(rb"(?:^|[;\s])(?:file)?name[ \t]*=", re.IGNORECASE)

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
    has_attachments = _detect_attachments(headers, raw_message)
    body_text = _extract_body_text(body, raw_message)
    message_id_raw = header_map.get(b"message-id", b"")
    message_id = " ".join(message_id_raw.decode("ascii", errors="replace").split())
    if not message_id:
        # Exchange sometimes folds the Message-ID value onto a bare (non-indented)
        # continuation line, which the regex parser misses.  Fall back to the
        # stdlib parser — only for this field to keep the fast path intact.
        _fb = email.message_from_bytes(raw_message, policy=email.policy.compat32)
        message_id = (_fb.get("Message-ID") or "").strip()

    return IndexedMessage(
        message_ref=message_ref,
        message_id=message_id,
        sender=sender,
        recipients=recipients,
        cc=cc,
        subject=subject,
        body_text=body_text,
        storage_key=storage_key,
        local_flags=frozenset(),
        base_flags=frozenset(),
        local_status=MessageStatus.ACTIVE,
        received_at=received_at,
        has_attachments=has_attachments,
    )


def _detect_attachments(headers: bytes, raw_message: bytes) -> bool:
    """Does this message carry an attachment, per part rather than per message?

    The rule has to be the one the reader applies, because it is the
    numbering the whole program exposes: ``message_renderer``'s
    :func:`~pony.message_renderer.is_attachment_part` counts a part that
    says it is an attachment, one that carries a filename, *or* one that
    is neither plain text nor HTML and so cannot be body.  This used to
    be a single regex searched over the whole raw message with no
    counterpart for the third clause, so a ``multipart/mixed`` carrying
    a bare ``image/png`` with no disposition indexed as having no
    attachment while the reader offered ``[1] attachment.png`` -- and
    the clause it did have matched ``filename=`` anywhere at all,
    including inside a body.

    The full parser stays out of it: building an
    ``email.message.Message`` to index one flag is what made the first
    rescan of a large account take minutes.
    """
    if b"multipart" not in headers.lower():
        return _part_is_attachment(headers)
    for part in _BOUNDARY_RE.split(raw_message)[1:]:
        separator = _HEADER_BODY_RE.search(part)
        part_headers = part[: separator.start()] if separator else part
        if _part_is_attachment(part_headers):
            return True
    return False


def _part_is_attachment(part_headers: bytes) -> bool:
    """One part's half of :func:`_detect_attachments`."""
    unfolded = _CONTINUATION_RE.sub(b" ", part_headers)
    disposition_match = _CONTENT_DISPOSITION_HEADER_RE.search(unfolded)
    disposition = disposition_match.group(1).strip() if disposition_match else b""
    if disposition.split(b";")[0].strip().lower() == b"attachment":
        return True
    type_match = _CONTENT_TYPE_HEADER_RE.search(unfolded)
    content_type_value = type_match.group(1).strip() if type_match else b""
    if _FILENAME_PARAM_RE.search(disposition) or _FILENAME_PARAM_RE.search(
        content_type_value
    ):
        return True
    content_type = content_type_value.split(b";")[0].strip().lower()
    if not content_type or content_type.startswith(b"multipart/"):
        # No Content-Type at all means text/plain, which is body; a
        # multipart part is a container whose children are walked.
        return False
    return content_type not in (b"text/plain", b"text/html")


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
            try:
                parts.append(fragment.decode(enc, errors="replace"))
            except LookupError:
                # Unknown/bogus charset label (e.g. "x-unknown" from a
                # malformed RFC 2047 encoded-word).  latin-1 is total —
                # every byte maps to a code point — so we get a lossless
                # best-effort string instead of aborting the whole import.
                parts.append(fragment.decode("latin-1"))
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
    rb"Content-Transfer-Encoding:\s*(\S+)",
    re.IGNORECASE,
)

# The ``charset`` parameter of a Content-Type, quoted or bare.  Read off
# the raw header block for the same reason the CTE is: this projection
# never builds an ``email.message.Message``, because parsing a whole
# message to index one preview line is what made the first rescan of a
# large account take minutes.
_CHARSET_RE = re.compile(
    rb'charset\s*=\s*"?([A-Za-z0-9_.:+-]+)"?',
    re.IGNORECASE,
)

# Safety cap applied to the final collapsed body text.  Protects against
# pathological emails (e.g. multi-megabyte log dumps pasted into a message)
# without silently truncating ordinary mail.
_MAX_BODY_TEXT_BYTES = 256 * 1024


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

    return _decode_text(decoded, part_headers)


def _decode_text(decoded: bytes, part_headers: bytes) -> str:
    """*decoded* as text, in the charset the part says it is in.

    Reading an ``iso-8859-1`` message as UTF-8 does not merely make the
    list column ugly (``Ni?o caf?`` where the reader shows
    ``Niño café``): ``body_text`` is what the full-text index is built
    from, so the message becomes unfindable by its own words.  Every
    other decoder in the program consults ``get_content_charset`` --
    :mod:`pony.message_renderer`, :mod:`pony.compose_utils` -- and this
    is the same rule applied to headers that have not been through
    ``email.parser``.

    A charset the codec registry does not know, or one the bytes do not
    honour, falls back to UTF-8, which is also what a part declaring no
    charset at all gets.  NUL is dropped either way: it is legal in a
    SQLite text value and truncates almost everything that later reads
    one.
    """
    match = _CHARSET_RE.search(part_headers)
    if match is not None:
        charset = match.group(1).decode("ascii", errors="replace")
        try:
            return decoded.decode(charset, errors="replace").replace("\x00", "")
        except LookupError:
            pass
    return decoded.decode("utf-8", errors="replace").replace("\x00", "")


def _extract_body_text(body: bytes, raw_message: bytes) -> str:
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
            text = html_to_preview_text(text)
        return _cap_body_text(_collapse_whitespace(text))

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
            html_preview = html_to_preview_text(raw_html)

    result = text_preview or html_preview
    return _cap_body_text(_collapse_whitespace(result))


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _cap_body_text(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_BODY_TEXT_BYTES:
        return value
    return encoded[:_MAX_BODY_TEXT_BYTES].decode("utf-8", errors="ignore")
