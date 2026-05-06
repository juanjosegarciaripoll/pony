"""OSC escape sequences for updating the terminal emulator's window title."""

from __future__ import annotations

import sys


def set_terminal_title(text: str) -> None:
    """Emit OSC 2 to set the terminal window title, no-op when not a TTY."""
    out = sys.__stdout__
    if out is None or not out.isatty():
        return
    out.write(f"\x1b]2;{text}\x07")
    out.flush()


def push_terminal_title() -> None:
    """Push the current terminal title onto the terminal's title stack."""
    out = sys.__stdout__
    if out is None or not out.isatty():
        return
    out.write("\x1b[22;2t")
    out.flush()


def pop_terminal_title() -> None:
    """Pop the terminal title stack, restoring the previous title."""
    out = sys.__stdout__
    if out is None or not out.isatty():
        return
    out.write("\x1b[23;2t")
    out.flush()
