"""The clock-time field on the event form.

Both time fields were dropdowns over the forty-eight half-hour slots a
day view draws, so an event could only begin and end on that grid --
while the same form in the browser is an ``<input type="datetime-local">``
and takes any minute of the hour.  The grid is the right default,
because it is where the cursor lands; it is the wrong limit.

So this is a field you type into, with the half hours still one press
away: ``up`` and ``down`` move to the next and previous slot from
wherever the value is, which is both fewer keys than walking a
forty-eight-row list and reachable from a time that is not on it.
"""

from __future__ import annotations

import re
from datetime import time
from typing import Any

from textual.binding import Binding
from textual.widgets import Input

#: Minutes between two neighbouring slots on a day view's time grid.
HALF_HOUR = 30

_MINUTES_IN_A_DAY = 24 * 60


class InvalidTimeError(ValueError):
    """What a field holds is not a time of day."""


#: Accepted spellings, tried in order: ``09:30`` and ``9.30`` as the
#: field writes it, ``0930`` and ``930`` as a hand types it, and a bare
#: ``9`` for the hour.  Minutes are two digits or nothing, so a
#: half-typed ``9:3`` is a mistake rather than three minutes past nine.
_SPELLINGS = (
    re.compile(r"(?P<hour>\d{1,2})[:.](?P<minute>\d{2})\Z"),
    re.compile(r"(?P<hour>\d{1,2})(?P<minute>\d{2})\Z"),
    re.compile(r"(?P<hour>\d{1,2})\Z"),
)


def parse_time_input(value: str, label: str = "time") -> time:
    """Parse a typed clock time, naming the field in any complaint.

    Raises `InvalidTimeError` -- a `ValueError`, which the form's save
    already reports -- for an empty field, an unrecognised spelling, and
    a well-formed one that is no time of day, such as ``25:00``.
    """
    text = value.strip()
    if not text:
        raise InvalidTimeError(f"{label} is required")
    for spelling in _SPELLINGS:
        found = spelling.match(text)
        if found is None:
            continue
        minute = found.groupdict().get("minute")
        try:
            return time(int(found["hour"]), int(minute) if minute else 0)
        except ValueError as exc:
            raise InvalidTimeError(f"{label} {text!r} is not a time of day") from exc
    raise InvalidTimeError(f"{label} must be HH:MM")


def step_time(value: str, minutes: int) -> str:
    """*value* moved one *minutes*-sized step, as ``HH:MM``.

    The step lands **on** the grid rather than adding to what is there,
    so a typed 14:17 is one press from 14:30 and not from 14:47, and it
    wraps, because a clock does: up from 23:30 is 00:00.  An empty field
    therefore enters the grid at the end the press comes from, which is
    what opening the old dropdown on nothing and pressing a key did.
    A field holding something that is not a time is treated as empty --
    it cannot be stepped, and it could not have been saved either.
    """
    step = abs(minutes)
    try:
        moment = parse_time_input(value)
    except InvalidTimeError:
        return _as_clock(0 if minutes > 0 else -step)
    total = moment.hour * 60 + moment.minute
    if minutes > 0:
        return _as_clock((total // step + 1) * step)
    # Ceiling division, so a value already between two slots steps down
    # to the one below it rather than two below.
    return _as_clock(-(-total // step) * step - step)


def _as_clock(total_minutes: int) -> str:
    total_minutes %= _MINUTES_IN_A_DAY
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


class TimePicker(Input):
    """Input field accepting a time of day, stepping by half hours.

    The widget itself is a thin Input. Parsing lives in
    `parse_time_input` and the stepping in `step_time`, callable from
    anywhere.
    """

    BINDINGS = [
        # An Input binds neither key and the event form wants neither,
        # so the field may have them. They move a cursor *inside* a
        # field, like ctrl+left is a word inside an address field,
        # rather than running an operation -- so they are not keymap
        # entries, and the browser has nothing to keep in step: its own
        # time field steps natively.
        Binding("up", "later", "Later", show=False),
        Binding("down", "earlier", "Earlier", show=False),
    ]

    def __init__(
        self,
        value: str = "",
        placeholder: str = "HH:MM",
        **kwargs: Any,
    ) -> None:
        super().__init__(value=value, placeholder=placeholder, **kwargs)

    def action_later(self) -> None:
        self._step(HALF_HOUR)

    def action_earlier(self) -> None:
        self._step(-HALF_HOUR)

    def parsed(self, label: str = "time") -> time:
        return parse_time_input(self.value, label)

    def _step(self, minutes: int) -> None:
        self.value = step_time(self.value, minutes)
        # Typing continues after the value rather than inside it.
        self.cursor_position = len(self.value)


__all__ = [
    "HALF_HOUR",
    "InvalidTimeError",
    "TimePicker",
    "parse_time_input",
    "step_time",
]
