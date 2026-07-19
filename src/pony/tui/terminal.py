"""Terminal helpers: OSC title control and opening files in the OS viewer."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

from textual.app import App, SuspendNotSupported

MAIL_TITLE_PREFIX = "✉ "


def format_terminal_title(text: str, *, has_inbox_mail: bool = False) -> str:
    """Return the terminal title, prefixed when inbox mail is available."""
    if has_inbox_mail:
        return f"{MAIL_TITLE_PREFIX}{text}"
    return text


def launch_file(path: Path) -> None:
    """Open *path* with the OS default application."""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":  # pyright: ignore[reportUnreachable]
        subprocess.run(["open", str(path)], check=False)  # noqa: S603 S607
    else:  # pyright: ignore[reportUnreachable]
        subprocess.run(["xdg-open", str(path)], check=False)  # noqa: S603 S607


@contextmanager
def suspend_for_external_program(app: App[object]) -> Iterator[None]:
    """Give an external program control of the terminal when supported."""
    with ExitStack() as stack:
        with suppress(SuspendNotSupported):
            stack.enter_context(app.suspend())
        yield


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
