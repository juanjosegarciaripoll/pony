"""Folder name discovery helpers for Pony Express.

Used by the composer to locate Sent and Drafts folders without requiring
explicit configuration.
"""

from __future__ import annotations


def find_folder(candidates: list[str], hint: str) -> str | None:
    """Return the candidate whose name best matches *hint*.

    Matching priority (case-insensitive):

    1. Exact match               — ``"Sent"`` matches ``"sent"``
    2. Ends-with-separator match — ``"INBOX/Sent"`` matches ``"Sent"``
    3. Contains match            — ``"[Gmail]/Sent Mail"`` matches ``"Sent"``

    Returns ``None`` if no candidate satisfies any of the criteria.
    """
    hint_lower = hint.lower()

    # Pass 1: exact
    for name in candidates:
        if name.lower() == hint_lower:
            return name

    # Pass 2: ends with a path separator followed by hint
    for name in candidates:
        lower = name.lower()
        if lower.endswith(f"/{hint_lower}") or lower.endswith(f".{hint_lower}"):
            return name

    # Pass 3: contains hint as a substring
    for name in candidates:
        if hint_lower in name.lower():
            return name

    return None
