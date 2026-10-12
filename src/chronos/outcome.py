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

import time
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

        A validation refusal is a *warning* and not an error, so a
        front end cannot decide on :attr:`failed` alone whether to keep
        what the user typed.

        It cannot decide on this either, though, and the browser front
        end no longer does: ``not acted`` is also true of a save that
        legitimately changed nothing, which is not a reason to hold a
        form open.  `pony.web.actions.holds_form` is the narrower
        question -- did this refuse what was typed? -- and the three
        browser forms all ask it.
        """
        return self.failed or not self.acted


# Progress is reported at most this often while work is flowing.  A
# per-item report costs a flushed terminal write in a CLI and a hop into
# the event loop in a TUI, and the loop doing the work pays for it
# between one item and the next -- so the slower the terminal, the
# slower the operation.  Ten a second is more than a person can read.
PROGRESS_INTERVAL_SECONDS = 0.1


# At most this many reports for one unit of work, however many items
# it holds.  The time cap alone is not enough: when each item takes
# longer than the interval -- a slow server, a cold index -- every item
# reports again, which is what "it still counts every message" means.
PROGRESS_REPORTS_PER_UNIT = 200


def progress_stride(total: int) -> int:
    """Report at most :data:`PROGRESS_REPORTS_PER_UNIT` times over *total*."""
    return max(1, total // PROGRESS_REPORTS_PER_UNIT)


class ProgressGate:
    """Decides when a per-item loop may report progress.

    Two caps, because either alone leaves a hole: no more than ten
    reports a second (a fast loop would otherwise pay for a terminal
    write per item), and no more than one per *stride* items (a slow
    loop passes the time cap on every single item).  The last item
    always reports, so a finished unit of work never leaves a stale
    count on screen.

    Asking costs a comparison and one ``monotonic()`` call, so the
    message itself is only built when it will be shown.

    It lives beside :class:`Notice` because it answers the same kind of
    question -- how an operation talks to whoever asked for it -- and
    because both halves of the program need it.  A front end that
    throttles for itself (``if done % 20``) fixes it only for itself.
    """

    __slots__ = ("_interval", "_last", "_last_item", "_stride")

    def __init__(
        self,
        interval: float = PROGRESS_INTERVAL_SECONDS,
        stride: int = 1,
    ) -> None:
        self._interval = interval
        self._stride = max(1, stride)
        self._last = 0.0
        self._last_item = 0

    def due(self, done: int = 0, *, final: bool = False) -> bool:
        if final:
            self._last = time.monotonic()
            self._last_item = done
            return True
        if done - self._last_item < self._stride:
            return False
        now = time.monotonic()
        if now - self._last < self._interval:
            return False
        self._last = now
        self._last_item = done
        return True


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
    "PROGRESS_INTERVAL_SECONDS",
    "PROGRESS_REPORTS_PER_UNIT",
    "Notice",
    "NoticeLog",
    "Outcome",
    "ProgressGate",
    "Severity",
    "failure",
    "progress_stride",
    "info",
    "warn",
]
