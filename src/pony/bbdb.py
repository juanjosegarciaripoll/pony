"""BBDB v3 (format 9) reader and writer.

Reads and writes the Emacs Big Brother Database file format so that
Pony's contacts store can interoperate with Emacs BBDB.  Each record
is a Lisp vector on one line; the file starts with a format header.

Only the subset of fields that Pony tracks is round-tripped faithfully.
Phone and address fields are preserved as opaque strings if present in
an imported file, but Pony never generates them.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .domain import Contact

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BBDB_HEADER = """\
;; -*-coding: utf-8-emacs;-*-
;;; file-format: 9
"""


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def write_bbdb(contacts: list[Contact], path: Path) -> None:
    """Write a BBDB v3 file from a list of Contact records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_BBDB_HEADER]
    for contact in contacts:
        lines.append(_contact_to_bbdb_line(contact))
    path.write_text("".join(lines), encoding="utf-8")


def _contact_to_bbdb_line(contact: Contact) -> str:
    """Serialize one Contact as a BBDB v3 record line."""
    fields = [
        _lisp_string(contact.first_name),  # 0: firstname
        _lisp_string(contact.last_name),  # 1: lastname
        _lisp_string_list(contact.affix),  # 2: affix
        _lisp_string_list(contact.aliases),  # 3: aka
        _lisp_string_list(
            (contact.organization,) if contact.organization else ()
        ),  # 4: organization
        "nil",  # 5: phone
        "nil",  # 6: address
        _lisp_string_list(contact.emails),  # 7: mail
        _lisp_xfields(contact.notes),  # 8: xfields
        f'(bbdb-id . "{uuid.uuid4()}")',  # 9: uuid
        f'(creation-date . "{contact.created_at:%Y-%m-%d}")',  # 10: creation-date
        f'(timestamp . "{contact.updated_at:%Y-%m-%d}")',  # 11: timestamp
        "nil",  # 12: cache
    ]
    return "[" + " ".join(fields) + "]\n"


def _lisp_string(value: str) -> str:
    if not value:
        return "nil"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _lisp_string_list(items: tuple[str, ...]) -> str:
    if not items:
        return "nil"
    return "(" + " ".join(_lisp_string(s) for s in items) + ")"


def _lisp_xfields(notes: str) -> str:
    if not notes:
        return "nil"
    return f"((notes . {_lisp_string(notes)}))"


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def read_bbdb(path: Path) -> list[Contact]:
    """Parse a BBDB v3 file and return Contact records."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    contacts: list[Contact] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            contact = _parse_bbdb_record(line)
            if contact is not None:
                contacts.append(contact)
    return contacts


def _parse_bbdb_record(line: str) -> Contact | None:
    """Parse one BBDB record line into a Contact."""
    # Strip the outer brackets.
    inner = line[1:-1].strip()
    fields = _parse_sexp_list(inner)
    if len(fields) < 8:
        return None

    first_name = _sexp_to_string(fields[0])
    last_name = _sexp_to_string(fields[1])
    affix = _sexp_to_string_tuple(fields[2])
    aliases = _sexp_to_string_tuple(fields[3])
    org_list = _sexp_to_string_tuple(fields[4])
    organization = org_list[0] if org_list else ""
    emails = _sexp_to_string_tuple(fields[7])
    notes = _extract_notes(fields[8]) if len(fields) > 8 else ""

    # Parse dates from fields 10-11 if available.
    created_at = _parse_bbdb_date(fields[10]) if len(fields) > 10 else None
    updated_at = _parse_bbdb_date(fields[11]) if len(fields) > 11 else None
    now = datetime.now(tz=UTC)

    return Contact(
        id=None,
        first_name=first_name,
        last_name=last_name,
        affix=affix,
        aliases=aliases,
        organization=organization,
        notes=notes,
        emails=emails,
        created_at=created_at or now,
        updated_at=updated_at or now,
    )


# ---------------------------------------------------------------------------
# S-expression parser (minimal, BBDB-specific)
# ---------------------------------------------------------------------------

# Tokenizer: strings, parens, dots, nil, symbols, dotted pairs.
_TOKEN_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"'  # quoted string
    r"|[(\)\[\]]"  # brackets/parens
    r"|nil"  # nil literal
    r"|\.(?=\s)"  # dot (for dotted pairs)
    r"|[^\s()\[\]\".]+"  # bare symbol/number
)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _parse_sexp_list(text: str) -> list[object]:
    """Parse the top-level fields of a BBDB record vector.

    Returns a flat list of parsed S-expressions (one per field).
    """
    tokens = _tokenize(text)
    result: list[object] = []
    pos = 0
    while pos < len(tokens):
        val, pos = _parse_sexp(tokens, pos)
        result.append(val)
    return result


def _parse_sexp(tokens: list[str], pos: int) -> tuple[object, int]:
    """Parse one S-expression starting at *pos*.  Returns (value, new_pos)."""
    if pos >= len(tokens):
        return None, pos

    tok = tokens[pos]

    if tok == "nil":
        return None, pos + 1

    if tok.startswith('"'):
        # Quoted string — strip quotes and unescape.
        s = tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return s, pos + 1

    if tok == "(":
        # List or alist.
        items: list[object] = []
        pos += 1
        while pos < len(tokens) and tokens[pos] != ")":
            val, pos = _parse_sexp(tokens, pos)
            # Handle dotted pair: (symbol . value)
            if pos < len(tokens) and tokens[pos] == ".":
                pos += 1  # skip dot
                val2, pos = _parse_sexp(tokens, pos)
                items.append((val, val2))
                continue
            items.append(val)
        if pos < len(tokens):
            pos += 1  # skip ')'
        return items, pos

    if tok == "[":
        # Vector (treated same as list for our purposes).
        items2: list[object] = []
        pos += 1
        while pos < len(tokens) and tokens[pos] != "]":
            val, pos = _parse_sexp(tokens, pos)
            items2.append(val)
        if pos < len(tokens):
            pos += 1  # skip ']'
        return items2, pos

    # Bare symbol or number.
    return tok, pos + 1


# ---------------------------------------------------------------------------
# S-expression value extractors
# ---------------------------------------------------------------------------


def _sexp_to_string(value: object) -> str:
    """Extract a string from an S-expression value (string or nil)."""
    if isinstance(value, str):
        return value
    return ""


def _sexp_to_string_tuple(value: object) -> tuple[str, ...]:
    """Extract a list of strings from an S-expression list or nil."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def _extract_notes(xfields: object) -> str:
    """Extract the 'notes' value from an xfields alist.

    The parsed S-expression for ``((notes . "text"))`` is a nested
    list: ``[[("notes", "text")]]``.  We flatten and search.
    """
    if not isinstance(xfields, list):
        return ""
    for item in xfields:
        if isinstance(item, tuple) and len(item) == 2:
            key, val = item
            if key == "notes" and isinstance(val, str):
                return val
        # Handle nested lists (the outer list wrapping the alist).
        if isinstance(item, list):
            result = _extract_notes(item)
            if result:
                return result
    return ""


def _parse_bbdb_date(value: object) -> datetime | None:
    """Parse a BBDB date from various formats.

    Handles:
    - Dotted pair: ``(creation-date . "2026-04-12")``
    - Bare date string: ``"2026-04-12"``
    - Bare datetime string: ``"2019-04-29 09:27:04 +0000"``
    """
    if isinstance(value, str):
        return _parse_date_string(value)
    if isinstance(value, tuple) and len(value) == 2:
        _, date_str = value
        if isinstance(date_str, str):
            return _parse_date_string(date_str)
    if isinstance(value, list):
        for item in value:
            result = _parse_bbdb_date(item)
            if result:
                return result
    return None


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",  # "2019-04-29 09:27:04 +0000"
    "%Y-%m-%d %H:%M:%S",  # "2019-04-29 09:27:04"
    "%Y-%m-%d",  # "2019-04-29"
)


def _parse_date_string(s: str) -> datetime | None:
    """Try multiple date formats and return the first that succeeds."""
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
        except ValueError:
            continue
    return None
