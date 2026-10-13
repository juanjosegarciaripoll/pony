"""What both front ends do with a background operation's result.

An action module returns a :class:`~chronos.outcome.Outcome`: a count of
what changed and the notices to show.  Turning those notices into toasts
is three lines, and it was written out five times across the two front
ends' screens -- each with its own docstring and its own choice of
whether to notify the screen or the application.  Getting onto the
thread that may raise them was written twice, with the same docstring.

Both live in ``chronos`` because the dependency arrow points that way:
the mail reader's screens and app import them as readily as the
calendar's.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Protocol

from chronos.outcome import Outcome, Severity


class Notifier(Protocol):
    """Anything that can raise a toast -- in practice a Textual app or screen."""

    def notify(self, message: str, *, severity: Severity = ...) -> None: ...


class ThreadHopper(Protocol):
    """Anything that can schedule work on the UI thread -- a Textual app."""

    def call_from_thread(self, callback: Callable[[], None]) -> object: ...


def report(notifier: Notifier, outcome: Outcome) -> None:
    """Show every notice *outcome* produced, in the order it produced them."""
    for notice in outcome.notices:
        notifier.notify(notice.text, severity=notice.severity)


def on_ui_thread(app: ThreadHopper, action: Callable[[], None]) -> None:
    """Run *action* on the UI thread, forgiving an application already gone.

    Every sync callback arrives on a scheduler thread, so it has to hop.
    A ``RuntimeError`` means the user quit while a sync was in flight,
    which is ordinary and not worth reporting.
    """
    with contextlib.suppress(RuntimeError):
        app.call_from_thread(action)


__all__ = ["Notifier", "ThreadHopper", "on_ui_thread", "report"]
