"""Tests for pony.folder_utils.find_folder."""

from __future__ import annotations

import unittest

from pony.folder_utils import find_folder


class FindFolderExactMatchTest(unittest.TestCase):
    def test_exact_case_insensitive(self) -> None:
        assert find_folder(["Sent", "Drafts", "INBOX"], "sent") == "Sent"

    def test_exact_match_preserved_case(self) -> None:
        assert find_folder(["SENT", "Inbox"], "SENT") == "SENT"

    def test_exact_preferred_over_contains(self) -> None:
        # "Sent" is an exact match; "Sent Mail" is a contains match.
        assert find_folder(["Sent Mail", "Sent"], "Sent") == "Sent"


class FindFolderSeparatorMatchTest(unittest.TestCase):
    def test_slash_separator(self) -> None:
        assert find_folder(["INBOX/Sent"], "Sent") == "INBOX/Sent"

    def test_dot_separator(self) -> None:
        assert find_folder(["INBOX.Drafts"], "Drafts") == "INBOX.Drafts"

    def test_separator_preferred_over_contains(self) -> None:
        candidates = ["[Gmail]/Sent Mail", "INBOX/Sent"]
        assert find_folder(candidates, "Sent") == "INBOX/Sent"

    def test_separator_not_triggered_without_separator_char(self) -> None:
        # "MySent" ends with "sent" but has no separator → not a separator match.
        # Falls through to contains match.
        assert find_folder(["MySent"], "Sent") == "MySent"


class FindFolderContainsMatchTest(unittest.TestCase):
    def test_gmail_sent_mail(self) -> None:
        assert find_folder(["[Gmail]/Sent Mail"], "Sent") == "[Gmail]/Sent Mail"

    def test_case_insensitive_contains(self) -> None:
        assert find_folder(["[Gmail]/All Mail"], "all") == "[Gmail]/All Mail"


class FindFolderNoMatchTest(unittest.TestCase):
    def test_empty_candidates(self) -> None:
        assert find_folder([], "Sent") is None

    def test_no_match(self) -> None:
        assert find_folder(["INBOX", "Trash"], "Sent") is None


if __name__ == "__main__":
    unittest.main()
