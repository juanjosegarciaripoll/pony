from __future__ import annotations

import sys


def set_terminal_title(text: str) -> None:
    out = sys.__stdout__
    if out is None or not out.isatty():
        return
    out.write(f"\x1b]2;{text}\x07")
    out.flush()


def push_terminal_title() -> None:
    out = sys.__stdout__
    if out is None or not out.isatty():
        return
    out.write("\x1b[22;2t")
    out.flush()


def pop_terminal_title() -> None:
    out = sys.__stdout__
    if out is None or not out.isatty():
        return
    out.write("\x1b[23;2t")
    out.flush()


def osc777_notification(title: str, body: str) -> str:
    """OSC 777 `notify` sequence asking the terminal for a desktop notification.

    Control characters would end the sequence early (and `;` in the
    title would shift the body), so both are flattened: newlines become
    " · " and the rest are dropped.
    """

    def clean(text: str) -> str:
        text = text.replace("\n", " · ")
        return "".join(ch for ch in text if ch >= " " and ch != "\x7f")

    return f"\x1b]777;notify;{clean(title).replace(';', ',')};{clean(body)}\x07"


__all__ = [
    "osc777_notification",
    "pop_terminal_title",
    "push_terminal_title",
    "set_terminal_title",
]
