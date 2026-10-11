"""What an operation tells the user, with no user interface attached.

The model layer — mirrors, indexes, sync, contacts — decides *what* went
wrong and how much got done.  Deciding how to say it is the front end's
job: a terminal raises a toast, a browser renders a banner, a script
prints a line.  Operations therefore return :class:`Notice` values
rather than calling into a UI, which is what lets one implementation
serve both front ends and lets a test assert on an outcome instead of
driving a screen.

This module lives in ``chronos`` for the same reason ``scheduler.py``
does: the dependency arrow points from ``pony`` to ``chronos``, so
anything both halves share has to sit on this side of it.  Nothing here
knows about calendars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Textual's own severity levels, spelled out so this module stays free
# of Textual; ``App.notify`` takes exactly these three.
Severity = Literal["information", "warning", "error"]


@dataclass(frozen=True, slots=True)
class Notice:
    """One thing to tell the user about an operation."""

    text: str
    severity: Severity = "information"


def info(text: str) -> Notice:
    return Notice(text)


def warn(text: str) -> Notice:
    return Notice(text, "warning")


def failure(text: str) -> Notice:
    return Notice(text, "error")


@dataclass(frozen=True, slots=True)
class Outcome:
    """What an operation changed, and what to say about it.

    ``changed`` counts the records the operation actually altered, so a
    caller can tell "nothing to do" from "did three things" without
    reading prose.  ``notices`` holds every message the operation
    produced, in the order it produced them.
    """

    changed: int = 0
    notices: tuple[Notice, ...] = ()

    @staticmethod
    def refused(notice: Notice) -> Outcome:
        """An operation that declined to act, and why."""
        return Outcome(changed=0, notices=(notice,))

    @property
    def acted(self) -> bool:
        return self.changed > 0

    @property
    def failed(self) -> bool:
        """True when something went wrong, whatever else got done."""
        return any(notice.severity == "error" for notice in self.notices)

    @property
    def blocked(self) -> bool:
        """True when the operation did not go through: refused or failed.

        A front end holding a form open tests this rather than
        :attr:`failed`, because a validation refusal is a *warning* and
        not an error -- throwing away what the user typed over a
        warning loses their work.
        """
        return self.failed or not self.acted


@dataclass
class NoticeLog:
    """Collects notices while an operation runs.

    A loop over several messages reports per-message failures without
    giving up on the rest, which means threading a growing list through
    the work.  This is that list, so no operation has to grow its own.
    """

    notices: list[Notice] = field(default_factory=list)

    def info(self, text: str) -> None:
        self.notices.append(info(text))

    def warn(self, text: str) -> None:
        self.notices.append(warn(text))

    def failure(self, text: str) -> None:
        self.notices.append(failure(text))

    def outcome(self, changed: int) -> Outcome:
        """Everything collected so far, against a count of what changed."""
        return Outcome(changed=changed, notices=tuple(self.notices))


__all__ = [
    "Notice",
    "NoticeLog",
    "Outcome",
    "Severity",
    "failure",
    "info",
    "warn",
]
