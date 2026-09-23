"""Terminal helpers: OSC title control and opening files in an external viewer."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

from textual.app import App, SuspendNotSupported

from ..domain import ViewerRule

MAIL_TITLE_PREFIX = "✉ "


def format_terminal_title(text: str, *, has_inbox_mail: bool = False) -> str:
    """Return the terminal title, prefixed when inbox mail is available."""
    if has_inbox_mail:
        return f"{MAIL_TITLE_PREFIX}{text}"
    return text


def resolve_viewer_command(
    viewers: Sequence[ViewerRule], content_type: str | None
) -> tuple[str, ...] | None:
    """Return the configured argv for *content_type*, or ``None``.

    Matching is exact on the lower-cased type, so a config entry for
    ``text/calendar`` does not claim ``text/plain``.  ``None`` means the
    caller should fall back to the OS default handler.
    """
    if not content_type:
        return None
    wanted = content_type.strip().lower()
    for rule in viewers:
        if rule.content_type == wanted:
            return rule.command
    return None


def launch_file(path: Path, command: Sequence[str] | None = None) -> None:
    """Open *path* with *command*, or with the OS default application.

    *command* is an argv prefix from the ``[viewers]`` config table; the
    path is appended as its final argument, the way ``xdg-open`` and
    ``EDITOR`` conventions expect.
    """
    if command:
        subprocess.run([*command, str(path)], check=False)  # noqa: S603
        return
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
