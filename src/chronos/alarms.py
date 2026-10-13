"""Which reminders are due, and what each one says.

Two front ends announce them and they announce them differently: the
calendar on its own raises a toast, rings the terminal bell and emits an
OSC 777 notification, while Pony Express publishes through its shared
notification space, which also carries new mail.  What they must not
decide separately is *which* alarms are due, how far back to look, and
that an alarm announced once is never announced twice -- that answer
lives here, with the query and the mark-as-fired in one place so a
failure midway cannot replay what was already shown.

No Textual, and no notification vocabulary either: this hands back a
title and a body, and each front end says it its own way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from chronos.domain import AlarmRecord
from chronos.protocols import IndexRepository

logger = logging.getLogger(__name__)

#: How far back a poll looks.  Wide enough that a laptop asleep for a
#: few minutes, or a missed tick, still announces the reminder.
ALARM_LOOKBACK = timedelta(minutes=15)

#: How often either front end polls.
ALARM_POLL_SECONDS = 30.0

#: How long a reminder stays on screen: long enough to be read after
#: looking away, which an ordinary toast timeout is not.
ALARM_TOAST_SECONDS = 120.0

# Google fills every VALARM with this DESCRIPTION; repeating it in the
# notification body says nothing the title doesn't.
_BOILERPLATE_ALARM_TEXT = "This is an event reminder"


@dataclass(frozen=True, slots=True)
class DueAlarm:
    """One reminder to announce: what it is about, and what it says."""

    title: str
    body: str


def alarm_message(alarm: AlarmRecord, now: datetime) -> str:
    """Notification body: when the event starts, then any alarm text."""
    start = alarm.occurrence_start.astimezone()
    local_now = now.astimezone()
    if start.date() == local_now.date():
        when = start.strftime("%H:%M")
    else:
        when = start.strftime("%a %d %b %H:%M")
    verb = "Started" if alarm.occurrence_start <= now else "Starts"
    lines = [f"{verb} {when}"]
    description = (alarm.description or "").strip()
    if description and description != _BOILERPLATE_ALARM_TEXT:
        lines.append(description)
    return "\n".join(lines)


def due_alarms(
    index: IndexRepository,
    now: datetime,
    *,
    lookback: timedelta = ALARM_LOOKBACK,
) -> tuple[DueAlarm, ...]:
    """Every alarm that fell due since the last poll, marked as fired.

    Marking happens as each alarm is converted rather than in a second
    pass, so a failure midway cannot replay the ones already announced;
    the mark is in the calendar's own index, so a reminder survives a
    restart without being shown again.
    """
    due: list[DueAlarm] = []
    for alarm in index.query_pending_alarms(now - lookback, now):
        if alarm.db_id is None:
            continue
        announcement = DueAlarm(
            title=alarm.summary or "Reminder",
            body=alarm_message(alarm, now),
        )
        try:
            index.mark_alarm_fired(alarm.db_id, now)
        except Exception:  # noqa: BLE001
            logger.exception("mark_alarm_fired failed for alarm %s", alarm.db_id)
        due.append(announcement)
    return tuple(due)


__all__ = [
    "ALARM_LOOKBACK",
    "ALARM_POLL_SECONDS",
    "ALARM_TOAST_SECONDS",
    "DueAlarm",
    "alarm_message",
    "due_alarms",
]
