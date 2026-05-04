"""Tests for the pure-function parts of ``pony.tui.widgets.folder_panel``.

The widget itself needs a running Textual app to mount; this file
exercises the builder that shapes what the widget displays —
``build_folder_tree`` — so the nesting + unread-aggregation logic can
be pinned without spinning up the UI.
"""

from __future__ import annotations

import unittest

from pony.domain import FolderRef
from pony.tui.widgets.folder_panel import (
    FolderTreeNode,
    _split_folder_name,
    build_folder_tree,
)


def _build(
    names: list[str],
    *,
    unread: dict[str, int] | None = None,
) -> tuple[FolderTreeNode, ...]:
    return build_folder_tree(
        folder_names=tuple(names),
        unread_counts=unread or {n: 0 for n in names},
        account_name="personal",
    )


class SplitFolderNameTest(unittest.TestCase):
    """The delimiter-detection heuristic.  INBOX is untouchable."""

    def test_inbox_is_never_split(self) -> None:
        self.assertEqual(_split_folder_name("INBOX"), ("INBOX",))

    def test_dotted_path_splits_on_dot(self) -> None:
        self.assertEqual(
            _split_folder_name("Archives.2026"),
            ("Archives", "2026"),
        )

    def test_slashed_path_splits_on_slash(self) -> None:
        self.assertEqual(
            _split_folder_name("Lists/Unions"),
            ("Lists", "Unions"),
        )

    def test_first_delimiter_wins_when_both_appear(self) -> None:
        # Dot appears before slash: whole name treated as dot-delimited.
        self.assertEqual(
            _split_folder_name("a.b/c"),
            ("a", "b/c"),
        )

    def test_flat_name_returns_single_segment(self) -> None:
        self.assertEqual(_split_folder_name("Drafts"), ("Drafts",))

    def test_empty_segments_are_dropped(self) -> None:
        self.assertEqual(_split_folder_name(".foo"), ("foo",))
        self.assertEqual(_split_folder_name("foo."), ("foo",))
        self.assertEqual(_split_folder_name("a..b"), ("a", "b"))


class BuildFolderTreeTest(unittest.TestCase):
    """Shape + aggregation contract for the nested builder."""

    def test_inbox_pinned_first_even_when_alphabetically_later(self) -> None:
        tree = _build(["Archives", "Drafts", "INBOX"])
        labels = [n.label for n in tree]
        self.assertEqual(labels[0], "INBOX")
        # Remaining siblings are alphabetical.
        self.assertEqual(labels[1:], ["Archives", "Drafts"])

    def test_dotted_creates_synthetic_parent(self) -> None:
        """With only ``Archives.2026`` on the server, ``Archives`` must
        appear as a synthetic parent (no FolderRef, not selectable)
        with the real folder as its child."""
        tree = _build(["Archives.2026"])
        [archives] = tree
        self.assertEqual(archives.label, "Archives")
        self.assertIsNone(archives.folder_ref)
        self.assertEqual(len(archives.children), 1)
        [y2026] = archives.children
        self.assertEqual(y2026.label, "2026")
        self.assertEqual(
            y2026.folder_ref,
            FolderRef(account_name="personal", folder_name="Archives.2026"),
        )

    def test_real_parent_keeps_its_folder_ref(self) -> None:
        """When both ``Archives`` and ``Archives.2026`` exist on the
        server, the parent is a real folder (selectable) with the child
        nested beneath it — not a synthetic node."""
        tree = _build(["Archives", "Archives.2026"])
        [archives] = tree
        self.assertEqual(archives.label, "Archives")
        self.assertEqual(
            archives.folder_ref,
            FolderRef(account_name="personal", folder_name="Archives"),
        )
        self.assertEqual(len(archives.children), 1)

    def test_mixed_delimiters_across_names(self) -> None:
        tree = _build(["Archives.2026", "Lists/Unions"])
        labels = sorted(n.label for n in tree)
        self.assertEqual(labels, ["Archives", "Lists"])
        archives = next(n for n in tree if n.label == "Archives")
        lists = next(n for n in tree if n.label == "Lists")
        self.assertEqual(archives.children[0].label, "2026")
        self.assertEqual(lists.children[0].label, "Unions")

    def test_multi_level_nesting(self) -> None:
        tree = _build(["a.b.c"])
        [a] = tree
        self.assertEqual(a.label, "a")
        [b] = a.children
        self.assertEqual(b.label, "b")
        [c] = b.children
        self.assertEqual(c.label, "c")
        self.assertEqual(
            c.folder_ref,
            FolderRef(account_name="personal", folder_name="a.b.c"),
        )

    def test_own_unread_only_on_real_nodes(self) -> None:
        tree = _build(
            ["Archives.2026"],
            unread={"Archives.2026": 5},
        )
        [archives] = tree
        self.assertEqual(archives.own_unread, 0)  # synthetic
        [y2026] = archives.children
        self.assertEqual(y2026.own_unread, 5)

    def test_descendant_unread_aggregates_through_synthetic_parents(self) -> None:
        """This is the load-bearing invariant for the dim-vs-bright
        rule: a synthetic parent must light up when any descendant has
        unread, even though its own_unread is always 0."""
        tree = _build(
            ["Archives.2024", "Archives.2025", "Archives.2026"],
            unread={"Archives.2024": 0, "Archives.2025": 0, "Archives.2026": 7},
        )
        [archives] = tree
        self.assertEqual(archives.own_unread, 0)
        self.assertEqual(archives.descendant_unread(), 7)

    def test_descendant_unread_zero_when_all_children_quiet(self) -> None:
        tree = _build(
            ["Archives.2024", "Archives.2025"],
            unread={"Archives.2024": 0, "Archives.2025": 0},
        )
        [archives] = tree
        self.assertEqual(archives.descendant_unread(), 0)

    def test_flat_folders_stay_flat(self) -> None:
        tree = _build(["INBOX", "Sent", "Drafts"])
        for node in tree:
            self.assertEqual(node.children, ())
            self.assertIsNotNone(node.folder_ref)

    def test_sibling_sort_at_deep_levels(self) -> None:
        """Siblings must be alphabetically sorted at every level so
        the display is stable across runs — dict iteration order
        inside the builder could otherwise leak through."""
        tree = _build(["x.c", "x.a", "x.b"])
        [x] = tree
        self.assertEqual([c.label for c in x.children], ["a", "b", "c"])
