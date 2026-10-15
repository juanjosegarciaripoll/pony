"""The one rule that decides what a sync covers, for both halves.

`pony.scope` exists because the mail half and the calendar half used
the same three keys -- ``include``, ``exclude``, ``read_only`` -- for
two different rules, so one configuration file meant two things
depending on which table it sat in. The table below is the contract;
`test_both_loaders_read_the_same_three_lists_alike` is what stops it
from being two contracts again.
"""

from __future__ import annotations

import unittest

from pony.scope import SyncScope, matches, pattern_problem


class TheRuleTest(unittest.TestCase):
    """`exclude` takes a name out, `include` is the exception to it.

    Each row is a configuration, a name, and whether that name is
    synced. The rows that used to answer differently in the two halves
    are marked.
    """

    CASES = (
        # (include, exclude, name, synced)
        # Nothing declared: everything syncs.
        ((), (), "INBOX", True),
        # `include` alone narrows nothing -- it is an exception, and
        # there is nothing to make an exception to. (The calendar half
        # read this as a whitelist and synced only INBOX.)
        (("INBOX",), (), "INBOX", True),
        (("INBOX",), (), "Sent", True),
        # An empty `include` is "no exceptions", not "nothing".
        ((), (), "Anything", True),
        # `exclude` alone takes names out.
        ((), ("Spam",), "Spam", False),
        ((), ("Spam",), "INBOX", True),
        # The two together: how a scope of one name is written.
        (("INBOX",), (".*",), "INBOX", True),
        (("INBOX",), (".*",), "Trash", False),
        # A name in both lists is rescued: `include` is the exception.
        # (The calendar half let `exclude` win.)
        (("Spam",), ("Spam",), "Spam", True),
        # Patterns match the whole name, so a parent does not cover its
        # children and the children need a pattern of their own.
        (("Archive/.*",), ("Archive.*",), "Archive/2024", True),
        (("Archive/.*",), ("Archive.*",), "Archive", False),
        # `*` is the glob shorthand, and is not a valid regex.
        ((), ("*",), "INBOX", False),
        (("INBOX",), ("*",), "INBOX", True),
    )

    def test_the_table(self) -> None:
        for include, exclude, name, synced in self.CASES:
            scope = SyncScope(include=include, exclude=exclude)
            with self.subTest(include=include, exclude=exclude, name=name):
                self.assertEqual(scope.should_sync(name), synced)

    def test_read_only_only_marks(self) -> None:
        """It answers what may be done with a name, not whether it syncs."""
        scope = SyncScope(read_only=("Sent",))
        self.assertTrue(scope.should_sync("Sent"))
        self.assertTrue(scope.is_read_only("Sent"))
        self.assertFalse(scope.is_read_only("INBOX"))

    def test_read_only_does_not_rescue_from_exclude(self) -> None:
        """The mail half let it rescue; it does not any more.

        Two keys deciding scope meant `exclude = [".*"]` behaved
        differently depending on an unrelated list, and the calendar
        half never did it. Scope is `include`/`exclude`'s question
        alone.
        """
        scope = SyncScope(exclude=("Sent",), read_only=("Sent",))
        self.assertFalse(scope.should_sync("Sent"))

    def test_read_only_takes_the_glob_shorthand_too(self) -> None:
        scope = SyncScope(read_only=("*",))
        self.assertTrue(scope.is_read_only("INBOX"))
        self.assertTrue(scope.is_read_only("[Gmail]/All Mail"))

    def test_matches_anchors_the_whole_name(self) -> None:
        self.assertTrue(matches((r"\[Gmail\]/.*",), "[Gmail]/All Mail"))
        self.assertFalse(matches(("Arch",), "Archive"))
        self.assertFalse(matches((), "INBOX"))


class PatternProblemTest(unittest.TestCase):
    """What both loaders accept, decided once."""

    def test_a_usable_pattern_has_no_problem(self) -> None:
        self.assertIsNone(pattern_problem(".*"))
        self.assertIsNone(pattern_problem("Archive/[0-9]{4}"))

    def test_the_glob_shorthand_is_accepted_though_it_is_no_regex(self) -> None:
        self.assertIsNone(pattern_problem("*"))

    def test_an_empty_pattern_is_refused(self) -> None:
        problem = pattern_problem("")
        assert problem is not None
        self.assertIn("non-empty", problem)

    def test_a_broken_regex_is_refused_with_its_reason(self) -> None:
        problem = pattern_problem("[")
        assert problem is not None
        self.assertIn("invalid regex", problem)
        self.assertIn("[", problem)


class UnrescuedReadOnlyTest(unittest.TestCase):
    """The shape that changed meaning when `read_only` stopped rescuing."""

    def test_a_wholesale_exclude_strands_a_read_only_pattern(self) -> None:
        scope = SyncScope(exclude=(".*",), read_only=("Sent",))
        self.assertEqual(scope.unrescued_read_only(), ("Sent",))

    def test_an_include_that_repeats_it_is_not_stranded(self) -> None:
        scope = SyncScope(exclude=(".*",), include=("Sent",), read_only=("Sent",))
        self.assertEqual(scope.unrescued_read_only(), ())

    def test_a_narrow_exclude_is_not_analysed(self) -> None:
        """Whether two regexes overlap is not asked here."""
        scope = SyncScope(exclude=("Spam",), read_only=("Sent",))
        self.assertEqual(scope.unrescued_read_only(), ())

    def test_nothing_to_report_without_a_read_only_list(self) -> None:
        self.assertEqual(SyncScope(exclude=(".*",)).unrescued_read_only(), ())


class BothHalvesTest(unittest.TestCase):
    """The two loaders build the same scope from the same three lists.

    This is the test that would have failed before `pony.scope`: the
    mail loader read `include` as an exception and the calendar loader
    as a whitelist, so the same table meant two things. It reaches into
    both private parsers on purpose -- what matters is the pair, and
    there is nowhere else both are visible at once.
    """

    LISTS = {
        "include": ["Archive/2026"],
        "exclude": ["Archive.*", "_backup"],
        "read_only": ["Sent"],
    }

    def test_both_loaders_read_the_same_three_lists_alike(self) -> None:
        from pony.calendar.config import _parse_scope
        from pony.config import _parse_folder_config

        mail = _parse_folder_config(dict(self.LISTS))
        calendar = _parse_scope(dict(self.LISTS), "calendar.accounts[0]")

        self.assertEqual(mail, calendar)
        for name in ("Archive/2026", "Archive", "_backup", "INBOX", "Sent"):
            with self.subTest(name=name):
                self.assertEqual(mail.should_sync(name), calendar.should_sync(name))
                self.assertEqual(mail.is_read_only(name), calendar.is_read_only(name))
