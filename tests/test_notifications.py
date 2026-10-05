"""The notification space mail and calendar share."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from pony.notifications import (
    Notification,
    NotificationCenter,
    NotificationSource,
)

_NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


def _note(
    source: NotificationSource = NotificationSource.MAIL,
    title: str = "New mail",
    body: str = "",
    *,
    urgent: bool = False,
) -> Notification:
    return Notification(source=source, title=title, body=body, at=_NOW, urgent=urgent)


class NotificationTest(unittest.TestCase):
    def test_summary_joins_title_and_body(self) -> None:
        self.assertEqual(
            "Standup — Starts 09:45",
            _note(title="Standup", body="Starts 09:45").summary,
        )

    def test_summary_is_the_title_alone_without_a_body(self) -> None:
        self.assertEqual("Standup", _note(title="Standup").summary)

    def test_summary_flattens_a_multiline_body(self) -> None:
        self.assertEqual(
            "Standup — Starts 09:45 · Room 3",
            _note(title="Standup", body="Starts 09:45\nRoom 3").summary,
        )


class NotificationCenterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.center = NotificationCenter()
        self.seen: list[Notification] = []
        self.center.subscribe(self.seen.append)

    def test_published_notifications_reach_subscribers(self) -> None:
        note = _note()
        self.center.publish(note)
        self.assertEqual([note], self.seen)

    def test_announce_builds_and_publishes(self) -> None:
        note = self.center.announce(
            NotificationSource.CALENDAR,
            "Standup",
            "Starts 09:45",
            at=_NOW,
            urgent=True,
        )
        self.assertEqual([note], self.seen)
        self.assertTrue(note.urgent)
        self.assertIs(NotificationSource.CALENDAR, note.source)

    def test_every_subscriber_is_called_in_order(self) -> None:
        order: list[str] = []
        self.center.subscribe(lambda _n: order.append("second"))
        self.center.subscribe(lambda _n: order.append("third"))
        self.center.publish(_note())
        self.assertEqual(["second", "third"], order)

    def test_a_failing_subscriber_does_not_stop_the_others(self) -> None:
        def _explode(_note: Notification) -> None:
            raise RuntimeError("renderer is broken")

        later: list[Notification] = []
        self.center.subscribe(_explode)
        self.center.subscribe(later.append)
        note = _note()
        self.center.publish(note)
        self.assertEqual([note], later)
        self.assertEqual((note,), self.center.history)

    def test_history_keeps_publication_order(self) -> None:
        first, second = _note(title="one"), _note(title="two")
        self.center.publish(first)
        self.center.publish(second)
        self.assertEqual((first, second), self.center.history)

    def test_history_is_bounded(self) -> None:
        center = NotificationCenter(history_limit=2)
        for index in range(5):
            center.publish(_note(title=f"note {index}"))
        self.assertEqual(["note 3", "note 4"], [n.title for n in center.history])

    def test_latest_returns_the_most_recent(self) -> None:
        self.center.publish(_note(title="old"))
        self.center.publish(_note(title="new"))
        latest = self.center.latest()
        assert latest is not None
        self.assertEqual("new", latest.title)

    def test_latest_can_be_filtered_by_source(self) -> None:
        self.center.publish(_note(NotificationSource.CALENDAR, title="reminder"))
        self.center.publish(_note(NotificationSource.MAIL, title="mail"))
        calendar = self.center.latest(source=NotificationSource.CALENDAR)
        assert calendar is not None
        self.assertEqual("reminder", calendar.title)

    def test_latest_is_none_when_nothing_matches(self) -> None:
        self.center.publish(_note(NotificationSource.MAIL))
        self.assertIsNone(self.center.latest(source=NotificationSource.CALENDAR))
        self.assertIsNone(NotificationCenter().latest())

    def test_count_totals_and_filters(self) -> None:
        self.center.publish(_note(NotificationSource.MAIL))
        self.center.publish(_note(NotificationSource.CALENDAR))
        self.center.publish(_note(NotificationSource.CALENDAR))
        self.assertEqual(3, self.center.count())
        self.assertEqual(2, self.center.count(source=NotificationSource.CALENDAR))
        self.assertEqual(1, self.center.count(source=NotificationSource.MAIL))

    def test_clear_empties_the_history(self) -> None:
        self.center.publish(_note())
        self.center.clear()
        self.assertEqual((), self.center.history)


if __name__ == "__main__":
    unittest.main()
