"""What the contact operations do, with no user interface attached.

Harvesting an address, deleting a marked set, merging several records
into one: each had been written inside the screen that offered it, which
is why the composer and the reader normalised an address two different
ways and why merging named its survivor in the dialog that asked about
it rather than where the decision is made.

Deciding *which* record survives a merge is model work — it is the
oldest, because its id is the lowest, and that is not something the
marks convey — so :func:`plan_merge` answers it and hands back the
labels a confirmation prompt needs.  Rendering the prompt stays with the
front end.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from email.utils import getaddresses
from typing import TYPE_CHECKING

from chronos.outcome import Notice, Outcome, info, warn

from .contact_naming import harvested_name
from .domain import Contact, IndexedMessage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .protocols import ContactRepository

# Long lists of names are summarised rather than printed in full: a
# prompt is there to be read before it is agreed to.
MAX_NAMED = 10


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Which record survives a merge, and which are folded into it."""

    target: int
    sources: tuple[int, ...]
    # Display labels in the same order, for whatever asks the user.
    target_label: str
    source_labels: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.sources) + 1


def normalize_address(address: str) -> str:
    """One spelling of an address, so two stores cannot disagree."""
    return address.lower().strip()


def harvest_addresses(
    contacts: ContactRepository | None,
    *fields: str,
) -> Outcome:
    """Add any address in *fields* that is not a contact yet.

    *fields* are raw header values — a ``To`` line, a ``Cc`` line — so
    the display names come along and are used to name a new record.  An
    address already known is left exactly as it is: the user's own
    spelling of someone's name outranks whatever a header claims.
    """
    if contacts is None:
        return Outcome()
    added = 0
    for display_name, raw in getaddresses(list(fields)):
        address = normalize_address(raw)
        if not address:
            continue
        if contacts.find_contact_by_email(email_address=address) is not None:
            continue
        first, last = harvested_name(display_name)
        contacts.upsert_contact(
            contact=Contact(
                id=None,
                first_name=first,
                last_name=last,
                emails=(address,),
            ),
        )
        added += 1
    return Outcome(changed=added)


def contact_for_address(
    contacts: ContactRepository,
    display_name: str,
    address: str,
) -> Contact:
    """The contact record an address should be edited through.

    The existing one when there is one — with the header's display name
    filled in if the record has no name at all, since a nameless contact
    is worse than a guessed one — and otherwise a new unsaved record.
    """
    normalized = normalize_address(address)
    existing = contacts.find_contact_by_email(email_address=normalized)
    first, last = harvested_name(display_name)
    if existing is None:
        return Contact(id=None, first_name=first, last_name=last, emails=(normalized,))
    if existing.first_name or existing.last_name or not display_name.strip():
        return existing
    return replace(existing, first_name=first, last_name=last)


def harvest_folder(
    contacts: ContactRepository | None,
    messages: Sequence[IndexedMessage],
    folder_name: str,
) -> Outcome:
    """Harvest every To/Cc address in *messages*."""
    if contacts is None:
        return Outcome()
    contacts.harvest_contacts(messages)
    return Outcome(
        changed=len(messages),
        notices=(
            info(
                f"Harvested contacts from {len(messages)} message(s) in {folder_name}.",
            ),
        ),
    )


def delete_contacts(
    contacts: ContactRepository,
    contact_ids: Sequence[int],
) -> Outcome:
    """Delete every record in *contact_ids*."""
    for contact_id in contact_ids:
        contacts.delete_contact(contact_id=contact_id)
    count = len(contact_ids)
    if not count:
        return Outcome()
    return Outcome(
        changed=count,
        notices=(info(f"Deleted {count} contact(s)."),),
    )


def plan_merge(
    contacts: ContactRepository,
    contact_ids: Sequence[int],
) -> MergePlan | Notice:
    """Decide which of *contact_ids* survives, or say why none can.

    The lowest id wins: it is the record that has been there longest, so
    anything already pointing at a contact keeps pointing at the right
    one.  Merging is irreversible, which is why this is answered up front
    and named in the prompt rather than discovered afterwards.
    """
    if len(contact_ids) < 2:
        return warn("Mark at least 2 contacts to merge.")
    ordered = sorted(contact_ids)
    target, *sources = ordered
    by_id = {contact.id: contact for contact in contacts.list_all_contacts()}

    def label(contact_id: int) -> str:
        contact = by_id.get(contact_id)
        if contact is None:
            return f"contact {contact_id}"
        return contact.display_name or (
            contact.emails[0] if contact.emails else "(no name)"
        )

    return MergePlan(
        target=target,
        sources=tuple(sources),
        target_label=label(target),
        source_labels=tuple(label(contact_id) for contact_id in sources),
    )


def merge_contacts(contacts: ContactRepository, plan: MergePlan) -> Outcome:
    """Fold *plan*'s sources into its target."""
    contacts.merge_contacts(target_id=plan.target, source_ids=list(plan.sources))
    return Outcome(
        changed=plan.total,
        notices=(info(f"Merged {plan.total} contacts."),),
    )


def summarize(labels: Sequence[str], *, prefix: str = "  - ") -> str:
    """Up to :data:`MAX_NAMED` labels, one per line, then a count.

    Both the delete and the merge prompt list what they are about to act
    on, and both have to stop somewhere; stopping in the same place means
    the two prompts look like one program.
    """
    shown = [f"{prefix}{label}" for label in labels[:MAX_NAMED]]
    body = "\n".join(shown)
    remaining = len(labels) - MAX_NAMED
    if remaining > 0:
        body += f"\n  … and {remaining} more"
    return body


__all__ = [
    "MAX_NAMED",
    "MergePlan",
    "contact_for_address",
    "delete_contacts",
    "harvest_addresses",
    "harvest_folder",
    "merge_contacts",
    "normalize_address",
    "plan_merge",
    "summarize",
]
