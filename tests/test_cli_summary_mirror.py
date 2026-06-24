"""Coverage for ``server-summary``, ``folder mirror``, and ``local-summary``.

These exercise :func:`pony.cli.run_server_summary`,
:func:`pony.cli.run_mirror_folder`, and the table-printing branches of
:func:`pony.cli.run_local_summary` / :func:`pony.cli.run_folder_list` that
the existing ``tests/test_cli.py`` smoke tests do not reach.
"""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from test_cli import (
    _seed_one_message,
    isolated_app_env,
    run_cli,
    run_cli_capture,
    sample_config_toml,
    temporary_config,
)

import pony.cli


class _FakeImapSession:
    """Stand-in for :class:`pony.imap_client.ImapSession`.

    Recorded canned data lets ``run_server_summary`` print a folder table
    without touching a real server.  Class attributes carry the scripted
    responses so the constructor signature can stay identical to the real
    session.
    """

    folders: Sequence[str] = ()
    status: dict[str, tuple[int, int]] = {}  # noqa: RUF012
    last_dates: dict[str, str | None] = {}  # noqa: RUF012
    raise_on_status: set[str] = set()  # noqa: RUF012
    raise_on_last: set[str] = set()  # noqa: RUF012
    raise_on_list: bool = False
    logged_out = False

    def __init__(self, **_kwargs: Any) -> None:  # noqa: ANN401
        type(self).logged_out = False

    def list_folders(self) -> Sequence[str]:
        if type(self).raise_on_list:
            raise OSError("list boom")
        return type(self).folders

    def get_folder_status(self, folder_name: str) -> tuple[int, int]:
        if folder_name in type(self).raise_on_status:
            raise OSError("status boom")
        return type(self).status.get(folder_name, (0, 0))

    def fetch_last_message_date(self, folder_name: str) -> str | None:
        if folder_name in type(self).raise_on_last:
            raise OSError("date boom")
        return type(self).last_dates.get(folder_name)

    def logout(self) -> None:
        type(self).logged_out = True


def _make_fake(**attrs: Any) -> type[_FakeImapSession]:  # noqa: ANN401
    """Return a fresh ``_FakeImapSession`` subclass with the given scripting."""
    return type("_ScriptedSession", (_FakeImapSession,), dict(attrs))


def _config_with_policy() -> str:
    """A single-account config that marks one folder ignored, one read-only."""
    return (
        "config_version = 2\n"
        "[[accounts]]\n"
        'name = "personal"\n'
        'email_address = "user@example.com"\n'
        'imap_host = "imap.example.com"\n'
        'username = "user"\n'
        'credentials_source = "plaintext"\n'
        'password = "test-password"\n'
        "[accounts.smtp]\n"
        'host = "smtp.example.com"\n'
        "[accounts.mirror]\n"
        'path = "mirrors/personal"\n'
        'format = "maildir"\n'
        "[accounts.folders]\n"
        'exclude = ["Junk"]\n'
        'read_only = ["Archive"]\n'
    )


class ServerSummaryTest(unittest.TestCase):
    """``pony server-summary`` against a patched ImapSession."""

    def test_prints_folder_table_with_counts_and_dates(self) -> None:
        fake = _make_fake(
            folders=["INBOX", "Archive", "Junk"],
            status={"INBOX": (5, 2), "Archive": (3, 0), "Junk": (0, 0)},
            last_dates={"INBOX": "2026-04-17", "Archive": "2026-01-01"},
        )
        with isolated_app_env() as env_root:
            config_path = env_root / "config" / "config.toml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(_config_with_policy(), encoding="utf-8")
            with mock.patch.object(pony.cli, "ImapSession", fake):
                output = run_cli("--config", str(config_path), "server-summary")
        self.assertIn("Account: personal", output)
        self.assertIn("INBOX", output)
        # Unseen count is shown only when non-zero.
        self.assertIn("2", output)
        self.assertIn("2026-04-17", output)
        # Ignored ([I]) and read-only ([R]) tags.
        self.assertIn("[I]", output)
        self.assertIn("[R]", output)
        self.assertIn("ignored", output)
        self.assertTrue(fake.logged_out)

    def test_status_error_is_reported_per_folder(self) -> None:
        fake = _make_fake(
            folders=["INBOX"],
            raise_on_status={"INBOX"},
        )
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch.object(pony.cli, "ImapSession", fake),
        ):
            output = run_cli("--config", str(config_path), "server-summary")
        self.assertIn("error:", output)
        self.assertIn("status boom", output)

    def test_last_date_error_falls_back_to_dash(self) -> None:
        fake = _make_fake(
            folders=["INBOX"],
            status={"INBOX": (4, 0)},
            raise_on_last={"INBOX"},
        )
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch.object(pony.cli, "ImapSession", fake),
        ):
            output = run_cli("--config", str(config_path), "server-summary")
        self.assertIn("INBOX", output)
        self.assertIn("—", output)  # em dash placeholder

    def test_list_folders_error_continues(self) -> None:
        fake = _make_fake(raise_on_list=True)
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch.object(pony.cli, "ImapSession", fake),
        ):
            output = run_cli("--config", str(config_path), "server-summary")
        self.assertIn("Could not list folders", output)
        self.assertTrue(fake.logged_out)

    def test_connect_failure_is_reported(self) -> None:
        def _boom(**_kwargs: Any) -> _FakeImapSession:  # noqa: ANN401
            raise ConnectionError("no route")

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch.object(pony.cli, "ImapSession", _boom),
        ):
            output = run_cli("--config", str(config_path), "server-summary")
        self.assertIn("Could not connect", output)

    def test_account_scope_selects_one(self) -> None:
        fake = _make_fake(folders=["INBOX"], status={"INBOX": (1, 0)})
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch.object(pony.cli, "ImapSession", fake),
        ):
            output = run_cli("--config", str(config_path), "server-summary", "personal")
        self.assertIn("Account: personal", output)

    def test_unknown_account_raises(self) -> None:
        fake = _make_fake(folders=["INBOX"])
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch.object(pony.cli, "ImapSession", fake),
            self.assertRaises(SystemExit) as ctx,
        ):
            run_cli("--config", str(config_path), "server-summary", "ghost")
        self.assertIn("ghost", str(ctx.exception))


class MirrorFolderTest(unittest.TestCase):
    """``pony folder mirror`` copies messages between local folders."""

    def _mirror_args(
        self, config_path: Path, dst_folder: str = "Archive"
    ) -> tuple[str, ...]:
        return (
            "--config",
            str(config_path),
            "folder",
            "mirror",
            "personal",
            "INBOX",
            "personal",
            dst_folder,
        )

    def test_copies_messages_and_writes_destination_rows(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Hi", body="body")
            output, rc = run_cli_capture(*self._mirror_args(config_path))

            self.assertEqual(rc, 0)
            self.assertIn("Copying 1 message", output)
            self.assertIn("Run `pony sync`", output)

            # Destination folder now holds an indexed row with uid=NULL.
            from pony.domain import FolderRef
            from pony.index_store import SqliteIndexRepository
            from pony.paths import AppPaths

            paths = AppPaths.default()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            dst = index.list_folder_messages(
                folder=FolderRef(account_name="personal", folder_name="Archive")
            )
            self.assertEqual(len(dst), 1)
            self.assertIsNone(dst[0].uid)

    def test_empty_source_raises(self) -> None:
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            self.assertRaises(SystemExit) as ctx,
        ):
            run_cli(*self._mirror_args(config_path))
        self.assertIn("No locally available messages", str(ctx.exception))

    def test_unknown_src_account_raises(self) -> None:
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            self.assertRaises(SystemExit) as ctx,
        ):
            run_cli(
                "--config",
                str(config_path),
                "folder",
                "mirror",
                "ghost",
                "INBOX",
                "personal",
                "Archive",
            )
        self.assertIn("ghost", str(ctx.exception))

    def test_unknown_dst_account_raises(self) -> None:
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            self.assertRaises(SystemExit) as ctx,
        ):
            run_cli(
                "--config",
                str(config_path),
                "folder",
                "mirror",
                "personal",
                "INBOX",
                "ghost",
                "Archive",
            )
        self.assertIn("ghost", str(ctx.exception))

    def test_nonempty_destination_noninteractive_aborts(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Src", body="src body")
            # Seed a message directly into the destination folder so it is
            # non-empty.  ``folder mirror`` reuses the source as destination.
            self._seed_into(config_path, folder_name="Archive", subject="Dst")
            with (
                mock.patch("sys.stdin.isatty", return_value=False),
                self.assertRaises(SystemExit) as ctx,
            ):
                run_cli(*self._mirror_args(config_path))
        self.assertIn("not empty", str(ctx.exception))

    def test_nonempty_destination_overwrite_confirmed(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Src", body="src body")
            self._seed_into(config_path, folder_name="Archive", subject="Dst")
            with (
                mock.patch("sys.stdin.isatty", return_value=True),
                mock.patch.object(pony.cli, "input", create=True, return_value="y"),
            ):
                output, rc = run_cli_capture(*self._mirror_args(config_path))
        self.assertEqual(rc, 0)
        self.assertIn("Clearing", output)
        self.assertIn("Copying", output)

    def test_nonempty_destination_overwrite_declined(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Src", body="src body")
            self._seed_into(config_path, folder_name="Archive", subject="Dst")
            with (
                mock.patch("sys.stdin.isatty", return_value=True),
                mock.patch.object(pony.cli, "input", create=True, return_value="n"),
            ):
                output, rc = run_cli_capture(*self._mirror_args(config_path))
        self.assertEqual(rc, 1)
        self.assertIn("Aborted.", output)

    def test_unreadable_source_file_is_skipped(self) -> None:
        from pony.storage import MaildirMirrorRepository

        def _boom(*_args: Any, **_kwargs: Any) -> bytes:  # noqa: ANN401
            raise OSError("unreadable")

        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Hi", body="body")
            with mock.patch.object(MaildirMirrorRepository, "get_message_bytes", _boom):
                output, rc = run_cli_capture(*self._mirror_args(config_path))
        self.assertEqual(rc, 0)
        self.assertIn("Warning: skipping", output)
        self.assertIn("Skipped 1 message", output)

    def test_delete_failure_during_overwrite_is_logged(self) -> None:
        from pony.storage import MaildirMirrorRepository

        def _boom(*_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
            raise OSError("cannot delete")

        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Src", body="src body")
            self._seed_into(config_path, folder_name="Archive", subject="Dst")
            with (
                mock.patch("sys.stdin.isatty", return_value=True),
                mock.patch.object(pony.cli, "input", create=True, return_value="y"),
                mock.patch.object(MaildirMirrorRepository, "delete_message", _boom),
            ):
                output, rc = run_cli_capture(*self._mirror_args(config_path))
        self.assertEqual(rc, 0)
        self.assertIn("Clearing", output)

    @staticmethod
    def _seed_into(config_path: Path, *, folder_name: str, subject: str) -> None:
        """Seed one ACTIVE message into an arbitrary folder of ``personal``."""
        import dataclasses
        from email.message import EmailMessage

        from pony.config import load_config
        from pony.domain import (
            AccountConfig,
            FolderRef,
            MessageRef,
            MessageStatus,
        )
        from pony.index_store import SqliteIndexRepository
        from pony.message_projection import project_rfc822_message
        from pony.paths import AppPaths
        from pony.storage import MaildirMirrorRepository

        config = load_config(config_path)
        account = next(iter(config.accounts))
        assert isinstance(account, AccountConfig)
        paths = AppPaths.default()
        paths.ensure_runtime_dirs()

        msg = EmailMessage()
        msg["From"] = "sender@example.com"
        msg["To"] = account.email_address
        msg["Subject"] = subject
        msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
        msg["Message-ID"] = "<dst-seed@example.com>"
        msg.set_content("destination body")
        raw = msg.as_bytes()

        mirror = MaildirMirrorRepository(
            account_name=account.name,
            root_dir=account.mirror.path,
        )
        folder = FolderRef(account_name=account.name, folder_name=folder_name)
        mirror.create_folder(account_name=account.name, folder_name=folder_name)
        storage_key = mirror.store_message(folder=folder, raw_message=raw)

        projected = project_rfc822_message(
            message_ref=MessageRef(
                account_name=account.name,
                folder_name=folder_name,
                id=0,
            ),
            raw_message=raw,
            storage_key=storage_key,
        )
        stored = dataclasses.replace(projected, local_status=MessageStatus.ACTIVE)
        index = SqliteIndexRepository(database_path=paths.index_db_file)
        index.initialize()
        index.insert_message(message=stored)


class LocalSummaryTableTest(unittest.TestCase):
    """``pony local-summary`` table rows once a folder is populated."""

    def test_summary_lists_seeded_folder_counts(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Hi", body="body")
            output = run_cli("--config", str(config_path), "local-summary")
        self.assertIn("Account: personal", output)
        self.assertIn("INBOX", output)
        self.assertIn("Folder", output)
        self.assertIn("Indexed", output)

    def test_summary_account_scope_with_data(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Hi", body="body")
            output = run_cli("--config", str(config_path), "local-summary", "personal")
        self.assertIn("INBOX", output)


class FolderListTableTest(unittest.TestCase):
    """``pony folder list`` once a folder exists in the mirror."""

    def test_lists_seeded_folder_with_count(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Hi", body="body")
            output = run_cli("--config", str(config_path), "folder", "list")
        self.assertIn("personal:", output)
        self.assertIn("INBOX", output)
        self.assertIn("never synced", output)

    def test_account_scope(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Hi", body="body")
            output = run_cli("--config", str(config_path), "folder", "list", "personal")
        self.assertIn("personal:", output)

    def test_unknown_account_raises(self) -> None:
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            self.assertRaises(SystemExit) as ctx,
        ):
            run_cli("--config", str(config_path), "folder", "list", "ghost")
        self.assertIn("ghost", str(ctx.exception))

    def test_lists_synced_folder_shows_last_sync(self) -> None:
        from datetime import UTC, datetime

        from pony.domain import FolderSyncState
        from pony.index_store import SqliteIndexRepository
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Hi", body="body")
            paths = AppPaths.default()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            index.record_folder_sync_state(
                state=FolderSyncState(
                    account_name="personal",
                    folder_name="INBOX",
                    uid_validity=1,
                    highest_uid=7,
                    synced_at=datetime(2026, 4, 17, 12, 0, tzinfo=UTC),
                )
            )
            output = run_cli("--config", str(config_path), "folder", "list")
            # And local-summary picks up the recorded sync timestamp too.
            summary = run_cli("--config", str(config_path), "local-summary")
        self.assertIn("last sync 2026-04-17", output)
        self.assertIn("uid 7", output)
        self.assertIn("2026-04-17", summary)


if __name__ == "__main__":
    unittest.main()


# Silence unused-import lint when ``sample_config_toml`` stays unreferenced
# in a future refactor; it is part of the reusable helper surface.
_ = sample_config_toml
