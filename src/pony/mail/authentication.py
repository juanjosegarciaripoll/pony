"""What the receiving server says about who really sent a message.

Pony verifies nothing itself: DKIM is a signature check against DNS, and
a mail reader is the wrong place to do it — by the time the message is
in the mirror the key may have rotated, and a reader that failed open
would be worse than one that says nothing.  The server that accepted the
message did check, and wrote down what it found in
``Authentication-Results`` (RFC 8601).  This reads that.

Two things make the difference between a useful indicator and a
misleading one.

**Only the topmost header counts.**  A receiving server prepends its
own; a message can arrive already carrying any number of forged ones,
and an attacker's `dkim=pass` is one line of text.  So exactly one
header is read, the first, and anything below it is ignored and
counted.

**A passing signature is not a matching signature.**  ``dkim=pass``
means *somebody* signed the message, and in phishing that somebody is
usually the sender's own throwaway domain.  What matters is whether the
signing domain is the domain the message claims to be from, which is
what DMARC evaluates and what :func:`parse_authentication` compares
when only DKIM is reported.  Without that comparison the indicator
would reassure the reader about exactly the mail it should not.

A third thing is a deliberate omission: ``spf=pass`` on its own is not
treated as a pass.  SPF authenticates the envelope sender, not the
``From`` header a reader is looking at, and the two differ for every
mailing list, so an aligned SPF alone says less than it appears to.
DKIM and DMARC are the positive signals.

The verdict says who vouched for what, never that Pony checked it; the
wording belongs to the front ends, which get it from :meth:`label`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parseaddr
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from email.message import EmailMessage

__all__ = [
    "Authenticity",
    "MessageAuthentication",
    "UNKNOWN_AUTHENTICATION",
    "parse_authentication",
]

#: Methods worth reading out of the header; the rest are noise here.
_METHODS = ("dmarc", "dkim", "spf", "arc")

#: Of the outcomes RFC 8601 s2.7 defines, only `fail` is evidence
#: against a message.  `softfail`, `neutral`, `none`, `temperror` and
#: `permerror` are degrees of silence, and `softfail` in particular is
#: what ordinary forwarded mail gets, so treating it as a failure would
#: mark half an inbox suspicious and teach the reader to ignore the
#: indicator.
_FAILURES = ("fail",)

_TOKEN = re.compile(r"\A[A-Za-z0-9._-]+\Z")


class Authenticity(StrEnum):
    """What the receiving server's report amounts to."""

    #: The signing domain is the domain the message claims to be from.
    PASSED = "passed"
    #: Something signed it, but not the domain in `From`.
    UNALIGNED = "unaligned"
    #: The server checked and the message did not pass.
    FAILED = "failed"
    #: No report, or nothing in it that says either way.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MessageAuthentication:
    """The first ``Authentication-Results`` header, read."""

    verdict: Authenticity = Authenticity.UNKNOWN
    #: The domain in `From`, which is what the reader is being shown.
    from_domain: str = ""
    #: The domain that actually vouched for the message, when one did.
    signed_domain: str = ""
    #: `(method, result)` pairs, in the order the server wrote them.
    methods: tuple[tuple[str, str], ...] = ()
    #: The server that did the checking (the header's authserv-id).
    reporter: str = ""
    #: How many headers were present.  More than one means the message
    #: arrived carrying reports that are not this server's.
    extra_reports: int = 0

    @property
    def worth_showing(self) -> bool:
        """False when no server reported, so the detail has nothing in it."""
        return self.verdict is not Authenticity.UNKNOWN

    @property
    def icon(self) -> str:
        """The one character a header line carries.

        Shut only for an aligned pass: a shut lock on a message that
        failed its check would be a lie, however briefly told, and a
        signature belonging to some other domain has not confirmed this
        sender either.  A check that ran and failed gets a third glyph
        rather than sharing the open lock with the ordinary case of no
        report at all, because those two are not the same news and a
        reader should not have to click to tell them apart.
        """
        match self.verdict:
            case Authenticity.PASSED:
                return "\U0001f512"  # shut padlock
            case Authenticity.FAILED:
                return "\u26a0\ufe0f"  # warning sign
            case Authenticity.UNALIGNED | Authenticity.UNKNOWN:
                return "\U0001f513"  # open padlock

    def headline(self) -> str:
        """The dialog's title: four words at most, no jargon."""
        match self.verdict:
            case Authenticity.PASSED:
                return "Sender confirmed"
            case Authenticity.UNALIGNED:
                return "Sender not confirmed"
            case Authenticity.FAILED:
                return "Sender check failed"
            case Authenticity.UNKNOWN:
                return "Sender not checked"

    def meaning(self) -> str:
        """What the verdict means, in one sentence.

        A shut lock is read as "safe", and this is the only place that
        can say it means something narrower.
        """
        match self.verdict:
            case Authenticity.PASSED:
                domain = self.from_domain or "the domain in From"
                return (
                    f"{domain} really did sign this message, which rules out "
                    "a forgery of that domain but says nothing about whether "
                    "its owner is honest."
                )
            case Authenticity.UNALIGNED:
                signer = self.signed_domain or "another domain"
                claimed = self.from_domain or "the domain in From"
                return (
                    f"Signed by {signer} rather than {claimed}, which is "
                    "normal for mail a service sends on someone's behalf and "
                    "is also what phishing looks like."
                )
            case Authenticity.FAILED:
                return (
                    "The server that accepted this message checked and it did "
                    "not pass, which can mean forgery, tampering in transit, "
                    "or a forwarder that rewrote it."
                )
            case Authenticity.UNKNOWN:
                return (
                    "The server that accepted this message did not check, "
                    "which is common and not suspicious by itself."
                )

    def label(self) -> str:
        """One short line for a header area."""
        methods = "+".join(name for name, _ in self.methods) or "no method"
        match self.verdict:
            case Authenticity.PASSED:
                return f"{self.signed_domain} confirmed by {methods}"
            case Authenticity.UNALIGNED:
                signer = self.signed_domain or "another domain"
                return f"signed by {signer}, not by {self.from_domain}"
            case Authenticity.FAILED:
                failed = "+".join(
                    name for name, result in self.methods if result in _FAILURES
                )
                return f"failed {failed or methods}"
            case Authenticity.UNKNOWN:
                return "not checked"

    def detail(self) -> str:
        """The same thing at length, for a page with room for it."""
        parts = [f"{name}={result}" for name, result in self.methods]
        if self.reporter:
            parts.append(f"reported by {self.reporter}")
        if self.extra_reports:
            plural = "" if self.extra_reports == 1 else "s"
            parts.append(
                f"ignored {self.extra_reports} other report{plural} the message carried"
            )
        return "; ".join(parts)


UNKNOWN_AUTHENTICATION = MessageAuthentication()


def _strip_comments(text: str) -> str:
    """Remove ``(...)`` comments, which may nest, per RFC 5322 s3.2.2."""
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _domain_of(address: str) -> str:
    _name, email_address = parseaddr(address)
    _, _, domain = email_address.rpartition("@")
    domain = domain.strip().strip("[]").lower().rstrip(".")
    return domain if _TOKEN.fullmatch(domain) else ""


def _aligned(signed: str, claimed: str) -> bool:
    """Relaxed alignment: the same domain, or one inside the other.

    A full answer needs the public-suffix list to find each side's
    organizational domain, which is a dependency and a data file that
    goes stale.  Subdomain containment is what relaxed alignment means
    in all but the cases where two different registrants share a suffix
    Pony cannot know about, and those resolve the safe way round: the
    pair is reported as unaligned rather than as a pass.
    """
    if not signed or not claimed:
        return False
    return signed == claimed or claimed.endswith(f".{signed}")


def parse_authentication(msg: EmailMessage) -> MessageAuthentication:
    """Read the first ``Authentication-Results`` header on *msg*."""
    headers = msg.get_all("Authentication-Results") or []
    from_domain = _domain_of(str(msg.get("From", "")))
    if not headers:
        return MessageAuthentication(from_domain=from_domain)

    fields = _strip_comments(str(headers[0])).split(";")
    reporter = fields[0].strip().split()[0].lower() if fields[0].strip() else ""
    results: dict[str, str] = {}
    properties: dict[str, dict[str, str]] = {}
    order: list[str] = []
    method = ""
    for field in fields[1:]:
        for item in field.split():
            name, _, value = item.partition("=")
            name, value = name.strip().lower(), value.strip().strip('"')
            if not value:
                continue
            if name in _METHODS:
                method = name
                if name not in results:
                    order.append(name)
                results[name] = value.lower()
            elif method and "." in name:
                properties.setdefault(method, {})[name] = value.lower()

    methods = tuple((name, results[name]) for name in order)
    dkim_domain = properties.get("dkim", {}).get("header.d", "")
    dmarc_domain = properties.get("dmarc", {}).get("header.from", "") or from_domain

    if results.get("dmarc") == "pass":
        verdict = Authenticity.PASSED
        signed = dmarc_domain
    elif results.get("dkim") == "pass" and _aligned(dkim_domain, from_domain):
        verdict = Authenticity.PASSED
        signed = dkim_domain
    elif any(results.get(name) in _FAILURES for name in ("dmarc", "dkim", "spf")):
        verdict = Authenticity.FAILED
        signed = dkim_domain
    elif results.get("dkim") == "pass":
        verdict = Authenticity.UNALIGNED
        signed = dkim_domain
    else:
        verdict = Authenticity.UNKNOWN
        signed = ""

    return MessageAuthentication(
        verdict=verdict,
        from_domain=from_domain,
        signed_domain=signed,
        methods=methods,
        reporter=reporter if _TOKEN.fullmatch(reporter) else "",
        extra_reports=len(headers) - 1,
    )
