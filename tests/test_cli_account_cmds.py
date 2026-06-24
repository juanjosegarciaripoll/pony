"""Tests for ``pony account`` and ``pony config`` CLI subcommands.

Covers the interactive / network-touching handlers in ``pony.cli`` that are
otherwise skipped by the non-interactive command surface: ``account test``,
the ``account add`` wizard, ``account set-password``, ``config edit`` and
``config show``.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from collections.abc import Iterator, Sequence
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from test_cli import (
    isolated_app_env,
    run_cli_capture,
    temporary_config,
)

from pony.cli import (
    main,
    run_account_add_interactive,
    run_account_set_password,
)
from pony.imap_client import ImapAuthError
from pony.paths import AppPaths


class _FakeSession:
    """Stand-in for ``ImapSession`` used by ``run_account_test``."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs

    def list_folders(self) -> list[str]:
        return ["INBOX", "Sent", "Archive"]

    def logout(self) -> None:
        return None


def _run_capturing_stderr(argv: Sequence[str]) -> tuple[str, str, int]:
    """Run ``main`` capturing stdout and stderr plus the exit code."""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return out.getvalue(), err.getvalue(), rc


def _encrypted_config_toml() -> str:
    """Config whose single account uses the ``encrypted`` credentials source."""
    return """
config_version = 2

[[accounts]]
name = "personal"
email_address = "user@example.com"
imap_host = "imap.example.com"
username = "user"
credentials_source = "encrypted"

[accounts.smtp]
host = "smtp.example.com"

[accounts.mirror]
path = "mirrors/personal"
format = "maildir"
trash_retention_days = 30
""".strip()


def _prompt_inputs(values: list[str]) -> Iterator[str]:
    """Yield successive interactive ``input`` responses."""
    yield from values


class AccountTestCommandTestCase(unittest.TestCase):
    """Exercise ``pony account test``."""

    def test_success_lists_folders(self) -> None:
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch("pony.cli.ImapSession", _FakeSession),
        ):
            out, err, rc = _run_capturing_stderr(
                ["--config", str(config_path), "account", "test", "personal"]
            )
        self.assertEqual(rc, 0)
        self.assertIn("Testing personal", out)
        self.assertIn("Password: OK", out)
        self.assertIn("Login: OK", out)
        self.assertIn("Folders: 3 found", out)
        self.assertIn("INBOX", out)
        self.assertEqual(err, "")
        self.assertEqual(_FakeSession.last_kwargs["host"], "imap.example.com")
        self.assertEqual(_FakeSession.last_kwargs["username"], "user")

    def test_unknown_account_returns_error(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            out, err, rc = _run_capturing_stderr(
                ["--config", str(config_path), "account", "test", "nope"]
            )
        self.assertEqual(rc, 1)
        self.assertIn("not found in config", err)

    def test_auth_failure_returns_error(self) -> None:
        def _raise(**_kwargs: Any) -> _FakeSession:
            raise ImapAuthError("user", "imap.example.com")

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch("pony.cli.ImapSession", _raise),
        ):
            out, err, rc = _run_capturing_stderr(
                ["--config", str(config_path), "account", "test", "personal"]
            )
        self.assertEqual(rc, 1)
        self.assertIn("Login: FAILED", err)
        self.assertIn("set-password", err)

    def test_connection_failure_returns_error(self) -> None:
        def _raise(**_kwargs: Any) -> _FakeSession:
            raise OSError("network down")

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch("pony.cli.ImapSession", _raise),
        ):
            out, err, rc = _run_capturing_stderr(
                ["--config", str(config_path), "account", "test", "personal"]
            )
        self.assertEqual(rc, 1)
        self.assertIn("Connection: FAILED", err)
        self.assertIn("network down", err)

    def test_password_retrieval_failure_returns_error(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            # An account whose credentials_source is 'encrypted' but with no
            # stored credential makes get_password raise ConfigError.
            config_path.write_text(_encrypted_config_toml(), encoding="utf-8")
            out, err, rc = _run_capturing_stderr(
                ["--config", str(config_path), "account", "test", "personal"]
            )
        self.assertEqual(rc, 1)
        self.assertIn("Password: FAILED", err)


class AccountSetPasswordTestCase(unittest.TestCase):
    """Exercise ``pony account set-password``."""

    def test_stores_encrypted_password(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            config_path.write_text(_encrypted_config_toml(), encoding="utf-8")
            with mock.patch("getpass.getpass", return_value="hunter2"):
                out, err, rc = _run_capturing_stderr(
                    [
                        "--config",
                        str(config_path),
                        "account",
                        "set-password",
                        "personal",
                    ]
                )
        self.assertEqual(rc, 0)
        self.assertIn("Password stored", out)

    def test_unknown_account_returns_error(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            config_path.write_text(_encrypted_config_toml(), encoding="utf-8")
            with mock.patch("getpass.getpass", return_value="hunter2"):
                out, err, rc = _run_capturing_stderr(
                    [
                        "--config",
                        str(config_path),
                        "account",
                        "set-password",
                        "missing",
                    ]
                )
        self.assertEqual(rc, 1)
        self.assertIn("not found in config", err)

    def test_non_encrypted_source_rejected(self) -> None:
        # The sample config uses plaintext credentials.
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch("getpass.getpass", return_value="hunter2"),
        ):
            out, err, rc = _run_capturing_stderr(
                [
                    "--config",
                    str(config_path),
                    "account",
                    "set-password",
                    "personal",
                ]
            )
        self.assertEqual(rc, 1)
        self.assertIn("not 'encrypted'", err)

    def test_empty_password_rejected(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            config_path.write_text(_encrypted_config_toml(), encoding="utf-8")
            with mock.patch("getpass.getpass", return_value=""):
                paths = AppPaths.default()
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = run_account_set_password(
                        paths=paths,
                        config_path=config_path,
                        account_name="personal",
                    )
        self.assertEqual(rc, 1)
        self.assertIn("must not be empty", err.getvalue())


class ConfigEditTestCase(unittest.TestCase):
    """Exercise ``pony config edit``."""

    def test_creates_from_sample_and_opens_editor(self) -> None:
        with isolated_app_env():
            paths = AppPaths.default()
            self.assertFalse(paths.config_file.exists())
            fake_run = mock.Mock(return_value=mock.Mock(returncode=0))
            with (
                mock.patch.dict("os.environ", {"EDITOR": "true"}, clear=False),
                mock.patch("pony.cli.subprocess.run", fake_run),
            ):
                out, err, rc = _run_capturing_stderr(["config", "edit"])
        self.assertEqual(rc, 0)
        self.assertTrue(paths.config_file.exists())
        self.assertIn("Opening", out)
        fake_run.assert_called_once()
        called_argv = fake_run.call_args.args[0]
        self.assertEqual(called_argv[0], "true")

    def test_existing_config_not_overwritten(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            original = config_path.read_text(encoding="utf-8")
            fake_run = mock.Mock(return_value=mock.Mock(returncode=0))
            with (
                mock.patch.dict("os.environ", {"EDITOR": "true"}, clear=False),
                mock.patch("pony.cli.subprocess.run", fake_run),
            ):
                out, err, rc = _run_capturing_stderr(
                    ["--config", str(config_path), "config", "edit"]
                )
            after = config_path.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertNotIn("Created", out)
        self.assertEqual(after, original)

    def test_bootstrap_without_sample_writes_stub(self) -> None:
        # Force the sample-config lookup to report "missing" so the stub
        # bootstrap branch runs.
        real_exists = Path.exists

        def fake_exists(self: Path) -> bool:
            if self.name == "config-sample.toml":
                return False
            return real_exists(self)

        with isolated_app_env():
            paths = AppPaths.default()
            fake_run = mock.Mock(return_value=mock.Mock(returncode=0))
            with (
                mock.patch.dict("os.environ", {"EDITOR": "true"}, clear=False),
                mock.patch("pony.cli.subprocess.run", fake_run),
                mock.patch.object(Path, "exists", fake_exists),
            ):
                out, err, rc = _run_capturing_stderr(["config", "edit"])
            text = paths.config_file.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertIn("Run `pony account add`", text)
        self.assertIn("Created", out)

    def test_default_editor_used_when_env_unset(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            fake_run = mock.Mock(return_value=mock.Mock(returncode=0))
            with (
                mock.patch.dict(
                    "os.environ",
                    {"EDITOR": "", "VISUAL": ""},
                    clear=False,
                ),
                mock.patch("pony.cli.subprocess.run", fake_run),
            ):
                out, err, rc = _run_capturing_stderr(
                    ["--config", str(config_path), "config", "edit"]
                )
        self.assertEqual(rc, 0)
        fake_run.assert_called_once()
        # vi (or notepad on Windows) is the platform default.
        called_argv = fake_run.call_args.args[0]
        self.assertIn(called_argv[0], ("vi", "notepad"))


class ConfigShowTestCase(unittest.TestCase):
    """Exercise ``pony config show``."""

    def test_prints_config_contents(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            out, rc = run_cli_capture("--config", str(config_path), "config", "show")
        self.assertEqual(rc, 0)
        self.assertIn("config_version = 2", out)

    def test_missing_config_returns_error(self) -> None:
        with isolated_app_env():
            missing = AppPaths.default().config_file.parent / "does-not-exist.toml"
            out, err, rc = _run_capturing_stderr(
                ["--config", str(missing), "config", "show"]
            )
        self.assertEqual(rc, 1)
        self.assertIn("No config file", err)


class AccountAddWizardTestCase(unittest.TestCase):
    """Exercise the interactive ``run_account_add_interactive`` wizard."""

    def test_plaintext_creates_new_config(self) -> None:
        inputs = _prompt_inputs(
            [
                "Work",  # account name
                "me@corp.example",  # email
                "",  # imap host -> default guess
                "",  # imap ssl -> default yes
                "",  # imap port -> default 993
                "",  # smtp host -> default guess
                "",  # smtp ssl -> default yes
                "",  # smtp port -> default 465
                "",  # username -> default email
                "1",  # credentials choice -> plaintext
                "",  # mirror format -> default maildir
            ]
        )
        with isolated_app_env():
            paths = AppPaths.default()
            config_file = paths.config_file
            out = io.StringIO()

            with (
                mock.patch("builtins.input", lambda *_a, **_k: next(inputs)),
                mock.patch("getpass.getpass", return_value="s3cret"),
                contextlib.redirect_stdout(out),
            ):
                rc = run_account_add_interactive(paths=paths, config_path=None)
            text = config_file.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertIn('name             = "Work"', text)
        self.assertIn('credentials_source = "plaintext"', text)
        self.assertIn('password         = "s3cret"', text)
        self.assertIn("Config validated successfully.", out.getvalue())

    def test_encrypted_appends_and_stores_password(self) -> None:
        inputs = _prompt_inputs(
            [
                "Cloud",  # account name (config exists, no dupe)
                "me@cloud.example",  # email
                "imap.cloud.example",  # imap host
                "yes",  # imap ssl
                "993",  # imap port
                "smtp.cloud.example",  # smtp host
                "yes",  # smtp ssl
                "465",  # smtp port
                "me@cloud.example",  # username
                "2",  # credentials choice -> encrypted
                "maildir",  # mirror format
            ]
        )
        with isolated_app_env(), temporary_config() as config_path:
            paths = AppPaths.default()
            out = io.StringIO()

            with (
                mock.patch("builtins.input", lambda *_a, **_k: next(inputs)),
                mock.patch("getpass.getpass", return_value="vault-pass"),
                contextlib.redirect_stdout(out),
            ):
                rc = run_account_add_interactive(paths=paths, config_path=config_path)
            text = config_path.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertIn('name             = "Cloud"', text)
        self.assertIn('credentials_source = "encrypted"', text)
        self.assertIn("Password encrypted and stored.", out.getvalue())
        self.assertIn("Appended to", out.getvalue())

    def test_encrypted_empty_password_skips_store(self) -> None:
        inputs = _prompt_inputs(
            [
                "Skip",
                "me@skip.example",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "2",  # encrypted
                "",
            ]
        )
        with isolated_app_env(), temporary_config() as config_path:
            paths = AppPaths.default()
            out = io.StringIO()

            with (
                mock.patch("builtins.input", lambda *_a, **_k: next(inputs)),
                mock.patch("getpass.getpass", return_value=""),
                contextlib.redirect_stdout(out),
            ):
                rc = run_account_add_interactive(paths=paths, config_path=config_path)
        self.assertEqual(rc, 0)
        self.assertIn("No password entered", out.getvalue())

    def test_duplicate_name_declined_cancels(self) -> None:
        inputs = _prompt_inputs(
            [
                "personal",  # name -> matches sample config account
                "n",  # decline replace
            ]
        )
        with isolated_app_env(), temporary_config() as config_path:
            paths = AppPaths.default()
            original = config_path.read_text(encoding="utf-8")
            out = io.StringIO()

            with (
                mock.patch("builtins.input", lambda *_a, **_k: next(inputs)),
                mock.patch("getpass.getpass", return_value="x"),
                contextlib.redirect_stdout(out),
            ):
                rc = run_account_add_interactive(paths=paths, config_path=config_path)
            text = config_path.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertIn("Cancelled.", out.getvalue())
        self.assertEqual(text, original)

    def test_duplicate_name_replaced(self) -> None:
        inputs = _prompt_inputs(
            [
                "personal",  # name -> matches sample config account
                "y",  # accept replace
                "new@example.com",  # email
                "imap.new.example",  # imap host
                "no",  # imap ssl -> non-ssl branch (port 143)
                "143",  # imap port
                "smtp.new.example",  # smtp host
                "no",  # smtp ssl -> non-ssl branch (port 587)
                "587",  # smtp port
                "new@example.com",  # username
                "1",  # plaintext
                "mbox",  # mirror format
            ]
        )
        with isolated_app_env(), temporary_config() as config_path:
            paths = AppPaths.default()
            out = io.StringIO()

            with (
                mock.patch("builtins.input", lambda *_a, **_k: next(inputs)),
                mock.patch("getpass.getpass", return_value="repl"),
                contextlib.redirect_stdout(out),
            ):
                rc = run_account_add_interactive(paths=paths, config_path=config_path)
            text = config_path.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertIn('email_address    = "new@example.com"', text)
        self.assertIn("imap_ssl         = false", text)
        self.assertIn('format = "mbox"', text)
        # Only one [[accounts]] block should remain for "personal".
        self.assertEqual(text.count('name             = "personal"'), 1)


if __name__ == "__main__":
    unittest.main()
