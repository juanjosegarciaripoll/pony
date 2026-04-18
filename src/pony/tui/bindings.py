"""Shared key bindings for list-style TUI widgets.

Both the contacts browser and the messages panel let the user mark
multiple rows with ``m`` / ``Shift+Down`` / ``Shift+Up`` and then act
on the marks in bulk; several list widgets also share ``n`` / ``p`` /
``<`` / ``>`` for row-cursor motion.  Only the bindings are shared —
the per-widget toggle and rendering logic is too coupled to each
host's internals to extract cleanly.
"""

from __future__ import annotations

from textual.binding import Binding

MARK_BINDINGS: tuple[Binding, ...] = (
    Binding("m", "mark_down", "Mark"),
    Binding("shift+down", "mark_down", "Mark \u2193", show=False),
    Binding("shift+up", "mark_up", "Mark \u2191", show=False),
)

MOTION_BINDINGS: tuple[Binding, ...] = (
    Binding("n", "cursor_down", "Next", show=False),
    Binding("p", "cursor_up", "Prev", show=False),
    Binding("<", "cursor_first", "First", show=False),
    Binding(">", "cursor_last", "Last", show=False),
)
