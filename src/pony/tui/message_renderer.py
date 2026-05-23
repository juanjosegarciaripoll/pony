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

from pony.html_sanitize import strip_invisible_blocks

# Used by build_browser_html() to carry the original email's <style> blocks
# into the self-contained view so the browser renders with the same styling.
_STYLE_BLOCK_RE = re.compile(
    r"<style[^>]*>.*?</style>",
    re.IGNORECASE | re.DOTALL,
)

# Detects whether a string looks like a URL (used to decide if anchor text is
# human-readable or just the URL itself duplicated as link text).
_IS_URL_RE = re.compile(r"^https?://|^mailto:", re.IGNORECASE)

# Collapses runs of whitespace to a single space (HTML text normalization).
_WHITESPACE_RE = re.compile(r"\s+")

# Matches plain-text link patterns in order of specificity to avoid overlap.
_PLAIN_LINK_RE = re.compile(
    r"<(https?://[^>\s]+)>"  # <https://…>  angle-bracketed web URL
    r"|<(mailto:[^>\s]+)>"  # <mailto:…>   angle-bracketed address
    r"|(https?://[^\s<>\"')\]]+)"  # bare web URL
    r"|(mailto:[^\s<>\"')\]]+)",  # bare mailto:
    re.IGNORECASE,
)

# Inline-format sentinel IDs used in styled_body.  One letter (B=bold, I=italic,
# U=underline, S=strikethrough) + digit 1 (open) or 0 (close).
_FORMAT_IDS = frozenset(("B1", "B0", "I1", "I0", "U1", "U0", "S1", "S0"))

# Characters that are forbidden in cross-platform filenames.
_UNSAFE_FILENAME_CHARS_RE = re.compile(r'[/\\<>:"|?*\x00-\x1f]')


def _safe_filename_stem(text: str, fallback: str = "message") -> str:
    """Sanitize *text* for use as a filename stem (no extension, no path)."""
    stem = _UNSAFE_FILENAME_CHARS_RE.sub("_", text).strip(". ")
    return stem[:80] if stem else fallback


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """Metadata for one attachment part."""

    index: int  # 1-based display number
    filename: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class AttachmentPayload:
    """Extracted attachment: metadata plus raw decoded bytes."""

    filename: str
    content_type: str
    size_bytes: int
    data: bytes


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """Everything the message view needs to display one message."""

    subject: str
    from_: str
    to: str
    cc: str
    date: str
    body: str  # plain text: URLs as text, no NUL characters — for CLI / MCP / quoting
    attachments: tuple[AttachmentInfo, ...]
    raw_bytes: bytes  # kept for W (open in browser)
    links: tuple[tuple[str, str], ...] = ()  # (kind, target): kind="web"|"mail"
    styled_body: str = ""  # body with \x00LINK:N\x00 + format sentinels for TUI


def render_message(raw_bytes: bytes) -> RenderedMessage:
    """Parse *raw_bytes* and return a ``RenderedMessage`` ready for display."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    subject = _header(msg, "Subject")
    from_ = _header(msg, "From")
    to = _header(msg, "To")
    cc = _header(msg, "Cc")
    date = _header(msg, "Date")

    body, styled_body, links, attachments = _extract_body_and_attachments(msg)

    return RenderedMessage(
        subject=subject,
        from_=from_,
        to=to,
        cc=cc,
        date=date,
        body=body,
        attachments=tuple(attachments),
        raw_bytes=raw_bytes,
        links=tuple(links),
        styled_body=styled_body,
    )


def render_message_markdown(rendered: RenderedMessage) -> str:
    """Format a :class:`RenderedMessage` as a Markdown document for saving.

    The header block uses ``**Field:** value`` notation.  The body comes
    from :func:`render_message` (plain text, HTML stripped via the existing
    ``_HTMLStripper`` pipeline — no extra dependency).  Attachments are
    listed in a fenced section at the end.
    """
    lines: list[str] = []
    if rendered.from_:
        lines.append(f"**From:** {rendered.from_}")
    if rendered.to:
        lines.append(f"**To:** {rendered.to}")
    if rendered.cc:
        lines.append(f"**Cc:** {rendered.cc}")
    if rendered.subject:
        lines.append(f"**Subject:** {rendered.subject}")
    if rendered.date:
        lines.append(f"**Date:** {rendered.date}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(rendered.body)
    if rendered.attachments:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## Attachments")
        for att in rendered.attachments:
            lines.append(f"- {att.filename} ({att.content_type}, {att.size_bytes:,} B)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _header(msg: EmailMessage, name: str) -> str:
    value = msg.get(name, "")
    return str(value).strip()


def _sentinels_to_plain(text: str, links: list[tuple[str, str]]) -> str:
    """Expand NUL sentinels to plain-text equivalents.

    ``\\x00LINK:N\\x00`` → the link target URL or e-mail address.
    ``\\x00B1\\x00`` / ``\\x00B0\\x00`` etc. (format sentinels) → removed.
    The result contains no NUL characters and is safe for CLI, MCP, and quoting.
    """
    segments = text.split("\x00")
    parts: list[str] = []
    for seg in segments:
        if seg.startswith("LINK:"):
            try:
                idx = int(seg[5:])
                _kind, target = links[idx]
                parts.append(target)
            except (ValueError, IndexError):
                pass
        elif seg in _FORMAT_IDS:
            pass  # format sentinel — omit from plain output
        else:
            parts.append(seg)
    return "".join(parts)


def _extract_body_and_attachments(
    msg: EmailMessage,
) -> tuple[str, str, list[tuple[str, str]], list[AttachmentInfo]]:
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
                sep = f"\n{'─' * 60}\n  Attached email: {subj}\n  From: {frm}\n"
                if date:
                    sep += f"  Date: {date}\n"
                sep += f"{'─' * 60}\n"
                body_parts.append(sep)

                # List the attached email itself as an attachment.
                raw = inner_msg.as_bytes()
                attachments.append(
                    AttachmentInfo(
                        index=attach_index,
                        filename=f"{_safe_filename_stem(subj)}.eml",
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
                    payload.decode(charset, errors="replace").replace("\x00", "")
                )

        elif content_type == "text/html" and not body_parts and not in_nested:
            # Only collect HTML if we have no plain text yet and we're
            # not inside a nested message.
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                html_parts.append(payload.decode(charset, errors="replace"))

    links: list[tuple[str, str]] = []
    if body_parts:
        styled_body = _inject_plaintext_links("\n".join(body_parts), links)
        body = _sentinels_to_plain(styled_body, links)
    elif html_parts:
        styled_raw, links = _strip_html("\n".join(html_parts))
        styled_body = _inject_plaintext_links(styled_raw, links)
        body = _sentinels_to_plain(styled_body, links)
    else:
        body = styled_body = "(no readable content)"

    return body, styled_body, links, attachments


class _HTMLStripper(HTMLParser):
    """Minimal tag stripper that preserves line structure.

    Anchor tags are intercepted: ``<a href="URL">text</a>`` is replaced by
    either ``text \x00LINK:{n}\x00`` (when the anchor text is human-readable)
    or ``\x00LINK:{n}\x00`` alone (when it is empty or duplicates the URL).
    Collected links are available via ``links()``.
    """

    # Paragraph-level: flush with blank separator on both start and end tags.
    _PARA_TAGS = frozenset(("p", "div", "h1", "h2", "h3", "h4", "ul", "ol", "table"))
    # Void/self-closing line break: flush only on start tag.
    # HTMLParser fires handle_endtag for <br /> too; we intentionally ignore it
    # so that a single <br> does not produce a spurious blank separator.
    _BR_TAGS = frozenset(("br",))
    # Container items: flush only on end tag so that adjacent <li>/<tr> tags
    # separated by whitespace do not insert blank lines between items.
    _END_ONLY_TAGS = frozenset(("li", "tr"))
    _BLOCK_TAGS = _PARA_TAGS | _BR_TAGS | _END_ONLY_TAGS

    # Inline formatting tags → sentinel letter used in \x00{letter}{1|0}\x00.
    _FORMAT_TAG_MAP: dict[str, str] = {
        "b": "B",
        "strong": "B",
        "i": "I",
        "em": "I",
        "u": "U",
        "s": "S",
        "del": "S",
        "strike": "S",
    }

    def __init__(self) -> None:
        super().__init__()
        self._lines: list[str] = []
        self._current: list[str] = []
        self._links: list[tuple[str, str]] = []
        self._in_anchor = False
        self._anchor_kind = ""
        self._anchor_target = ""
        self._anchor_text: list[str] = []
        self._format_depth: dict[str, int] = {"B": 0, "I": 0, "U": 0, "S": 0}

    def handle_data(self, data: str) -> None:
        # Normalize whitespace like a browser: collapse runs (incl. newlines from
        # HTML source line-wrapping) to a single space.  Strip NUL bytes so that
        # &#0; entities cannot inject internal format sentinels.
        normalized = _WHITESPACE_RE.sub(" ", data).replace("\x00", "")
        if self._in_anchor:
            self._anchor_text.append(normalized)
        else:
            self._current.append(normalized)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._PARA_TAGS:
            self._flush(paragraph=True)
        elif tag in self._BR_TAGS:
            self._flush(add_blank_if_empty=True)
        # _END_ONLY_TAGS: no flush on start tag
        if tag == "a":
            attr_dict = dict(attrs)
            href = (attr_dict.get("href") or "").strip()
            if href.startswith(("http://", "https://")):
                self._in_anchor = True
                self._anchor_kind = "web"
                self._anchor_target = href
                self._anchor_text = []
            elif href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip()
                self._in_anchor = True
                self._anchor_kind = "mail"
                self._anchor_target = addr
                self._anchor_text = []
        fmt = self._FORMAT_TAG_MAP.get(tag)
        if fmt and not self._in_anchor:
            depth = self._format_depth[fmt]
            self._format_depth[fmt] = depth + 1
            if depth == 0:
                self._current.append(f"\x00{fmt}1\x00")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._PARA_TAGS:
            self._flush(paragraph=True)
        elif tag in self._END_ONLY_TAGS:
            self._flush()
        # _BR_TAGS: no flush on end tag
        if tag == "a" and self._in_anchor:
            anchor_text = "".join(self._anchor_text).strip()
            idx = len(self._links)
            self._links.append((self._anchor_kind, self._anchor_target))
            sentinel = f"\x00LINK:{idx}\x00"
            is_url = (
                bool(_IS_URL_RE.match(anchor_text))
                or anchor_text == self._anchor_target
            )
            if anchor_text and not is_url:
                self._current.append(f"{anchor_text} {sentinel}")
            else:
                self._current.append(sentinel)
            self._in_anchor = False
            self._anchor_kind = ""
            self._anchor_target = ""
            self._anchor_text = []
        fmt = self._FORMAT_TAG_MAP.get(tag)
        if fmt and not self._in_anchor:
            depth = self._format_depth[fmt]
            if depth > 0:
                self._format_depth[fmt] = depth - 1
                if depth == 1:
                    self._current.append(f"\x00{fmt}0\x00")

    def _flush(
        self, *, paragraph: bool = False, add_blank_if_empty: bool = False
    ) -> None:
        text = "".join(self._current).strip()
        if text:
            self._lines.append(text)
            if paragraph:
                self._lines.append("")
        elif (
            (add_blank_if_empty or paragraph) and self._lines and self._lines[-1] != ""
        ):
            # Empty boundary after non-empty content → blank separator.
            # <br/><br/> produces this via add_blank_if_empty; paragraph-tag
            # boundaries (</ul>, </div> etc.) produce it via paragraph=True.
            self._lines.append("")
        self._current.clear()

    def result(self) -> str:
        self._flush()
        # Strip any trailing blank separator lines added by paragraph flushes.
        while self._lines and not self._lines[-1]:
            self._lines.pop()
        return "\n".join(self._lines)

    def links(self) -> list[tuple[str, str]]:
        return list(self._links)


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
    from_ = _header(msg, "From")
    to = _header(msg, "To")
    cc = _header(msg, "Cc")
    date = _header(msg, "Date")

    html_body: str | None = None
    plain_body: str | None = None
    cid_map: dict[str, str] = {}  # bare Content-ID → data URI
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
                attachments.append(
                    AttachmentInfo(
                        index=attach_index,
                        filename=f"{_safe_filename_stem(subj)}.eml",
                        content_type="message/rfc822",
                        size_bytes=len(raw_inner),
                    )
                )
                attach_index += 1
            continue

        if part.get_content_maintype() == "multipart":
            continue

        disposition = part.get_content_disposition() or ""
        filename = part.get_filename()
        content_id = part.get("Content-ID", "").strip().strip("<>")

        payload = part.get_payload(decode=True)

        # Build CID map for inline parts (images embedded in HTML).
        if content_id and isinstance(payload, bytes):
            b64 = base64.b64encode(payload).decode("ascii")
            cid_map[content_id] = f"data:{content_type};base64,{b64}"

        # Real attachments go to the list.
        if disposition == "attachment" or (filename and disposition != "inline"):
            size = len(payload) if isinstance(payload, bytes) else 0
            attachments.append(
                AttachmentInfo(
                    index=attach_index,
                    filename=filename or "(unnamed)",
                    content_type=content_type,
                    size_bytes=size,
                )
            )
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
        extra_styles = "".join(m.group(0) for m in _STYLE_BLOCK_RE.finditer(html_body))
    elif plain_body is not None:
        body_content = (
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
    cc_row = f"<tr><td>Cc</td><td>{_html.escape(cc)}</td></tr>" if cc else ""
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


def extract_attachment(raw_bytes: bytes, index: int) -> AttachmentPayload | None:
    """Return the bytes of the 1-based *index*-th attachment in *raw_bytes*.

    The indexing contract must match :func:`_extract_body_and_attachments`:
    ``message/rfc822`` parts count as attachments (their bytes are the
    inner message serialised via ``EmailMessage.as_bytes``), followed by
    every part whose disposition is ``attachment`` or which carries a
    filename.  Returns ``None`` when *index* is out of range.
    """
    if index < 1:
        return None
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    found = 0
    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = part.get_content_disposition() or ""

        if content_type == "message/rfc822":
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                inner = inner[0]
            if isinstance(inner, EmailMessage):
                found += 1
                if found == index:
                    data = inner.as_bytes()
                    subj = _header(inner, "Subject") or "(no subject)"
                    return AttachmentPayload(
                        filename=f"{_safe_filename_stem(subj)}.eml",
                        content_type="message/rfc822",
                        size_bytes=len(data),
                        data=data,
                    )
            continue

        if part.get_content_maintype() == "multipart":
            continue

        if disposition == "attachment" or part.get_filename():
            found += 1
            if found == index:
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes):
                    return None
                return AttachmentPayload(
                    filename=part.get_filename() or "(unnamed)",
                    content_type=content_type,
                    size_bytes=len(payload),
                    data=payload,
                )
    return None


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} GB"


def _strip_html(html: str) -> tuple[str, list[tuple[str, str]]]:
    stripper = _HTMLStripper()
    stripper.feed(strip_invisible_blocks(html))
    return stripper.result(), stripper.links()


def _inject_plaintext_links(text: str, links: list[tuple[str, str]]) -> str:
    """Replace bare and angle-bracketed URLs in *text* with sentinels.

    Each detected URL is appended to *links* (mutated in-place) and replaced
    with ``\\x00LINK:{idx}\\x00`` so the view layer can render a clickable token.
    Already-injected sentinels (from HTML stripping) are untouched because NUL
    never appears in URL text.
    """

    def _replace(m: re.Match[str]) -> str:
        if m.group(1) is not None:  # <https://...>
            kind, target = "web", m.group(1)
        elif m.group(2) is not None:  # <mailto:...>
            kind, target = "mail", m.group(2)[7:].split("?")[0].strip()
        elif m.group(3) is not None:  # bare https://...
            kind, target = "web", m.group(3)
        else:  # bare mailto:...
            kind, target = "mail", (m.group(4) or "")[7:].split("?")[0].strip()
        idx = len(links)
        links.append((kind, target))
        return f"\x00LINK:{idx}\x00"

    return _PLAIN_LINK_RE.sub(_replace, text)
