"""Utilities for email composition: quoting, message building."""

from __future__ import annotations

import mimetypes
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from pathlib import Path

from .message_renderer import RenderedMessage


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
    rendered: RenderedMessage, *, self_address: str,
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
        cc_parts.append(formataddr((name, addr)))

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
        from markdown_it import MarkdownIt
        # Convert plain-text sig-dashes to a Markdown HR and fix up the
        # signature body so its internal newlines become hard line breaks
        # (two trailing spaces).  Without this Markdown collapses multi-line
        # signatures into a single run-on line.
        # The editor always stores "-- " so the split is safe regardless of
        # whether the user toggled markdown mode after opening the composer.
        sig_parts = body.rsplit("\n-- \n", 1)
        if len(sig_parts) == 2:
            main_part, sig_part = sig_parts
            sig_md = sig_part.replace("\n", "  \n")
            md_body = f"{main_part}\n\n---\n\n{sig_md}"
        else:
            md_body = body
        html_body = MarkdownIt().render(md_body)
        msg.set_content(body)  # text/plain: sig-dashes and newlines intact
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

    return msg
