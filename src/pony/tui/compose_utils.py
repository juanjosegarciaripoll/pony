"""Utilities for email composition: quoting, message building."""

from __future__ import annotations

import mimetypes
import re
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path

from .message_renderer import RenderedMessage

_QUOTE_BOUNDARY_RE = re.compile(
    r"(?m)^(On .+ wrote:|---------- Forwarded message ----------)$"
)

_ADDR_SPECIAL_RE = re.compile(r'[",;:<>\[\]()\\]')


def format_display_address(name: str, addr: str) -> str:
    """Format an address pair as a human-readable string without RFC 2047 encoding.

    Used for display in the composer UI.  Python's EmailMessage re-encodes
    non-ASCII display names to RFC 2047 automatically when serializing for SMTP.
    """
    if not name:
        return addr
    if _ADDR_SPECIAL_RE.search(name):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"


def _split_at_quote_boundary(text: str) -> tuple[str, str]:
    """Split *text* into (user_written, quoted) at the reply/forward boundary.

    Returns the full text as the first element and an empty string if no
    boundary is found.
    """
    m = _QUOTE_BOUNDARY_RE.search(text)
    if not m:
        return text, ""
    cut = text.rfind("\n", 0, m.start())
    if cut == -1:
        return "", text
    return text[:cut], text[cut + 1 :]


def _sig_block(signature: str) -> str:
    """Return a standard signature block: blank line, '-- ', signature text."""
    return f"\n\n-- \n{signature}"


def new_compose_body(signature: str | None) -> str:
    """Return the initial body for a blank compose window.

    If *signature* is set, the body is pre-filled with the signature block so
    the user starts typing above it.  Otherwise returns an empty string.
    """
    return _sig_block(signature) if signature else ""


def build_reply_body(rendered: RenderedMessage, *, signature: str | None = None) -> str:
    """Build a top-posted reply body.

    Returns two blank lines (cursor position), then attribution + quoted body,
    then the signature block (if any) after the quoted text.
    """
    attribution = f"On {rendered.date}, {rendered.from_} wrote:"
    quoted = "\n".join(f"> {line}" for line in rendered.body.splitlines())
    body = f"\n\n{attribution}\n\n{quoted}"
    if signature:
        body += _sig_block(signature)
    return body


def build_forward_body(
    rendered: RenderedMessage, *, signature: str | None = None
) -> str:
    """Build a forward body with a standard attribution block.

    The signature block (if any) is appended after the forwarded content.
    """
    lines = [
        "",
        "---------- Forwarded message ----------",
        f"From: {rendered.from_}",
        f"Date: {rendered.date}",
        f"Subject: {rendered.subject}",
        f"To: {rendered.to}",
        "",
        rendered.body,
    ]
    body = "\n".join(lines)
    if signature:
        body += _sig_block(signature)
    return body


def build_reply_all_recipients(
    rendered: RenderedMessage,
    *,
    self_address: str,
) -> tuple[str, str]:
    """Return ``(to, cc)`` for a reply-all to *rendered*.

    *to* is the original sender (``From``).  *cc* contains every address
    from the original ``To`` and ``Cc`` headers, minus the user's own
    *self_address* and the addresses already in *to*.  Display names
    from the source headers are preserved; comparisons are
    case-insensitive on the address part only.
    """
    self_norm = self_address.strip().lower()
    excluded: set[str] = {
        addr.lower() for _, addr in getaddresses([rendered.from_]) if addr
    }
    excluded.add(self_norm)

    cc_parts: list[str] = []
    seen: set[str] = set(excluded)
    for name, addr in getaddresses([rendered.to, rendered.cc]):
        if not addr:
            continue
        norm = addr.lower()
        if norm in seen:
            continue
        seen.add(norm)
        cc_parts.append(format_display_address(name, addr))

    return rendered.from_, ", ".join(cc_parts)


def reply_subject(subject: str) -> str:
    """Prefix *subject* with 'Re: ' if not already present."""
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def forward_subject(subject: str) -> str:
    """Prefix *subject* with 'Fwd: ' if not already present."""
    if subject.lower().startswith(("fwd:", "fw:")):
        return subject
    return f"Fwd: {subject}"


def parse_draft_fields(raw: bytes) -> dict[str, str]:
    """Extract editable fields from stored draft bytes.

    Returns a dict with keys: ``to``, ``cc``, ``bcc``, ``subject``, ``body``.
    The plain-text part is used for the body regardless of whether the draft
    was composed in Markdown mode (the raw Markdown source is always stored
    as the text/plain alternative).
    """
    import email as _email
    import email.policy as _policy

    parsed = _email.message_from_bytes(raw, policy=_policy.default)

    def _hdr(name: str) -> str:
        return str(parsed.get(name, "")).strip()

    body = ""
    for part in parsed.walk():
        if (
            part.get_content_type() == "text/plain"
            and part.get_content_disposition() != "attachment"
        ):
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")
            break

    return {
        "to": _hdr("To"),
        "cc": _hdr("Cc"),
        "bcc": _hdr("Bcc"),
        "subject": _hdr("Subject"),
        "body": body,
    }


def build_email_message(
    *,
    from_address: str,
    to: str,
    cc: str,
    bcc: str,
    subject: str,
    body: str,
    attachment_paths: list[Path],
    markdown_mode: bool = False,
    forwarded_message: bytes | None = None,
) -> EmailMessage:
    """Build a ready-to-send :class:`EmailMessage`.

    When *markdown_mode* is ``True``, the body is interpreted as Markdown
    source.  The message is built as ``multipart/alternative`` with a
    ``text/plain`` part (raw Markdown source, readable as-is by plain-text
    clients) and a ``text/html`` part (rendered HTML).

    Attachments are read from disk and encoded inline.  Raises
    ``FileNotFoundError`` if an attachment path does not exist.
    """
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    if markdown_mode:
        import html as _html_mod

        from markdown_it import MarkdownIt

        md = MarkdownIt()

        # Separate the signature first.
        sig_parts = body.rsplit("\n-- \n", 1)
        if len(sig_parts) == 2:
            main_part, sig_part = sig_parts
        else:
            main_part, sig_part = body, None

        # Separate user-written text from quoted/forwarded content so only
        # the user's part is interpreted as Markdown.
        user_part, quoted_part = _split_at_quote_boundary(main_part)

        html_sections: list[str] = []
        if user_part.strip():
            html_sections.append(md.render(user_part))
        if quoted_part.strip():
            escaped = _html_mod.escape(quoted_part)
            html_sections.append(
                '<blockquote style="white-space:pre-wrap;'
                'border-left:2px solid #ccc;margin:0;padding-left:1em">'
                f"{escaped}</blockquote>"
            )
        if sig_part is not None:
            sig_md = sig_part.replace("\n", "  \n")
            html_sections.append(md.render(f"---\n\n{sig_md}"))

        html_body = "\n".join(html_sections)
        msg.set_content(body)  # text/plain unchanged
        msg.add_alternative(html_body, subtype="html")
    else:
        msg.set_content(body)

    for path in attachment_paths:
        ctype, encoding = mimetypes.guess_type(str(path))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    if forwarded_message is not None:
        import email.policy
        from email import message_from_bytes as _parse

        original = _parse(forwarded_message, policy=email.policy.default)
        msg.add_attachment(original)

    return msg
