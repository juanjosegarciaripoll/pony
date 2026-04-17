"""Render RFC 5322 message bytes for display in the TUI.

Public surface:
- ``RenderedMessage``  — plain-text body + attachment metadata
- ``render_message``   — parse raw bytes → RenderedMessage
"""

from __future__ import annotations

import base64
import email
import email.policy
import html as _html
import re
from dataclasses import dataclass
from email.message import EmailMessage
from html.parser import HTMLParser

_STYLE_BLOCK_RE = re.compile(
    r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL,
)
_SCRIPT_BLOCK_RE = re.compile(
    r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """Metadata for one attachment part."""

    index: int          # 1-based display number
    filename: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """Everything the message view needs to display one message."""

    subject: str
    from_: str
    to: str
    cc: str
    date: str
    body: str                           # plain text, always
    attachments: tuple[AttachmentInfo, ...]
    raw_bytes: bytes                    # kept for W (open in browser)


def render_message(raw_bytes: bytes) -> RenderedMessage:
    """Parse *raw_bytes* and return a ``RenderedMessage`` ready for display."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    subject = _header(msg, "Subject")
    from_ = _header(msg, "From")
    to = _header(msg, "To")
    cc = _header(msg, "Cc")
    date = _header(msg, "Date")

    body, attachments = _extract_body_and_attachments(msg)

    return RenderedMessage(
        subject=subject,
        from_=from_,
        to=to,
        cc=cc,
        date=date,
        body=body,
        attachments=tuple(attachments),
        raw_bytes=raw_bytes,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _header(msg: EmailMessage, name: str) -> str:
    value = msg.get(name, "")
    return str(value).strip()


def _extract_body_and_attachments(
    msg: EmailMessage,
) -> tuple[str, list[AttachmentInfo]]:
    """Walk the MIME tree and collect the best plain-text body + attachments.

    ``message/rfc822`` parts (attached emails) are rendered as a header
    block separator in the body and listed as named attachments.  Their
    inner text and attachments are included naturally by the walk.
    """
    body_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentInfo] = []
    attach_index = 1
    # Track which parts are inside a message/rfc822 so we can skip
    # collecting HTML for inner messages (we already get their plain text).
    in_nested = 0

    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = part.get_content_disposition() or ""

        # Detect attached emails and render a header separator.
        if content_type == "message/rfc822":
            inner = part.get_payload()
            if isinstance(inner, list) and len(inner) > 0:
                inner_msg = inner[0]
            else:
                inner_msg = inner
            if isinstance(inner_msg, EmailMessage):
                subj = _header(inner_msg, "Subject") or "(no subject)"
                frm = _header(inner_msg, "From")
                date = _header(inner_msg, "Date")
                sep = (
                    f"\n{'─' * 60}\n"
                    f"  Attached email: {subj}\n"
                    f"  From: {frm}\n"
                )
                if date:
                    sep += f"  Date: {date}\n"
                sep += f"{'─' * 60}\n"
                body_parts.append(sep)

                # List the attached email itself as an attachment.
                raw = inner_msg.as_bytes()
                attachments.append(
                    AttachmentInfo(
                        index=attach_index,
                        filename=f"{subj}.eml",
                        content_type="message/rfc822",
                        size_bytes=len(raw),
                    )
                )
                attach_index += 1
            in_nested += 1
            continue

        # Skip multipart containers — we process their children.
        if part.get_content_maintype() == "multipart":
            continue

        if disposition == "attachment" or part.get_filename():
            filename = part.get_filename() or "(unnamed)"
            payload = part.get_payload(decode=True)
            size = len(payload) if isinstance(payload, bytes) else 0
            attachments.append(
                AttachmentInfo(
                    index=attach_index,
                    filename=filename,
                    content_type=content_type,
                    size_bytes=size,
                )
            )
            attach_index += 1
            continue

        if content_type == "text/plain":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                body_parts.append(
                    payload.decode(charset, errors="replace")
                )

        elif content_type == "text/html" and not body_parts and not in_nested:
            # Only collect HTML if we have no plain text yet and we're
            # not inside a nested message.
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                html_parts.append(
                    payload.decode(charset, errors="replace")
                )

    if body_parts:
        body = "\n".join(body_parts)
    elif html_parts:
        body = _strip_html("\n".join(html_parts))
    else:
        body = "(no readable content)"

    return body, attachments


class _HTMLStripper(HTMLParser):
    """Minimal tag stripper that preserves line structure."""

    def __init__(self) -> None:
        super().__init__()
        self._lines: list[str] = []
        self._current: list[str] = []

    def handle_data(self, data: str) -> None:
        self._current.append(data)

    def handle_starttag(self, tag: str, attrs: object) -> None:  # noqa: ARG002
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._flush()

    def _flush(self) -> None:
        text = "".join(self._current).strip()
        if text:
            self._lines.append(text)
        self._current.clear()

    def result(self) -> str:
        self._flush()
        return "\n".join(self._lines)


def build_browser_html(raw_bytes: bytes) -> str:
    """Build a self-contained HTML page from a raw RFC 5322 message.

    - Headers are rendered in a styled table at the top.
    - The HTML body part is preferred; plain text is wrapped in ``<pre>``.
    - ``cid:`` image references are replaced with base64 data URIs so the
      file is fully self-contained (no external requests needed).
    - Attachments are listed at the bottom.
    """
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    subject = _header(msg, "Subject")
    from_   = _header(msg, "From")
    to      = _header(msg, "To")
    cc      = _header(msg, "Cc")
    date    = _header(msg, "Date")

    html_body: str | None = None
    plain_body: str | None = None
    cid_map: dict[str, str] = {}        # bare Content-ID → data URI
    attachments: list[AttachmentInfo] = []
    attach_index = 1

    nested_headers: list[str] = []  # HTML blocks for attached emails

    for part in msg.walk():
        content_type = part.get_content_type()

        # Detect attached emails and build a header separator.
        if content_type == "message/rfc822":
            inner = part.get_payload()
            if isinstance(inner, list) and len(inner) > 0:
                inner_msg = inner[0]
            else:
                inner_msg = inner
            if isinstance(inner_msg, EmailMessage):
                subj = _header(inner_msg, "Subject") or "(no subject)"
                frm = _header(inner_msg, "From")
                dt = _header(inner_msg, "Date")
                raw_inner = inner_msg.as_bytes()
                nested_headers.append(
                    f"<div class='nested-header'>"
                    f"<strong>Attached email:</strong> "
                    f"{_html.escape(subj)}<br>"
                    f"From: {_html.escape(frm)}"
                    + (f"<br>Date: {_html.escape(dt)}" if dt else "")
                    + "</div>"
                )
                attachments.append(AttachmentInfo(
                    index=attach_index,
                    filename=f"{subj}.eml",
                    content_type="message/rfc822",
                    size_bytes=len(raw_inner),
                ))
                attach_index += 1
            continue

        if part.get_content_maintype() == "multipart":
            continue

        disposition  = part.get_content_disposition() or ""
        filename     = part.get_filename()
        content_id   = part.get("Content-ID", "").strip().strip("<>")

        payload = part.get_payload(decode=True)

        # Build CID map for inline parts (images embedded in HTML).
        if content_id and isinstance(payload, bytes):
            b64 = base64.b64encode(payload).decode("ascii")
            cid_map[content_id] = f"data:{content_type};base64,{b64}"

        # Real attachments go to the list.
        if disposition == "attachment" or (filename and disposition != "inline"):
            size = len(payload) if isinstance(payload, bytes) else 0
            attachments.append(AttachmentInfo(
                index=attach_index,
                filename=filename or "(unnamed)",
                content_type=content_type,
                size_bytes=size,
            ))
            attach_index += 1
            continue

        if content_type == "text/html" and html_body is None:
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                html_body = payload.decode(charset, errors="replace")
        elif (
            content_type == "text/plain"
            and plain_body is None
            and isinstance(payload, bytes)
        ):
            charset = part.get_content_charset() or "utf-8"
            plain_body = payload.decode(charset, errors="replace")

    # Resolve cid: references in the HTML body.
    if html_body is not None:
        def _replace_cid(m: re.Match[str]) -> str:
            quote, cid, end = m.group(1), m.group(2), m.group(3)
            return quote + cid_map.get(cid, f"cid:{cid}") + end

        html_body = re.sub(
            r'(["\'])cid:([^"\']+)(["\'])',
            _replace_cid,
            html_body,
            flags=re.IGNORECASE,
        )
        # If the part is a full document, extract just its <body> content so
        # we can inject our own header into a single wrapper page.
        body_match = re.search(
            r"<body[^>]*>(.*)</body>", html_body, re.IGNORECASE | re.DOTALL
        )
        body_content = body_match.group(1) if body_match else html_body

        # Carry over any <style> blocks from the original <head>.
        extra_styles = "".join(
            m.group(0)
            for m in _STYLE_BLOCK_RE.finditer(html_body)
        )
    elif plain_body is not None:
        body_content  = (
            "<pre style='white-space:pre-wrap;font-family:monospace'>"
            + _html.escape(plain_body)
            + "</pre>"
        )
        extra_styles = ""
    else:
        body_content = "<p><em>(no readable content)</em></p>"
        extra_styles = ""

    # Inject nested email headers into the body.
    if nested_headers:
        body_content += "\n".join(nested_headers)

    # Header block.
    cc_row = (
        f"<tr><td>Cc</td><td>{_html.escape(cc)}</td></tr>" if cc else ""
    )
    header_block = f"""
<table class="headers">
  <tr><td>From</td><td>{_html.escape(from_)}</td></tr>
  <tr><td>To</td><td>{_html.escape(to)}</td></tr>
  {cc_row}
  <tr><td>Date</td><td>{_html.escape(date)}</td></tr>
  <tr><td>Subject</td><td><strong>{_html.escape(subject)}</strong></td></tr>
</table>
<hr class="header-sep">
"""

    # Attachment list.
    if attachments:
        items = "".join(
            f"<li>{_html.escape(a.filename)}"
            f" <span class='att-meta'>({_html.escape(a.content_type)},"
            f" {fmt_size(a.size_bytes)})</span></li>"
            for a in attachments
        )
        att_block = (
            f"<div class='attachments'><strong>Attachments:</strong>"
            f"<ul>{items}</ul></div>"
        )
    else:
        att_block = ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body{{font-family:sans-serif;margin:0;padding:0;color:#222}}
table.headers{{border-collapse:collapse;width:100%;background:#f5f5f5;
              font-size:.9em;padding:.6em}}
table.headers td{{padding:3px 10px;vertical-align:top}}
table.headers td:first-child{{font-weight:bold;color:#555;
                              white-space:nowrap;width:5em}}
hr.header-sep{{border:none;border-top:1px solid #ccc;margin:0}}
.body-wrap{{padding:1em}}
.attachments{{padding:.6em 1em;background:#fffbe6;
             border-top:1px solid #e0d080;font-size:.9em}}
.attachments ul{{margin:.3em 0 0 1.2em;padding:0}}
.att-meta{{color:#777}}
.nested-header{{margin:1em 0;padding:.6em 1em;background:#eef6ff;
               border:1px solid #b0d0f0;border-radius:4px;font-size:.9em}}
{extra_styles}
</style>
</head>
<body>
{header_block}
{att_block}
<div class="body-wrap">
{body_content}
</div>
</body>
</html>"""


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} GB"


def _strip_html(html: str) -> str:
    # Remove <style> and <script> blocks so their content is not treated
    # as visible text by the tag stripper.
    html = _STYLE_BLOCK_RE.sub("", html)
    html = _SCRIPT_BLOCK_RE.sub("", html)
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.result()
