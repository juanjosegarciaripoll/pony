"""Representative RFC 5322 message corpus for testing.

Each factory function returns raw bytes that simulate a realistic mail message
for a specific structural or encoding scenario.  Import the functions you need
in any test module that requires varied message structures.

Scenarios — message structure
-----------------------------
plain_text                  Simple text/plain — the baseline case.
multipart_alternative       text/plain + text/html — the most common modern format.
multipart_mixed_attachment  text/plain body + one named file attachment.
multipart_mixed_multi       text/plain body + two named file attachments.
html_only                   No text/plain part; only text/html.
inline_image                multipart/related with an inline CID image — the
                            inline part must NOT be counted as an attachment.
nested_forward              Forwarded message containing its own attachments.

Scenarios — encoding edge cases
-------------------------------
encoded_headers             RFC 2047 Q-encoded Subject and From display name.
quoted_printable_body       Content-Transfer-Encoding: quoted-printable with
                            non-ASCII characters in the body.
base64_body                 Content-Transfer-Encoding: base64 body.
non_ascii_sender            From with CJK display name.

Scenarios — missing / degenerate fields
---------------------------------------
missing_date                No Date header; projection must fall back to now().
missing_message_id          No Message-ID header; projection must synthesise one.
empty_body                  Headers only, zero-length body.
very_long_subject           Subject exceeding typical display widths.

Scenarios — harvesting
----------------------
many_recipients             Many To/Cc addresses for contact harvesting stress.

Scenarios — malformed / hostile input
-------------------------------------
html_rich_formatting        text/html with lists, tables, nested bold and
                            double ``<br>`` — every stripper construct at once.
html_first_multipart        multipart whose only text part is HTML.
attachment_only             No readable text part; attachment only.
base64_rfc822_attachment    message/rfc822 part carried as base64.
no_header_body_separator    Headers with no terminating blank line.
duplicate_headers           Repeated From/Subject; the first must win.
invalid_date                Date header the RFC 5322 parser rejects.
naive_date                  Date header with no UTC offset.
corrupt_base64_body         Declares base64, body is not base64.
oversized_body              Body larger than the preview cap.
traversal_attachment_filename
                            Attachment filename that tries to escape the
                            destination directory.
inline_image_named          Inline image that also carries a filename.
inline_text_named           Inline text/plain that also carries a filename.
inline_html_named           Inline text/html that also carries a filename.
unnamed_attachment          Content-Disposition: attachment, no filename.
"""

from __future__ import annotations

import textwrap
from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# Shared constants — deterministic values reusable across tests
# ---------------------------------------------------------------------------

FROM_ADDR = "Alice Smith <alice@example.com>"
TO_ADDR = "Bob Jones <bob@example.com>"
CC_ADDR = "Carol White <carol@example.com>"
DATE = "Fri, 11 Apr 2026 10:00:00 +0000"
MESSAGE_ID = "<corpus-fixture@example.com>"

PLAIN_BODY = textwrap.dedent("""\
    Hi Bob,

    Just a quick note to confirm Tuesday's meeting is still on.
    The room is booked from 14:00 to 15:30.

    Best,
    Alice
""")

HTML_BODY = textwrap.dedent("""\
    <html><body>
    <p>Hi Bob,</p>
    <p>Just a quick note to confirm <strong>Tuesday's meeting</strong> is still on.
    The room is booked from 14:00 to 15:30.</p>
    <p>Best,<br>Alice</p>
    </body></html>
""")

# Minimal 1×1 transparent PNG (binary literal, not generated at runtime).
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def plain_text() -> bytes:
    """Simple single-part text/plain message."""
    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Cc"] = CC_ADDR
    msg["Subject"] = "Tuesday meeting confirmed"
    msg["Date"] = DATE
    msg["Message-ID"] = MESSAGE_ID
    msg.set_content(PLAIN_BODY)
    return msg.as_bytes()


def mid_thread_message() -> bytes:
    """A message several replies deep, with a folded ``References`` chain.

    Real threads arrive with ``References`` folded across continuation
    lines once the chain outgrows one header line, so a reply built from
    this fixture exercises unfolding as well as chain extension.
    """
    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Re: Tuesday meeting confirmed"
    msg["Date"] = DATE
    msg["Message-ID"] = "<thread-third@example.com>"
    msg.set_content(PLAIN_BODY)
    raw = msg.as_bytes()
    # Inject the folded header directly: EmailMessage refuses to store a
    # value containing newlines, but that is exactly how it arrives.
    return raw.replace(
        b"Message-ID:",
        b"References: <thread-root@example.com>\r\n"
        b" <thread-second@example.com>\r\nMessage-ID:",
        1,
    )


def unthreaded_message() -> bytes:
    """A message with no ``Message-ID`` — nothing for a reply to point at."""
    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Anonymous notice"
    msg["Date"] = DATE
    msg.set_content(PLAIN_BODY)
    return msg.as_bytes()


def multipart_alternative() -> bytes:
    """text/plain + text/html alternative — the most common real-world format.

    Projection should prefer the text/plain part for the body preview.
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Tuesday meeting confirmed"
    msg["Date"] = DATE
    msg["Message-ID"] = "<alt-fixture@example.com>"
    msg.attach(MIMEText(PLAIN_BODY, "plain", "utf-8"))
    msg.attach(MIMEText(HTML_BODY, "html", "utf-8"))
    return msg.as_bytes()


def multipart_mixed_attachment() -> bytes:
    """text/plain body with one named PDF attachment.

    has_attachments must be True; body_preview must come from the text part.
    """
    msg = MIMEMultipart("mixed")
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Q1 report attached"
    msg["Date"] = DATE
    msg["Message-ID"] = "<att1-fixture@example.com>"
    msg.attach(MIMEText("Please find the Q1 report attached.\n", "plain", "utf-8"))
    pdf = MIMEApplication(b"%PDF-1.4 fake pdf content", Name="q1-report.pdf")
    pdf["Content-Disposition"] = 'attachment; filename="q1-report.pdf"'
    msg.attach(pdf)
    return msg.as_bytes()


def multipart_mixed_multi() -> bytes:
    """text/plain body with two named attachments.

    has_attachments must be True.
    """
    msg = MIMEMultipart("mixed")
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Two attachments"
    msg["Date"] = DATE
    msg["Message-ID"] = "<att2-fixture@example.com>"
    msg.attach(MIMEText("See both files attached.\n", "plain", "utf-8"))
    for name, content in [
        ("report.pdf", b"%PDF fake"),
        ("data.csv", b"col1,col2\n1,2\n"),
    ]:
        part = MIMEApplication(content, Name=name)
        part["Content-Disposition"] = f'attachment; filename="{name}"'
        msg.attach(part)
    return msg.as_bytes()


def html_only() -> bytes:
    """HTML-only message with no text/plain part.

    body_preview must fall back to the HTML part with tags stripped.
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "HTML-only newsletter"
    msg["Date"] = DATE
    msg["Message-ID"] = "<html-fixture@example.com>"
    msg.attach(MIMEText(HTML_BODY, "html", "utf-8"))
    return msg.as_bytes()


def encoded_headers() -> bytes:
    """RFC 2047 Q-encoded Subject and From display name.

    The parser must decode the encoded words so that the projected sender
    and subject fields contain plain Unicode strings, not encoded tokens.
    """
    # Write raw bytes to simulate what an IMAP server would deliver.
    # =?UTF-8?Q?...?= is Q-encoding; spaces encoded as underscores.
    return (
        b"From: =?UTF-8?Q?Andr=C3=A9_M=C3=BCller?= <andre@example.com>\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: =?UTF-8?Q?R=C3=A9union=3A_Probl=C3=A8me_r=C3=A9solu?=\r\n"
        b"Date: Fri, 11 Apr 2026 10:00:00 +0000\r\n"
        b"Message-ID: <enc-fixture@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"Corps du message.\r\n"
    )


def missing_date() -> bytes:
    """Message with no Date header.

    Projection must fall back to datetime.now() without raising.
    """
    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "No date header"
    msg["Message-ID"] = "<nodate-fixture@example.com>"
    msg.set_content("Body without a date.\n")
    return msg.as_bytes()


def missing_message_id() -> bytes:
    """Message with no Message-ID header.

    The projection layer accepts this; callers (e.g. the sync engine) are
    responsible for synthesising a stable ID.
    """
    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "No message-id header"
    msg["Date"] = DATE
    msg.set_content("Body without a message-id.\n")
    return msg.as_bytes()


def inline_image() -> bytes:
    """multipart/related with an inline CID-referenced image.

    The image part has Content-Disposition: inline, so has_attachments
    must be False — inline images are not user-downloadable attachments.
    """
    html_with_cid = (
        "<html><body><p>Hello!</p>"
        '<img src="cid:logo@example.com" alt="logo">'
        "</body></html>"
    )
    related = MIMEMultipart("related")
    related["From"] = FROM_ADDR
    related["To"] = TO_ADDR
    related["Subject"] = "Message with inline image"
    related["Date"] = DATE
    related["Message-ID"] = "<inline-fixture@example.com>"

    # Wrap in multipart/alternative so there's a plain fallback.
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Hello! (see image in HTML version)", "plain", "utf-8"))
    alt.attach(MIMEText(html_with_cid, "html", "utf-8"))
    related.attach(alt)

    img = MIMEImage(_TINY_PNG, "png")
    img["Content-ID"] = "<logo@example.com>"
    img["Content-Disposition"] = "inline"
    related.attach(img)

    return related.as_bytes()


def quoted_printable_body() -> bytes:
    """Body encoded with Content-Transfer-Encoding: quoted-printable.

    The projection must decode the transfer encoding so that body_preview
    contains the decoded Unicode text, not QP escape sequences.
    """
    # é = =C3=A9 in UTF-8 QP encoding
    return (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Quoted-printable body\r\n"
        b"Date: Fri, 11 Apr 2026 10:00:00 +0000\r\n"
        b"Message-ID: <qp-fixture@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"Caf=C3=A9 au lait, s=C3=BCper sch=C3=B6n.\r\n"
    )


def base64_body() -> bytes:
    """Body encoded with Content-Transfer-Encoding: base64.

    The projection must decode the transfer encoding so that body_preview
    contains readable text, not base64 gibberish.
    """
    import base64 as _b64

    body = "This message body is base64-encoded.\n"
    encoded = _b64.b64encode(body.encode()).decode()
    return (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Base64 body\r\n"
        b"Date: Fri, 11 Apr 2026 10:00:00 +0000\r\n"
        b"Message-ID: <b64-fixture@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + encoded.encode() + b"\r\n"
    )


def non_ascii_sender() -> bytes:
    """From header with a CJK display name.

    The parser must handle non-Latin characters in the sender field
    without errors.
    """
    return (
        b"From: =?UTF-8?B?5bGx55Sw5aSq6YOO?= <taro@example.jp>\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: CJK sender\r\n"
        b"Date: Fri, 11 Apr 2026 10:00:00 +0000\r\n"
        b"Message-ID: <cjk-fixture@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Message from a CJK sender.\r\n"
    )


def empty_body() -> bytes:
    """Message with headers but zero-length body.

    The projection must not crash; body_preview should be empty.
    """
    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Empty body"
    msg["Date"] = DATE
    msg["Message-ID"] = "<empty-fixture@example.com>"
    # No set_content() call — body is empty.
    return msg.as_bytes()


def very_long_subject() -> bytes:
    """Subject line exceeding typical display widths.

    Should not cause truncation errors or layout issues.
    """
    long_subject = "Re: " * 30 + "This is a very long subject line"
    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = long_subject
    msg["Date"] = DATE
    msg["Message-ID"] = "<long-subj-fixture@example.com>"
    msg.set_content("Short body.\n")
    return msg.as_bytes()


def nested_forward() -> bytes:
    """Forwarded message containing its own attachment.

    The outer message is multipart/mixed wrapping a text/plain intro
    and a message/rfc822 part.  The inner message has an attachment.
    has_attachments should be True (the inner attachment counts).
    """
    # Inner message with an attachment.
    inner = MIMEMultipart("mixed")
    inner["From"] = "charlie@example.com"
    inner["To"] = FROM_ADDR
    inner["Subject"] = "Original with attachment"
    inner["Date"] = DATE
    inner["Message-ID"] = "<inner-fixture@example.com>"
    inner.attach(MIMEText("See attached spreadsheet.\n", "plain", "utf-8"))
    xls = MIMEApplication(b"\x00\x01\x02 fake xls", Name="data.xlsx")
    xls["Content-Disposition"] = 'attachment; filename="data.xlsx"'
    inner.attach(xls)

    # Outer forwarding wrapper.
    outer = MIMEMultipart("mixed")
    outer["From"] = FROM_ADDR
    outer["To"] = TO_ADDR
    outer["Subject"] = "Fwd: Original with attachment"
    outer["Date"] = DATE
    outer["Message-ID"] = "<fwd-fixture@example.com>"
    outer.attach(MIMEText("FYI, see below.\n", "plain", "utf-8"))
    # Attach the inner message as message/rfc822.
    from email.mime.message import MIMEMessage

    outer.attach(MIMEMessage(inner))
    return outer.as_bytes()


def double_attached_emails() -> bytes:
    """Message with two attached emails, each containing their own attachments.

    The renderer must:
    - List both attached emails as named attachments
    - Show header separators for each attached email in the body
    - List inner attachments (contract.pdf, budget.xlsx) as saveable
    """
    inner1 = MIMEMultipart("mixed")
    inner1["From"] = "charlie@example.com"
    inner1["To"] = FROM_ADDR
    inner1["Subject"] = "Contract draft"
    inner1["Date"] = DATE
    inner1["Message-ID"] = "<inner1-fixture@example.com>"
    inner1.attach(MIMEText("Please review the contract.\n", "plain", "utf-8"))
    pdf = MIMEApplication(b"%PDF-1.4 fake contract", Name="contract.pdf")
    pdf["Content-Disposition"] = 'attachment; filename="contract.pdf"'
    inner1.attach(pdf)

    inner2 = MIMEMultipart("mixed")
    inner2["From"] = "dave@example.com"
    inner2["To"] = FROM_ADDR
    inner2["Subject"] = "Budget numbers"
    inner2["Date"] = DATE
    inner2["Message-ID"] = "<inner2-fixture@example.com>"
    inner2.attach(MIMEText("See the attached spreadsheet.\n", "plain", "utf-8"))
    xls = MIMEApplication(b"\x00\x01 fake xls", Name="budget.xlsx")
    xls["Content-Disposition"] = 'attachment; filename="budget.xlsx"'
    inner2.attach(xls)

    from email.mime.message import MIMEMessage

    outer = MIMEMultipart("mixed")
    outer["From"] = FROM_ADDR
    outer["To"] = TO_ADDR
    outer["Subject"] = "Fwd: Two emails for review"
    outer["Date"] = DATE
    outer["Message-ID"] = "<double-att-fixture@example.com>"
    outer.attach(MIMEText("Please review both attached emails.\n", "plain", "utf-8"))
    outer.attach(MIMEMessage(inner1))
    outer.attach(MIMEMessage(inner2))
    return outer.as_bytes()


def many_recipients() -> bytes:
    """Message with many To and Cc recipients.

    Exercises contact harvesting with a large address list.
    """
    to_addrs = ", ".join(f"User{i} <user{i}@example.com>" for i in range(1, 21))
    cc_addrs = ", ".join(f"CC{i} <cc{i}@example.com>" for i in range(1, 11))
    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = to_addrs
    msg["Cc"] = cc_addrs
    msg["Subject"] = "Team announcement"
    msg["Date"] = DATE
    msg["Message-ID"] = "<many-recip-fixture@example.com>"
    msg.set_content("Please review the attached proposal.\n")
    return msg.as_bytes()


# ---------------------------------------------------------------------------
# Malformed and hostile shapes
#
# Real mail is produced by decades of clients, some of them broken.  These
# fixtures are deliberately non-conforming: the projection and renderer must
# degrade gracefully rather than raise, because a single bad message in a
# folder would otherwise take down the whole sync or folder open.  They are
# built from raw bytes, not ``EmailMessage``, precisely because the stdlib
# builders refuse to emit most of them.
# ---------------------------------------------------------------------------


def html_rich_formatting() -> bytes:
    """text/html exercising every block and inline construct the stripper knows.

    Lists (``<li>``) and table rows (``<tr>``) end a line without starting a
    paragraph; ``<br><br>`` collapses to one blank separator; nested
    ``<b>`` tags must emit a single bold span, not one per nesting level.
    """
    html = (
        "<html><body>"
        "<p>Intro paragraph.</p>"
        "<ul><li>First item</li><li>Second item</li></ul>"
        "<table><tr><td>Cell A</td><td>Cell B</td></tr>"
        "<tr><td>Cell C</td></tr></table>"
        "<p>Nested <b>bold <b>deeper</b> back</b> and <i>italic</i>.</p>"
        "Line one<br><br>Line three"
        "</body></html>"
    )
    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Rich formatting"
    msg["Date"] = DATE
    msg["Message-ID"] = "<rich-html-fixture@example.com>"
    return msg.as_bytes()


def html_first_multipart() -> bytes:
    """multipart/mixed whose first part is text/html, with no text/plain.

    The preview extractor has to fall through the parts looking for plain
    text, find none, and settle for the stripped HTML.
    """
    return (
        b"From: " + FROM_ADDR.encode() + b"\r\n"
        b"To: " + TO_ADDR.encode() + b"\r\n"
        b"Subject: HTML before anything else\r\n"
        b"Date: " + DATE.encode() + b"\r\n"
        b"Message-ID: <html-first-fixture@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="HFB"\r\n'
        b"\r\n"
        b"--HFB\r\n"
        b'Content-Type: text/html; charset="utf-8"\r\n'
        b"\r\n"
        b"<html><body><p>Hello <b>there</b></p></body></html>\r\n"
        b"--HFB\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="payload.bin"\r\n'
        b"\r\n"
        b"Zm9v\r\n"
        b"--HFB--\r\n"
    )


def attachment_only() -> bytes:
    """multipart/mixed with an attachment and no readable text part at all."""
    msg = MIMEMultipart("mixed")
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Only a file"
    msg["Date"] = DATE
    msg["Message-ID"] = "<attachment-only-fixture@example.com>"
    part = MIMEApplication(b"binary payload", Name="thing.bin")
    part["Content-Disposition"] = 'attachment; filename="thing.bin"'
    msg.attach(part)
    return msg.as_bytes()


def base64_rfc822_attachment() -> bytes:
    """An attached message whose message/rfc822 part is base64-encoded.

    Mailing-list digests and some Exchange forwards do this.  The inner
    message still has to be listed as a downloadable ``.eml``.
    """
    return (
        b"From: " + FROM_ADDR.encode() + b"\r\n"
        b"To: " + TO_ADDR.encode() + b"\r\n"
        b"Subject: Digest with an encoded forward\r\n"
        b"Date: " + DATE.encode() + b"\r\n"
        b"Message-ID: <b64-rfc822-fixture@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B64B"\r\n'
        b"\r\n"
        b"--B64B\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"\r\n"
        b"See the attached message.\r\n"
        b"--B64B\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"U3ViamVjdDogSW5uZXIgbm90ZQoKSGVsbG8u\r\n"
        b"--B64B--\r\n"
    )


def no_header_body_separator() -> bytes:
    """Headers that simply stop — no blank line, no body.

    Some POP3 gateways truncate here.  The header/body split has no match
    to work with and must treat the whole buffer as headers.
    """
    return (
        b"From: " + FROM_ADDR.encode() + b"\r\n"
        b"To: " + TO_ADDR.encode() + b"\r\n"
        b"Subject: Truncated before the body\r\n"
        b"Message-ID: <no-separator-fixture@example.com>\r\n"
    )


def duplicate_headers() -> bytes:
    """The same header repeated — the first occurrence must win."""
    return (
        b"From: " + FROM_ADDR.encode() + b"\r\n"
        b"From: Impostor <mallory@example.com>\r\n"
        b"To: " + TO_ADDR.encode() + b"\r\n"
        b"Subject: First subject wins\r\n"
        b"Subject: Second subject ignored\r\n"
        b"Date: " + DATE.encode() + b"\r\n"
        b"Message-ID: <duplicate-headers-fixture@example.com>\r\n"
        b"\r\n"
        b"Body text.\r\n"
    )


def invalid_date() -> bytes:
    """A Date header the RFC 5322 parser cannot make sense of."""
    return (
        b"From: " + FROM_ADDR.encode() + b"\r\n"
        b"To: " + TO_ADDR.encode() + b"\r\n"
        b"Subject: Unparseable date\r\n"
        b"Date: sometime last Tuesday\r\n"
        b"Message-ID: <invalid-date-fixture@example.com>\r\n"
        b"\r\n"
        b"Body text.\r\n"
    )


def naive_date() -> bytes:
    """A Date header with no UTC offset — must be assumed UTC, not dropped."""
    return (
        b"From: " + FROM_ADDR.encode() + b"\r\n"
        b"To: " + TO_ADDR.encode() + b"\r\n"
        b"Subject: Timezone-less date\r\n"
        b"Date: Fri, 17 Apr 2026 12:00:00\r\n"
        b"Message-ID: <naive-date-fixture@example.com>\r\n"
        b"\r\n"
        b"Body text.\r\n"
    )


def corrupt_base64_body() -> bytes:
    """Declares base64 but the body is not valid base64.

    The decoder must fall back to the raw bytes rather than raise.
    """
    return (
        b"From: " + FROM_ADDR.encode() + b"\r\n"
        b"To: " + TO_ADDR.encode() + b"\r\n"
        b"Subject: Corrupt base64\r\n"
        b"Date: " + DATE.encode() + b"\r\n"
        b"Message-ID: <corrupt-b64-fixture@example.com>\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"!!! this is definitely not base64 !!!\r\n"
    )


def oversized_body(size_bytes: int = 300 * 1024) -> bytes:
    """A body far larger than the preview cap, to prove the cap is applied."""
    return (
        b"From: " + FROM_ADDR.encode() + b"\r\n"
        b"To: " + TO_ADDR.encode() + b"\r\n"
        b"Subject: Very large body\r\n"
        b"Date: " + DATE.encode() + b"\r\n"
        b"Message-ID: <oversized-fixture@example.com>\r\n"
        b"\r\n" + (b"x" * size_bytes)
    )


def traversal_attachment_filename() -> bytes:
    """An attachment whose filename tries to escape the download directory.

    ``Content-Disposition`` filenames are chosen by the sender, so any
    code that joins one onto a destination path without reducing it to a
    bare component can be steered outside that directory.
    """
    msg = MIMEMultipart("mixed")
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Please open the attached"
    msg["Date"] = DATE
    msg["Message-ID"] = "<traversal-fixture@example.com>"
    msg.attach(MIMEText("See attached.\n", "plain", "utf-8"))
    part = MIMEApplication(b"payload-bytes", Name="escape")
    part["Content-Disposition"] = 'attachment; filename="../../escaped.txt"'
    msg.attach(part)
    return msg.as_bytes()


def _inline_named_probe(inline_part: MIMEText | MIMEImage, message_id: str) -> bytes:
    """Body + one *inline* part carrying a filename + one real attachment.

    The inline-with-filename shape is the one that told three MIME walks
    apart: the reader listed it, while the browser/PDF export renamed it,
    dropped it, or promoted it to being the message body.
    """
    msg = MIMEMultipart("mixed")
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Inline part with a filename"
    msg["Date"] = DATE
    msg["Message-ID"] = message_id
    msg.attach(MIMEText("hello plain body\n", "plain", "utf-8"))
    msg.attach(inline_part)
    pdf = MIMEApplication(b"%PDF fake", Name="doc.pdf")
    pdf["Content-Disposition"] = 'attachment; filename="doc.pdf"'
    msg.attach(pdf)
    return msg.as_bytes()


def inline_image_named() -> bytes:
    """Inline image that also carries ``filename="logo.png"``."""
    part = MIMEImage(_TINY_PNG, "png")
    part["Content-ID"] = "<logo@example.com>"
    part["Content-Disposition"] = 'inline; filename="logo.png"'
    return _inline_named_probe(part, "<inline-named-image@example.com>")


def inline_text_named() -> bytes:
    """Inline text/plain that also carries ``filename="notes.txt"``."""
    part = MIMEText("NOTES CONTENT", "plain", "utf-8")
    del part["Content-Disposition"]
    part["Content-Disposition"] = 'inline; filename="notes.txt"'
    return _inline_named_probe(part, "<inline-named-text@example.com>")


def inline_html_named() -> bytes:
    """Inline text/html that also carries ``filename="report.html"``.

    The nastiest of the three: the export used to render this part as
    the message body, so the browser view showed different text than the
    reader it was opened from.
    """
    part = MIMEText("<p>REPORT HTML</p>", "html", "utf-8")
    del part["Content-Disposition"]
    part["Content-Disposition"] = 'inline; filename="report.html"'
    return _inline_named_probe(part, "<inline-named-html@example.com>")


def unnamed_attachment() -> bytes:
    """``Content-Disposition: attachment`` with no ``filename`` parameter."""
    msg = MIMEMultipart("mixed")
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = "Attachment with no name"
    msg["Date"] = DATE
    msg["Message-ID"] = "<unnamed-attachment@example.com>"
    msg.attach(MIMEText("See attached.\n", "plain", "utf-8"))
    part = MIMEApplication(b"payload")
    part["Content-Disposition"] = "attachment"
    msg.attach(part)
    return msg.as_bytes()
