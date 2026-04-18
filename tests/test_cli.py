"""CLI tests for the Phase 1 scaffold."""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

from conftest import TMP_ROOT

from pony.cli import main


class CliTestCase(unittest.TestCase):
    """Exercise the command surface exposed in Phase 1."""

    def test_doctor_runs_without_config(self) -> None:
        with isolated_app_env():
            output = run_cli("doctor")
        self.assertIn("Pony Express doctor", output)
        self.assertIn("[ERROR] Config file", output)
        self.assertIn("Not found:", output)

    def test_sync_reports_planning_failure(self) -> None:
        # With an unreachable server the planning pass raises, and the
        # CLI should surface the error.
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli("--config", str(config_path), "sync", "--yes")
            self.assertIn("failed", str(ctx.exception).lower())

    def test_account_add_mentions_target_file(self) -> None:
        output = run_cli("account", "add", "personal")
        self.assertIn("personal", output)
        self.assertIn("config.toml", output)

    def test_doctor_includes_index_path_line(self) -> None:
        with isolated_app_env():
            output = run_cli("doctor")
        self.assertIn("Index DB:", output)

    def test_doctor_with_valid_config_shows_ok(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            output = run_cli("--config", str(config_path), "doctor")
        self.assertIn("[OK   ] Config file", output)
        self.assertIn("personal", output)

    def test_doctor_shows_mirror_warning_when_path_missing(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            output = run_cli("--config", str(config_path), "doctor")
        # Mirror path in the sample config doesn't exist yet
        self.assertIn('[WARN ] Mirror "personal"', output)

    def test_doctor_shows_summary_line(self) -> None:
        with isolated_app_env():
            output = run_cli("doctor")
        # Should end with either "All N checks passed." or "N OK, ..."
        self.assertTrue(
            "checks passed" in output or " OK," in output,
            msg=f"Summary line not found in:\n{output}",
        )

    def test_search_uses_indexed_fixture_data(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            run_cli("--config", str(config_path), "fixture-ingest")
            output = run_cli("--config", str(config_path), "search", "fixture")
        self.assertIn("Search results", output)
        self.assertIn("Total hits: 1", output)

    def test_doctor_creates_runtime_directories(self) -> None:
        with isolated_app_env() as env_root:
            run_cli("doctor")
            self.assertTrue((env_root / "config" / "pony").exists())
            self.assertTrue((env_root / "data" / "pony").exists())
            self.assertTrue((env_root / "state" / "pony" / "logs").exists())
            self.assertTrue((env_root / "cache" / "pony").exists())

    def test_rescan_refreshes_body_preview(self) -> None:
        """`pony rescan` re-projects stored rows and fixes stale previews."""
        from email.message import EmailMessage

        from pony.config import load_config
        from pony.domain import FolderRef, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.message_projection import project_rfc822_message
        from pony.paths import AppPaths
        from pony.storage import MaildirMirrorRepository

        with isolated_app_env(), temporary_config() as config_path:
            config = load_config(config_path)
            account = next(iter(config.accounts))
            paths = AppPaths.default()
            paths.ensure_runtime_dirs()

            html_msg = EmailMessage()
            html_msg["From"] = "sender@example.com"
            html_msg["To"] = account.email_address
            html_msg["Subject"] = "HTML with CSS"
            html_msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
            html_msg.set_content(
                "<html><head><style>.x{color:red}</style></head>"
                "<body><p>Real body text</p></body></html>",
                subtype="html",
            )
            raw = html_msg.as_bytes()

            mirror = MaildirMirrorRepository(
                account_name=account.name, root_dir=account.mirror.path,
            )
            folder = FolderRef(account_name=account.name, folder_name="INBOX")
            message_ref = mirror.store_message(folder=folder, raw_message=raw)

            # Seed the index with a row whose body_preview simulates the
            # pre-fix bug (CSS text leaked into the preview).
            projected = project_rfc822_message(
                message_ref=message_ref, raw_message=raw,
                storage_key=message_ref.message_id,
            )
            import dataclasses
            stale = dataclasses.replace(
                projected,
                body_preview=".x{color:red} Real body text",
                local_status=MessageStatus.ACTIVE,
            )
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            index.upsert_message(message=stale)

            output = run_cli("--config", str(config_path), "rescan")
            self.assertIn("Rescan complete", output)
            self.assertIn("1/1", output)

            refreshed = index.get_message(message_ref=message_ref)
            assert refreshed is not None
            self.assertNotIn("color:red", refreshed.body_preview)
            self.assertNotIn(".x{", refreshed.body_preview)
            self.assertIn("Real body text", refreshed.body_preview)

    def test_rescan_preserves_sync_state(self) -> None:
        """Rescan must not overwrite flags, uid, or other sync-state fields."""
        from email.message import EmailMessage

        from pony.config import load_config
        from pony.domain import FolderRef, MessageFlag, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.message_projection import project_rfc822_message
        from pony.paths import AppPaths
        from pony.storage import MaildirMirrorRepository

        with isolated_app_env(), temporary_config() as config_path:
            config = load_config(config_path)
            account = next(iter(config.accounts))
            paths = AppPaths.default()
            paths.ensure_runtime_dirs()

            msg = EmailMessage()
            msg["From"] = "sender@example.com"
            msg["To"] = account.email_address
            msg["Subject"] = "Sync state test"
            msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
            msg.set_content("plain body")
            raw = msg.as_bytes()

            mirror = MaildirMirrorRepository(
                account_name=account.name, root_dir=account.mirror.path,
            )
            folder = FolderRef(account_name=account.name, folder_name="INBOX")
            message_ref = mirror.store_message(folder=folder, raw_message=raw)

            projected = project_rfc822_message(
                message_ref=message_ref, raw_message=raw,
                storage_key=message_ref.message_id,
            )
            import dataclasses
            with_sync_state = dataclasses.replace(
                projected,
                uid=4242,
                local_flags=frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
                base_flags=frozenset({MessageFlag.SEEN}),
                local_status=MessageStatus.ACTIVE,
                body_preview="stale preview",
            )
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            index.upsert_message(message=with_sync_state)

            run_cli("--config", str(config_path), "rescan")

            refreshed = index.get_message(message_ref=message_ref)
            assert refreshed is not None
            self.assertEqual(refreshed.uid, 4242)
            self.assertEqual(
                refreshed.local_flags,
                frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
            )
            self.assertEqual(refreshed.base_flags, frozenset({MessageFlag.SEEN}))
            self.assertIn("plain body", refreshed.body_preview)
            self.assertNotEqual(refreshed.body_preview, "stale preview")

    def test_rescan_unknown_account_errors(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli("--config", str(config_path), "rescan", "nonexistent")
            self.assertIn("nonexistent", str(ctx.exception))


def run_cli(*argv: str) -> str:
    """Capture CLI stdout for one invocation."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        main(argv)
    return buffer.getvalue()


@contextlib.contextmanager
def temporary_config() -> Iterator[Path]:
    """Yield a temporary valid config file."""
    temp_root = TMP_ROOT
    temp_root.mkdir(exist_ok=True)
    config_path = temp_root / "config.toml"
    config_path.write_text(sample_config_toml(), encoding="utf-8")
    try:
        yield config_path
    finally:
        config_path.unlink(missing_ok=True)


@contextlib.contextmanager
def isolated_app_env() -> Iterator[Path]:
    """Create isolated app directories through PONY_* environment overrides."""
    env_root = TMP_ROOT / "env" / uuid4().hex
    config_dir = env_root / "config"
    data_dir = env_root / "data"
    state_dir = env_root / "state"
    cache_dir = env_root / "cache"
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    previous = {
        "PONY_CONFIG_DIR": os.environ.get("PONY_CONFIG_DIR"),
        "PONY_DATA_DIR": os.environ.get("PONY_DATA_DIR"),
        "PONY_STATE_DIR": os.environ.get("PONY_STATE_DIR"),
        "PONY_CACHE_DIR": os.environ.get("PONY_CACHE_DIR"),
    }

    os.environ["PONY_CONFIG_DIR"] = str(config_dir)
    os.environ["PONY_DATA_DIR"] = str(data_dir)
    os.environ["PONY_STATE_DIR"] = str(state_dir)
    os.environ["PONY_CACHE_DIR"] = str(cache_dir)
    try:
        yield env_root
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def sample_config_toml() -> str:
    """Return a minimal valid TOML app configuration."""
    return """
[[accounts]]
name = "personal"
email_address = "user@example.com"
imap_host = "imap.example.com"
smtp_host = "smtp.example.com"
username = "user"
credentials_source = "plaintext"
password = "test-password"

[accounts.mirror]
path = "mirrors/personal"
format = "maildir"
trash_retention_days = 30
""".strip()
