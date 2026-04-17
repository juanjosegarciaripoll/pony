"""Parse a simple keyword search query into a SearchQuery.

Supported syntax
----------------
    bare words            → body field
    from:alice            → from_address
    to:bob                → to_address
    cc:carol              → cc_address
    subject:hello         → subject
    body:world            → body (explicit)
    "quoted string"       → single token; field prefix still applies
    case:yes / case:no    → case-sensitive toggle (default: insensitive)

Multiple tokens for the same field are space-joined.
Unknown field prefixes are treated as bare body words.
"""

from __future__ import annotations

import shlex

from ..domain import SearchQuery

_FIELD_ALIASES: dict[str, str] = {
    "from": "from_address",
    "to": "to_address",
    "cc": "cc_address",
    "subject": "subject",
    "subj": "subject",
    "body": "body",
}


def parse_query(raw: str) -> SearchQuery:
    """Parse *raw* into a :class:`~pony.domain.SearchQuery`."""
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()

    buckets: dict[str, list[str]] = {f: [] for f in _FIELD_ALIASES.values()}
    case_sensitive = False

    for token in tokens:
        if ":" in token:
            prefix, _, value = token.partition(":")
            prefix_low = prefix.lower()
            if prefix_low == "case":
                case_sensitive = value.lower() in ("yes", "true", "1", "on")
                continue
            field = _FIELD_ALIASES.get(prefix_low)
            if field is not None:
                buckets[field].append(value)
                continue
        # Bare word or unknown prefix → body.
        buckets["body"].append(token)

    return SearchQuery(
        from_address=" ".join(buckets["from_address"]),
        to_address=" ".join(buckets["to_address"]),
        cc_address=" ".join(buckets["cc_address"]),
        subject=" ".join(buckets["subject"]),
        body=" ".join(buckets["body"]),
        case_sensitive=case_sensitive,
    )
