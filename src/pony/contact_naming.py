"""Turning an email display name into a contact's first and last name.

Contacts are harvested from three places — the sync/index path, the
composer after a send, and the reader's harvest action — and every one
of them must produce the *same* record for the same person.  When each
site rolled its own splitting they disagreed for any name of three or
more words, so the same correspondent ended up stored differently
depending on which door they came through.

Kept at package top level rather than under ``tui`` because
``index_store`` needs it too, and the storage layer must not import from
the UI.
"""

from __future__ import annotations

# Number of trailing words treated as the family name once a display name
# runs to three or more words.  Compound family names are the norm in
# Spanish and Portuguese naming ("Juan José García Ripoll" is first name
# "Juan José", family name "García Ripoll"), and guessing two is right
# far more often than guessing one for the corpus this client sees.
_FAMILY_NAME_WORDS = 2


def clean_display_name(display_name: str) -> str:
    """Return *display_name* unless it is really just an address.

    Some mailers echo the address as its own display name
    (``"alice@example.com" <alice@example.com>``).  Storing that as a
    person's name is wrong on its face, and worse, it blocks a later
    message carrying the real name from filling the field in.
    """
    # Only when the name *is* the address.  Testing for a bare "@"
    # anywhere threw away real names — "Bob (bob@corp) Jones" stored a
    # nameless contact.
    stripped = display_name.strip().strip("<>").strip()
    local, sep, domain = stripped.partition("@")
    if sep and local and "." in domain and " " not in stripped:
        return ""
    return display_name


def split_display_name(display_name: str) -> tuple[str, str]:
    """Split a display name into ``(first_name, last_name)``.

    Heuristic: 1 word → first only; 2 words → one each; 3 or more →
    the last two words are the family name.
    """
    parts = display_name.strip().split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    if len(parts) == 2:
        return (parts[0], parts[1])
    return (
        " ".join(parts[:-_FAMILY_NAME_WORDS]),
        " ".join(parts[-_FAMILY_NAME_WORDS:]),
    )


def harvested_name(display_name: str) -> tuple[str, str]:
    """``(first_name, last_name)`` for a display name seen on a message.

    The one entry point every harvest path should use: it drops an
    address masquerading as a name and then applies the split.
    """
    return split_display_name(clean_display_name(display_name))
