"""One notification space for mail and calendar.

Pony Express shows one subsystem at a time — the mail reader or the
agenda — so anything that happens in the other half has no screen of its
own to appear on.  Both halves announce through a single
:class:`NotificationCenter`, and the application turns each announcement
into a toast, a terminal bell and a desktop notification once, wherever
the user happens to be looking.

Two kinds of announcement, distinguished by ``urgent``:

* A reminder must reach the user wherever they are, including while the
  calendar itself is on screen.  Those are ``urgent``.
* News about the other half — mail that just arrived while the agenda is
  open — is worth a toast only when that half is *not* on screen, since
  the subsystem it came from reports it locally as well.  The
  application applies that rule; this module only records it.

Nothing here is persisted.  Mail and calendar each already keep the
state that outlives a session (unread flags, a fired alarm's
``fired_at``), so a second store would be a second truth.

This module is deliberately free of Textual: the rendering lives in
``pony.tui.app``, which is also what decides which subsystem is visible.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

# Textual's own severity levels, spelled out here so this module stays
# free of Textual; `App.notify` takes exactly these three.
Severity = Literal["information", "warning", "error"]

# Keeps the history bounded on a long-running session.  Large enough
# that a day of reminders and syncs stays readable.
DEFAULT_HISTORY_LIMIT = 200


class NotificationSource(StrEnum):
    """Which half of the program an announcement came from."""

    MAIL = "mail"
    CALENDAR = "calendar"


@dataclass(frozen=True, slots=True)
class Notification:
    """One announcement, ready to render."""

    source: NotificationSource
    title: str
    body: str
    at: datetime
    # True when the user must see this wherever they are: a calendar
    # reminder falling due.  False for news the originating subsystem
    # also reports on its own screen.
    urgent: bool = False
    # Passed straight through to ``App.notify``.
    severity: Severity = "information"

    @property
    def summary(self) -> str:
        """Title and body on one line, for a status bar or a log."""
        body = self.body.replace("\n", " · ").strip()
        return f"{self.title} — {body}" if body else self.title


Subscriber = Callable[[Notification], None]


@dataclass(slots=True)
class NotificationCenter:
    """Records announcements and hands them to its subscribers.

    Subscribers are called in registration order, synchronously, on the
    thread that published.  A subscriber that raises would otherwise
    lose the announcement for every later subscriber, so failures are
    swallowed: a broken renderer must not cost the user a reminder.
    """

    history_limit: int = DEFAULT_HISTORY_LIMIT
    _history: deque[Notification] = field(init=False, repr=False)
    _subscribers: list[Subscriber] = field(init=False, default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._history = deque(maxlen=self.history_limit)

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def publish(self, notification: Notification) -> None:
        self._history.append(notification)
        for subscriber in self._subscribers:
            try:
                subscriber(notification)
            except Exception:  # noqa: BLE001 — never lose a reminder
                continue

    def announce(
        self,
        source: NotificationSource,
        title: str,
        body: str = "",
        *,
        at: datetime,
        urgent: bool = False,
        severity: Severity = "information",
    ) -> Notification:
        """Build and publish a notification.  Returns what was published."""
        notification = Notification(
            source=source,
            title=title,
            body=body,
            at=at,
            urgent=urgent,
            severity=severity,
        )
        self.publish(notification)
        return notification

    @property
    def history(self) -> tuple[Notification, ...]:
        """Every announcement still held, oldest first."""
        return tuple(self._history)

    def latest(
        self, *, source: NotificationSource | None = None
    ) -> Notification | None:
        """The most recent announcement, optionally from one subsystem."""
        for notification in reversed(self._history):
            if source is None or notification.source is source:
                return notification
        return None

    def count(self, *, source: NotificationSource | None = None) -> int:
        return len(tuple(self._matching(source)))

    def clear(self) -> None:
        self._history.clear()

    def _matching(self, source: NotificationSource | None) -> Iterable[Notification]:
        for notification in self._history:
            if source is None or notification.source is source:
                yield notification


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "Severity",
    "Notification",
    "NotificationCenter",
    "NotificationSource",
    "Subscriber",
]
