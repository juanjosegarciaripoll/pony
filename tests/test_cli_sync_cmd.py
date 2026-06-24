"""Coverage for ``run_sync`` in :mod:`pony.cli`.

These tests drive the ``sync`` subcommand through the CLI entry point
``main(["--config", cfg, "sync", ...])`` while patching the
``pony.cli.ImapSession`` symbol so no real network connection is made.
The fake server is provided by :class:`tests.test_sync.FakeImapSession`.
"""

from __future__ import annotations

import dataclasses
import unittest
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_cli import (
    isolated_app_env,
    run_cli_capture,
    temporary_config,
)
from test_sync import FakeImapSession, _make_raw_message

from pony.config import load_config
from pony.domain import (
    AccountConfig,
    FolderRef,
    MessageFlag,
    MessageRef,
    MessageStatus,
)
from pony.imap_client import ImapAuthError
from pony.index_store import SqliteIndexRepository
from pony.message_projection import project_rfc822_message
from pony.paths import AppPaths
from pony.storage import MaildirMirrorRepository

# Type alias for the per-folder seed map FakeImapSession accepts.
_FolderMap = dict[str, dict[int, tuple[str, frozenset[Any], bytes]]]


def _fake_factory(folders: _FolderMap) -> Callable[..., FakeImapSession]:
    """Return a drop-in replacement for ``ImapSession`` yielding a fake.

    ``run_sync`` calls ``ImapSession(host=, port=, ssl=, username=,
    password=)``; we ignore every argument and hand back a freshly
    constructed :class:`FakeImapSession` seeded with *folders* so the
    planner and executor have something to work on.
    """

    def factory(*_args: object, **_kwargs: object) -> FakeImapSession:
        return FakeImapSession(folders)

    return factory


def _seeded_inbox() -> _FolderMap:
    """A single-message INBOX that produces one download op."""
    raw = _make_raw_message("Hello sync", "<seed1@example.com>")
    return {"INBOX": {1: ("<seed1@example.com>", frozenset(), raw)}}


def _seed_local_rows(config_path: Path, count: int) -> None:
    """Insert *count* ACTIVE index rows whose UIDs no longer exist server-side.

    The fake server is given an empty INBOX, so the planner sees every
    seeded row as a server-side deletion.  With more than 20% of the
    folder gone, this drives the mass-deletion confirmation path.
    """
    config = load_config(config_path)
    account = next(iter(config.accounts))
    assert isinstance(account, AccountConfig)
    paths = AppPaths.default()
    paths.ensure_runtime_dirs()

    mirror = MaildirMirrorRepository(
        account_name=account.name,
        root_dir=account.mirror.path,
    )
    folder = FolderRef(account_name=account.name, folder_name="INBOX")
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    for i in range(count):
        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = account.email_address
        msg["Subject"] = f"Old message {i}"
        msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
        msg["Message-ID"] = f"<old{i}@example.com>"
        msg.set_content("body")
        raw = msg.as_bytes()
        storage_key = mirror.store_message(folder=folder, raw_message=raw)
        projected = project_rfc822_message(
            message_ref=MessageRef(
                account_name=account.name,
                folder_name="INBOX",
                id=0,
            ),
            raw_message=raw,
            storage_key=storage_key,
        )
        stored = dataclasses.replace(
            projected,
            message_id=f"<old{i}@example.com>",
            uid=i + 1,
            local_flags=frozenset({MessageFlag.SEEN}),
            base_flags=frozenset({MessageFlag.SEEN}),
            server_flags=frozenset({MessageFlag.SEEN}),
            local_status=MessageStatus.ACTIVE,
        )
        index.insert_message(message=stored)


class RunSyncTestCase(unittest.TestCase):
    """Exercise the ``pony sync`` command surface via ``run_sync``."""

    def test_sync_yes_downloads_seeded_message(self) -> None:
        """``--yes`` plans, executes, and prints the per-account summary."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory(_seeded_inbox())),
        ):
            out, rc = run_cli_capture("--config", str(cfg), "sync", "--yes")
        self.assertEqual(rc, 0)
        self.assertIn("Sync plan", out)
        self.assertIn("1 new message(s)", out)
        # Per-account summary line is emitted for accounts with changes.
        self.assertIn("personal:", out)
        self.assertIn("1 folder(s) with changes", out)

    def test_sync_nothing_to_do_reports_up_to_date(self) -> None:
        """An empty server folder yields the up-to-date short-circuit."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory({"INBOX": {}})),
        ):
            out, rc = run_cli_capture("--config", str(cfg), "sync", "--yes")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to sync", out)

    def test_sync_positional_account_scopes_run(self) -> None:
        """Passing a valid account name scopes the sync to that account."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory(_seeded_inbox())),
        ):
            out, rc = run_cli_capture("--config", str(cfg), "sync", "personal", "--yes")
        self.assertEqual(rc, 0)
        self.assertIn("personal:", out)

    def test_sync_unknown_account_raises_systemexit(self) -> None:
        """An unknown account name aborts before any IMAP work."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory(_seeded_inbox())),
            self.assertRaises(SystemExit) as ctx,
        ):
            run_cli_capture("--config", str(cfg), "sync", "does-not-exist")
        self.assertIn("does-not-exist", str(ctx.exception))
        self.assertIn("No account named", str(ctx.exception))

    def test_sync_auth_failure_surfaces_as_systemexit(self) -> None:
        """A connection that raises is reported as a planning failure.

        The fake factory raises :class:`ImapAuthError`; the planner wraps
        per-account failures into a ``RuntimeError`` which ``run_sync``
        re-raises as ``SystemExit``.
        """

        def raising_factory(*_args: object, **_kwargs: object) -> FakeImapSession:
            raise ImapAuthError("user", "imap.example.com")

        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", raising_factory),
            self.assertRaises(SystemExit) as ctx,
        ):
            run_cli_capture("--config", str(cfg), "sync", "--yes")
        self.assertIn("failed", str(ctx.exception).lower())

    def test_sync_interactive_confirm_yes_executes(self) -> None:
        """Without ``--yes`` an interactive ``y`` proceeds with execution."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory(_seeded_inbox())),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
        ):
            out, rc = run_cli_capture("--config", str(cfg), "sync")
        self.assertEqual(rc, 0)
        self.assertIn("1 folder(s) with changes", out)

    def test_sync_interactive_decline_aborts(self) -> None:
        """An interactive ``n`` aborts without executing."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory(_seeded_inbox())),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="n"),
        ):
            out, rc = run_cli_capture("--config", str(cfg), "sync")
        self.assertEqual(rc, 0)
        self.assertIn("Aborted", out)
        # The execution summary should not appear after an abort.
        self.assertNotIn("folder(s) with changes", out)

    def test_sync_interactive_eof_aborts(self) -> None:
        """EOF on the prompt is treated as an empty answer (abort)."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory(_seeded_inbox())),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=EOFError),
        ):
            out, rc = run_cli_capture("--config", str(cfg), "sync")
        self.assertEqual(rc, 0)
        self.assertIn("Aborted", out)

    def test_sync_interactive_keyboard_interrupt_aborts(self) -> None:
        """Ctrl-C at the prompt aborts cleanly with rc 0."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory(_seeded_inbox())),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=KeyboardInterrupt),
        ):
            out, rc = run_cli_capture("--config", str(cfg), "sync")
        self.assertEqual(rc, 0)
        self.assertIn("Aborted", out)

    def test_sync_interactive_list_then_confirm(self) -> None:
        """Answering ``l`` opens the pager, then ``y`` proceeds."""
        answers: Iterator[str] = iter(["l", "y"])

        def _answer(*_args: object) -> str:
            return next(answers)

        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory(_seeded_inbox())),
            patch("sys.stdin.isatty", return_value=True),
            patch("pony.cli._run_pager") as pager,
            patch("builtins.input", side_effect=_answer),
        ):
            out, rc = run_cli_capture("--config", str(cfg), "sync")
        self.assertEqual(rc, 0)
        self.assertTrue(pager.called)
        self.assertIn("1 folder(s) with changes", out)

    def test_sync_mass_deletion_warns_and_confirms(self) -> None:
        """A folder losing >20% of known mail triggers the CONFIRM warning."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory({"INBOX": {}})),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
        ):
            _seed_local_rows(cfg, 5)
            out, rc = run_cli_capture("--config", str(cfg), "sync")
        self.assertEqual(rc, 0)
        self.assertIn("large server-side deletions", out)
        self.assertIn("[CONFIRM:", out)
        # The folder still ends up in the executed summary.
        self.assertIn("personal:", out)

    def test_sync_mass_deletion_list_shows_ops_table(self) -> None:
        """Answering ``l`` renders the deletions in the pager detail table."""
        captured: list[str] = []

        def _capture_pager(text: str) -> None:
            captured.append(text)

        answers: Iterator[str] = iter(["l", "y"])

        def _answer(*_args: object) -> str:
            return next(answers)

        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory({"INBOX": {}})),
            patch("sys.stdin.isatty", return_value=True),
            patch("pony.cli._run_pager", side_effect=_capture_pager),
            patch("builtins.input", side_effect=_answer),
        ):
            _seed_local_rows(cfg, 5)
            out, rc = run_cli_capture("--config", str(cfg), "sync")
        self.assertEqual(rc, 0)
        self.assertTrue(captured)
        # The ops table lists the server→trash deletions.
        self.assertIn("server", captured[0])
        self.assertIn("Old message", captured[0])

    def test_sync_not_a_tty_refuses_without_yes(self) -> None:
        """A non-TTY stdin without ``--yes`` refuses with rc 1."""
        with (
            isolated_app_env(),
            temporary_config() as cfg,
            patch("pony.cli.ImapSession", _fake_factory(_seeded_inbox())),
            patch("sys.stdin.isatty", return_value=False),
        ):
            out, rc = run_cli_capture("--config", str(cfg), "sync")
        self.assertEqual(rc, 1)
        self.assertIn("not a TTY", out)


if __name__ == "__main__":
    unittest.main()
