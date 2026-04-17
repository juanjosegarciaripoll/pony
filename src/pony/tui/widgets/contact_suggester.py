"""Textual Suggester that completes email addresses from the contacts store."""

from __future__ import annotations

from email.utils import formataddr

from textual.suggester import Suggester

from ...protocols import ContactRepository


class ContactSuggester(Suggester):
    """Complete a comma-separated address field against the contacts store.

    Handles multi-address values such as ``"Alice <a@ex.com>, Bo"`` by
    isolating the last token being typed, looking it up, and returning the
    full reconstructed value with the suggestion appended.
    """

    def __init__(self, contacts: ContactRepository) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self._contacts = contacts

    async def get_suggestion(self, value: str) -> str | None:
        # Isolate the token being typed (everything after the last comma).
        last_comma = value.rfind(",")
        if last_comma == -1:
            prefix_part = ""
            typed = value
        else:
            prefix_part = value[: last_comma + 1] + " "
            typed = value[last_comma + 1 :].lstrip()

        if len(typed) < 2:
            return None

        results = self._contacts.search_contacts(prefix=typed, limit=1)
        if not results:
            return None

        contact = results[0]
        name = contact.display_name
        email = contact.primary_email
        if not email:
            return None
        suggestion = formataddr((name, email)) if name else email

        return prefix_part + suggestion
