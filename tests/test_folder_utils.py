"""Tests for pony.folder_utils."""

from __future__ import annotations

import unittest

from pony.folder_utils import find_folder, is_sent_folder


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


class IsSentFolderTest(unittest.TestCase):
    """Recognising the Sent folder without SPECIAL-USE data.

    The message list swaps its From column for a To column here, so a
    false positive on an ordinary folder hides the sender.
    """

    def test_english_names(self) -> None:
        for name in ("Sent", "sent", "Sent Items", "Sent Mail", "Sent Messages"):
            assert is_sent_folder(name), name

    def test_localised_names(self) -> None:
        for name in (
            "Enviados",
            "Enviats",
            "Envoyés",
            "Gesendete Objekte",
            "Posta inviata",
            "Verzonden items",
            "Wysłane",
            "Отправленные",
            "送信済み",
        ):
            assert is_sent_folder(name), name

    def test_diacritics_are_optional(self) -> None:
        # Servers differ on composed vs decomposed forms, and some strip
        # the accents outright.
        assert is_sent_folder("Envoyes")
        assert is_sent_folder("Envoye\u0301s")  # e + combining acute

    def test_only_the_last_path_segment_counts(self) -> None:
        assert is_sent_folder("INBOX/Enviados")
        assert is_sent_folder("INBOX.Sent")
        assert is_sent_folder("[Gmail]/Sent Mail")

    def test_a_folder_filed_under_sent_is_not_the_sent_folder(self) -> None:
        # Archives of old sent mail hold correspondence with many people;
        # their From column is worth keeping.
        assert not is_sent_folder("Sent/2019")
        assert not is_sent_folder("INBOX.Sent.Archive")

    def test_ordinary_folders_are_not_sent_folders(self) -> None:
        for name in ("INBOX", "Drafts", "Trash", "Sent-ish", "Presentations"):
            assert not is_sent_folder(name), name


if __name__ == "__main__":
    unittest.main()
