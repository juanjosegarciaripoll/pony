"""Running a command that prints a secret.

Both halves of the program let a configuration name a command instead
of a password, and each had its own copy of running it. The copies had
drifted, and this covers what the differences cost: the mail side had no
timeout, said nothing about why the command failed, and stripped the
secret's own leading and trailing spaces.
"""

from __future__ import annotations

import sys
import unittest

from pony.shell import CommandSecretError, pick_editor, secret_from_command


def _prints(text: str) -> list[str]:
    """A command that writes *text* to stdout, on any platform."""
    return [sys.executable, "-c", f"import sys; sys.stdout.write({text!r})"]


class SecretFromCommandTest(unittest.TestCase):
    def test_the_secret_is_what_the_command_printed(self) -> None:
        self.assertEqual(secret_from_command(_prints("hunter2\n"), what="x"), "hunter2")

    def test_only_the_trailing_newlines_go(self) -> None:
        """A password may legally begin or end with a space.

        One of the two copies called `.strip()`, which handed the server
        a different password than the one the user stored.
        """
        self.assertEqual(
            secret_from_command(_prints("  pass phrase  \n"), what="x"),
            "  pass phrase  ",
        )

    def test_a_command_that_is_not_there_names_itself(self) -> None:
        with self.assertRaises(CommandSecretError) as caught:
            secret_from_command(["no-such-binary-xyz"], what="password_command")
        self.assertIn("password_command not found", str(caught.exception))
        self.assertIn("no-such-binary-xyz", str(caught.exception))

    def test_a_failure_carries_what_the_command_said(self) -> None:
        """`exited 2` on its own has sent people looking in the wrong place."""
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('no such entry\\n'); sys.exit(2)",
        ]
        with self.assertRaises(CommandSecretError) as caught:
            secret_from_command(command, what="password_command")
        self.assertIn("exited 2", str(caught.exception))
        self.assertIn("no such entry", str(caught.exception))

    def test_a_silent_failure_still_says_the_code(self) -> None:
        command = [sys.executable, "-c", "import sys; sys.exit(7)"]
        with self.assertRaises(CommandSecretError) as caught:
            secret_from_command(command, what="credential command")
        self.assertEqual(str(caught.exception), "credential command exited 7")

    def test_a_command_that_never_answers_gives_up(self) -> None:
        """`pass` and `gpg` can block on a prompt with no terminal for it.

        Without a timeout the sync waits for ever and reads as a
        network problem; the mail half had no timeout.
        """
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.assertRaises(CommandSecretError) as caught:
            secret_from_command(command, what="password_command", timeout=0.3)
        self.assertIn("timed out after 0.3s", str(caught.exception))


class PickEditorTest(unittest.TestCase):
    """`pick_editor` resolves an editor command without ever raising.

    Order of precedence (POSIX): VISUAL, then EDITOR, then a
    platform-appropriate default. Tests inject env / platform / which
    so they don't depend on the host OS or PATH.

    Both `config edit` commands ask this one function now. The mail
    half had its own, which preferred EDITOR over VISUAL and passed the
    variable as a single argv element -- so the two commands could open
    different editors on one machine, and `code --wait` was looked up as
    a file by that name.
    """

    def test_visual_takes_precedence_over_editor(self) -> None:
        cmd = pick_editor(
            env={"VISUAL": "vim", "EDITOR": "ed"},
            platform="linux",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["vim"])

    def test_editor_used_when_visual_unset(self) -> None:
        cmd = pick_editor(
            env={"EDITOR": "ed"},
            platform="linux",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["ed"])

    def test_visual_with_arguments_is_shlex_split(self) -> None:
        cmd = pick_editor(
            env={"VISUAL": "code --wait"},
            platform="darwin",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["code", "--wait"])

    def test_windows_default_is_notepad(self) -> None:
        cmd = pick_editor(
            env={},
            platform="win32",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["notepad"])

    def test_posix_prefers_nano_when_available(self) -> None:
        cmd = pick_editor(
            env={},
            platform="linux",
            which=lambda name: "/usr/bin/nano" if name == "nano" else None,
        )
        self.assertEqual(cmd, ["nano"])

    def test_posix_falls_back_to_vi_without_nano(self) -> None:
        cmd = pick_editor(
            env={},
            platform="linux",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["vi"])

    def test_macos_uses_same_posix_fallback(self) -> None:
        cmd = pick_editor(
            env={},
            platform="darwin",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["vi"])

    def test_uses_real_environment_when_unspecified(self) -> None:
        # Smoke test the no-injection path: just verify it returns a
        # non-empty list without raising. The exact contents depend on
        # the test runner's environment.
        cmd = pick_editor()
        self.assertTrue(cmd)
        self.assertIsInstance(cmd[0], str)
