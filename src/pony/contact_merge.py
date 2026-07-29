"""Combining two contact records into one.

Two contacts describing the same person turn up constantly: someone is
harvested from a message as a bare name and address, and the same person
is later imported from an address book with an organisation, a second
address and notes. Deciding what the combined record holds is one policy
and it belongs in one place.

It had two. The BBDB import merged field by field — richer name wins,
distinct notes are concatenated, emails and aliases union — while
``merge_contacts``, the one the contact browser's merge key calls, moved
emails and aliases and then deleted the source, taking its name,
organisation, notes, affix and last-seen date with it. The browser picks
the lowest row id as the target, which is the oldest record and so
usually the sparse harvested stub, so the routine gesture of cleaning up
duplicates deleted precisely the details the user had typed.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .domain import Contact


def _first_non_empty(preferred: str, fallback: str) -> str:
    """*preferred* unless it is blank, in which case *fallback*."""
    return preferred or fallback


def _combined_notes(target: str, source: str) -> str:
    """Both notes, in order, without repeating one inside the other."""
    if not source:
        return target
    if not target:
        return source
    if source in target:
        return target
    return f"{target}\n{source}"


def merged_contact(target: Contact, source: Contact) -> Contact:
    """Return *target* enriched with everything *source* knows.

    Nothing is discarded. Multi-valued fields (emails, aliases, affix)
    are unioned; notes are concatenated unless one already contains the
    other; message counts add up; the earliest creation date and the
    latest sighting win. For single-valued text the surviving record's
    value is kept when it has one, and *source* fills the blanks — which
    is what makes the merge safe whichever record was chosen as target.

    The identity of *target* — its row id and its email ordering — is
    preserved, so callers can write the result straight back.
    """
    merged_emails = list(target.emails)
    merged_emails += [e for e in source.emails if e not in merged_emails]
    merged_aliases = list(target.aliases)
    merged_aliases += [a for a in source.aliases if a not in merged_aliases]
    merged_affix = list(target.affix)
    merged_affix += [a for a in source.affix if a not in merged_affix]

    last_seen = target.last_seen
    if source.last_seen is not None and (
        last_seen is None or source.last_seen > last_seen
    ):
        last_seen = source.last_seen

    return dataclasses.replace(
        target,
        first_name=_first_non_empty(target.first_name, source.first_name),
        last_name=_first_non_empty(target.last_name, source.last_name),
        organization=_first_non_empty(target.organization, source.organization),
        notes=_combined_notes(target.notes, source.notes),
        emails=tuple(merged_emails),
        aliases=tuple(merged_aliases),
        affix=tuple(merged_affix),
        message_count=target.message_count + source.message_count,
        created_at=min(target.created_at, source.created_at),
        last_seen=last_seen,
    )
