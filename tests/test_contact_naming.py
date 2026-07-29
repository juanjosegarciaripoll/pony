"""One display name must yield one contact, whichever door it comes through.

Contacts are harvested from three places: the index during sync, the
composer after a send, and the reader's harvest action.  Each used to
split names its own way, so the same correspondent was stored
differently depending on the entry point — the sync path handled
compound family names, the two TUI paths did not, and only the sync path
discarded an address masquerading as a name.

These tests pin the shared implementation and, more importantly, that
the three paths still agree.
"""

from __future__ import annotations

import unittest

from pony.contact_naming import (
    clean_display_name,
    harvested_name,
    split_display_name,
)


class SplitDisplayNameTest(unittest.TestCase):
    """The word-count heuristic."""

    def test_an_empty_name_yields_two_empty_parts(self) -> None:
        self.assertEqual(split_display_name(""), ("", ""))
        self.assertEqual(split_display_name("   "), ("", ""))

    def test_a_mononym_is_a_first_name(self) -> None:
        self.assertEqual(split_display_name("Prince"), ("Prince", ""))

    def test_two_words_split_one_and_one(self) -> None:
        self.assertEqual(split_display_name("Alice Smith"), ("Alice", "Smith"))

    def test_three_words_keep_a_compound_family_name(self) -> None:
        """Spanish and Portuguese names carry two family names."""
        self.assertEqual(
            split_display_name("Juan José García"), ("Juan", "José García")
        )

    def test_four_words_split_evenly(self) -> None:
        self.assertEqual(
            split_display_name("Juan José García Ripoll"),
            ("Juan José", "García Ripoll"),
        )

    def test_surrounding_whitespace_is_ignored(self) -> None:
        self.assertEqual(split_display_name("  Alice   Smith  "), ("Alice", "Smith"))


class CleanDisplayNameTest(unittest.TestCase):
    """An address is not a name."""

    def test_an_address_echoed_as_a_name_is_discarded(self) -> None:
        self.assertEqual(clean_display_name("alice@example.com"), "")

    def test_a_real_name_is_kept(self) -> None:
        self.assertEqual(clean_display_name("Alice Smith"), "Alice Smith")


class HarvestedNameTest(unittest.TestCase):
    def test_an_address_never_becomes_a_first_name(self) -> None:
        self.assertEqual(harvested_name("alice@example.com"), ("", ""))

    def test_a_real_name_is_split(self) -> None:
        self.assertEqual(
            harvested_name("Juan José García Ripoll"),
            ("Juan José", "García Ripoll"),
        )


class HarvestPathsAgreeTest(unittest.TestCase):
    """The regression this refactor exists to prevent.

    Every harvest entry point is driven with the same display name and
    the resulting contact records are compared.  They must be identical:
    the same person seen in a sync and in the composer is one contact,
    not two spellings of one.
    """

    _NAMES = (
        "Alice",
        "Alice Smith",
        "Juan José García",
        "Juan José García Ripoll",
        "Maria del Carmen Rodriguez Perez",
        "alice@example.com",
        "",
    )

    def test_the_index_and_the_composer_store_the_same_name(self) -> None:
        import dataclasses
        from datetime import UTC, datetime

        from tui_helpers import make_index, make_tmp_paths

        from pony.domain import IndexedMessage, MessageRef, MessageStatus

        for display in self._NAMES:
            with self.subTest(display=display):
                index = make_index(make_tmp_paths("harvest-agree"))
                address = "person@example.test"
                recipients = f'"{display}" <{address}>' if display else f"<{address}>"
                message = IndexedMessage(
                    message_ref=MessageRef(
                        account_name="acct", folder_name="INBOX", id=0
                    ),
                    message_id="<harvest@example.test>",
                    sender="sender@example.test",
                    recipients=recipients,
                    cc="",
                    subject="Subject",
                    body_preview="Body",
                    storage_key="key",
                    local_flags=frozenset(),
                    base_flags=frozenset(),
                    local_status=MessageStatus.ACTIVE,
                    received_at=datetime(2026, 4, 17, 12, tzinfo=UTC),
                )
                saved = index.insert_message(message=message)
                index.harvest_contacts(messages=[dataclasses.replace(saved)])

                stored = index.find_contact_by_email(email_address=address)
                assert stored is not None
                # What the composer / reader paths would have produced.
                expected_first, expected_last = harvested_name(display)

                self.assertEqual(stored.first_name, expected_first)
                self.assertEqual(stored.last_name, expected_last)


class CleanDisplayNameBreadthTest(unittest.TestCase):
    """Reject a name that *is* an address, not any name containing one."""

    def test_an_address_used_as_its_own_name_is_dropped(self) -> None:
        self.assertEqual(clean_display_name("alice@example.com"), "")
        self.assertEqual(clean_display_name("<alice@example.com>"), "")

    def test_a_real_name_containing_an_address_survives(self) -> None:
        # Testing for a bare "@" anywhere stored this contact nameless.
        self.assertEqual(
            clean_display_name("Bob (bob@corp.com) Jones"), "Bob (bob@corp.com) Jones"
        )

    def test_ordinary_names_are_untouched(self) -> None:
        for name in ("Ana Ruiz", "O'Brien", "José García-Ñuñez"):
            self.assertEqual(clean_display_name(name), name)
