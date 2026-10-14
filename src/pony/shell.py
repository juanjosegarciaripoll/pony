"""Command lines: splitting one, picking an editor, running one for a secret.

Both halves of the program let a configuration name a command instead of
a secret -- the mail side as ``password_command``, the calendar as a
``[credential] command`` -- so that a password can live in `pass`, `gpg`
or the system keychain rather than in `config.toml`.  Both also open a
configuration in an editor, and both reach for `$VISUAL` and `$EDITOR`
to find one.

None of it is either half's, and each piece was written twice before it
landed here.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence


def split_command(value: str, *, platform: str | None = None) -> list[str]:
    """Split *value* into argv, using the platform's quoting rules.

    ``shlex`` defaults to POSIX rules, where a backslash is an escape:
    ``shlex.split(r"C:\\Windows\\notepad.exe")`` is
    ``["C:Windowsnotepad.exe"]``, so every real Windows editor path
    failed to launch.

    Three callers want it: the editor a configuration is edited in
    (:func:`pick_editor`), the ``$BROWSER`` an OAuth flow decides whether
    it can open a window on, and a credential command written as one
    string.  They sit on three sides of the model/view line, which is why
    it is nobody's.
    """
    real_platform = platform if platform is not None else sys.platform
    return shlex.split(value, posix=not real_platform.startswith("win"))


def pick_editor(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Resolve a command list for an interactive editor.

    Priority follows POSIX convention plus a platform-default fallback:

    1. `$VISUAL` (full-screen / GUI editors).
    2. `$EDITOR` (line editors).
    3. Platform default — `notepad` on Windows, `nano` if installed,
       else `vi` (POSIX-required, present on every Unix).
    """
    real_env = env if env is not None else os.environ
    real_platform = platform if platform is not None else sys.platform
    real_which = which if which is not None else shutil.which
    for var in ("VISUAL", "EDITOR"):
        value = real_env.get(var)
        if value:
            return split_command(value, platform=real_platform)
    if real_platform.startswith("win"):
        return ["notepad"]
    if real_which("nano") is not None:
        return ["nano"]
    return ["vi"]


#: How long a command that is to print a secret may take. The usual ones
#: (`pass`, `gpg`, `security`) can block on a passphrase prompt that has
#: no terminal to appear on, and a sync that hangs forever reads as a
#: network problem. The mail half had no timeout at all.
COMMAND_TIMEOUT_SECONDS = 30


class CommandSecretError(RuntimeError):
    """A command that was to print a secret did not."""


def secret_from_command(
    command: Sequence[str],
    *,
    what: str,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Run *command* and return what it printed, as a secret.

    *command* is argv, passed to ``subprocess.run`` with no shell, so
    nothing in it is interpolated. *what* names the configuration key
    that put it there, because that is what the reader of the error has
    to go and fix.

    Only trailing newlines are removed. A secret may legally begin or
    end with a space, and one of the two callers used to ``strip()`` the
    lot -- silently handing the server a different password than the one
    the user stored.

    Whatever the command wrote to stderr goes in the message: "exited 2"
    on its own has sent people looking in the wrong place.
    """
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CommandSecretError(f"{what} not found: {command[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandSecretError(f"{what} timed out after {timeout:g}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise CommandSecretError(
            f"{what} exited {exc.returncode}" + (f": {detail}" if detail else "")
        ) from exc
    return completed.stdout.rstrip("\n")


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "pick_editor",
    "CommandSecretError",
    "secret_from_command",
    "split_command",
]
