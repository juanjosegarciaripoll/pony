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
