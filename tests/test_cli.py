"""CLI tests for the Phase 1 scaffold."""

from __future__ import annotations

import contextlib
import io
import os
import sys
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
        from pony.domain import FolderRef, MessageRef, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.message_projection import project_rfc822_message
        from pony.paths import AppPaths
        from pony.storage import MaildirMirrorRepository

        with isolated_app_env(), temporary_config() as config_path:
            config = load_config(config_path)
            account = next(iter(config.accounts))
            paths = AppPaths.default()
            paths.ensure_runtime_dirs()

            rfc5322_id = f"<rescan-test-{uuid4().hex}@example.com>"
            html_msg = EmailMessage()
            html_msg["From"] = "sender@example.com"
            html_msg["To"] = account.email_address
            html_msg["Subject"] = "HTML with CSS"
            html_msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
            html_msg["Message-ID"] = rfc5322_id
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
            storage_key = mirror.store_message(folder=folder, raw_message=raw)
            message_ref = MessageRef(
                account_name=account.name,
                folder_name="INBOX",
                rfc5322_id=rfc5322_id,
            )

            # Seed the index with a row whose body_preview simulates the
            # pre-fix bug (CSS text leaked into the preview).
            projected = project_rfc822_message(
                message_ref=message_ref, raw_message=raw,
                storage_key=storage_key,
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
        from pony.domain import FolderRef, MessageFlag, MessageRef, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.message_projection import project_rfc822_message
        from pony.paths import AppPaths
        from pony.storage import MaildirMirrorRepository

        with isolated_app_env(), temporary_config() as config_path:
            config = load_config(config_path)
            account = next(iter(config.accounts))
            paths = AppPaths.default()
            paths.ensure_runtime_dirs()

            rfc5322_id = f"<sync-state-{uuid4().hex}@example.com>"
            msg = EmailMessage()
            msg["From"] = "sender@example.com"
            msg["To"] = account.email_address
            msg["Subject"] = "Sync state test"
            msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
            msg["Message-ID"] = rfc5322_id
            msg.set_content("plain body")
            raw = msg.as_bytes()

            mirror = MaildirMirrorRepository(
                account_name=account.name, root_dir=account.mirror.path,
            )
            folder = FolderRef(account_name=account.name, folder_name="INBOX")
            storage_key = mirror.store_message(folder=folder, raw_message=raw)
            message_ref = MessageRef(
                account_name=account.name,
                folder_name="INBOX",
                rfc5322_id=rfc5322_id,
            )

            projected = project_rfc822_message(
                message_ref=message_ref, raw_message=raw,
                storage_key=storage_key,
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

    def test_folder_list_shows_counts_and_sync_status(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            message_ref = _seed_one_message(
                config_path, subject="Folder list probe", body="hello",
            )
            output = run_cli("--config", str(config_path), "folder", "list")
        self.assertIn("personal:", output)
        self.assertIn("INBOX", output)
        self.assertIn("1 messages", output)
        self.assertIn("never synced", output)
        self.assertIsNotNone(message_ref)  # quiet unused-variable warning

    def test_folder_list_unknown_account_errors(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config", str(config_path),
                    "folder", "list", "nonexistent",
                )
            self.assertIn("nonexistent", str(ctx.exception))

    def test_message_get_prints_metadata(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            message_ref = _seed_one_message(
                config_path, subject="Metadata probe", body="body-text",
            )
            output = run_cli(
                "--config", str(config_path), "message", "get",
                message_ref.account_name,
                message_ref.folder_name,
                message_ref.rfc5322_id,
            )
        self.assertIn("Metadata probe", output)
        self.assertIn("sender@example.com", output)
        # `message get` is metadata-only — body text lives in the mirror
        # and must be fetched via `message body`.
        self.assertNotIn("body-text", output)
        self.assertNotIn("Preview:", output)

    def test_message_get_unknown_errors(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config", str(config_path), "message", "get",
                    "personal", "INBOX", "does-not-exist",
                )
            self.assertIn("not found", str(ctx.exception).lower())

    def test_message_body_renders_from_mirror(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            message_ref = _seed_one_message(
                config_path,
                subject="Body probe",
                body="This is the actual body text.",
            )
            output = run_cli(
                "--config", str(config_path), "message", "body",
                message_ref.account_name,
                message_ref.folder_name,
                message_ref.rfc5322_id,
            )
        self.assertIn("Subject: Body probe", output)
        self.assertIn("This is the actual body text.", output)

    def test_message_body_missing_errors(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config", str(config_path), "message", "body",
                    "personal", "INBOX", "does-not-exist",
                )
            self.assertIn(
                "not found", str(ctx.exception).lower(),
            )

    def test_message_get_lists_attachments_individually(self) -> None:
        """Previously showed only 'Attach.: yes/no'; now each attachment
        is listed by index, name, content-type, and size — matching
        what 'pony message body' and the MCP tools already return."""
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_message_with_attachments(config_path)
            output = run_cli(
                "--config", str(config_path), "message", "get",
                ref.account_name, ref.folder_name, ref.rfc5322_id,
            )
        self.assertIn("Attach.:    yes (1)", output)
        self.assertIn("1. q1-report.pdf", output)
        self.assertIn("application/octet-stream", output)

    def test_message_attachment_writes_file_to_cwd(self) -> None:
        import tempfile
        with isolated_app_env(), temporary_config() as config_path, \
                tempfile.TemporaryDirectory() as tmpdir:
            ref = _seed_message_with_attachments(config_path)
            prev_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                output = run_cli(
                    "--config", str(config_path), "message", "attachment",
                    ref.account_name, ref.folder_name, ref.rfc5322_id, "1",
                )
            finally:
                os.chdir(prev_cwd)
            written = Path(tmpdir) / "q1-report.pdf"
            self.assertTrue(written.exists())
            self.assertTrue(written.read_bytes().startswith(b"%PDF"))
            self.assertIn("Wrote", output)

    def test_message_attachment_refuses_overwrite_without_force(self) -> None:
        import tempfile
        with isolated_app_env(), temporary_config() as config_path, \
                tempfile.TemporaryDirectory() as tmpdir:
            ref = _seed_message_with_attachments(config_path)
            out_path = Path(tmpdir) / "out.bin"
            out_path.write_bytes(b"untouched")
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config", str(config_path), "message", "attachment",
                    ref.account_name, ref.folder_name, ref.rfc5322_id, "1",
                    "-o", str(out_path),
                )
            self.assertIn("Refusing to overwrite", str(ctx.exception))
            # File was not clobbered.
            self.assertEqual(out_path.read_bytes(), b"untouched")

    def test_message_attachment_force_overwrites(self) -> None:
        import tempfile
        with isolated_app_env(), temporary_config() as config_path, \
                tempfile.TemporaryDirectory() as tmpdir:
            ref = _seed_message_with_attachments(config_path)
            out_path = Path(tmpdir) / "out.bin"
            out_path.write_bytes(b"untouched")
            run_cli(
                "--config", str(config_path), "message", "attachment",
                ref.account_name, ref.folder_name, ref.rfc5322_id, "1",
                "-o", str(out_path), "--force",
            )
            self.assertTrue(out_path.read_bytes().startswith(b"%PDF"))

    def test_message_attachment_out_of_range_errors(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_message_with_attachments(config_path)
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config", str(config_path), "message", "attachment",
                    ref.account_name, ref.folder_name, ref.rfc5322_id, "99",
                )
            self.assertIn("not found", str(ctx.exception).lower())


class SchemaMismatchRecoveryTests(unittest.TestCase):
    """The CLI must explain, prompt (default N), and only reset on y/yes."""

    def _seed_legacy_db_and_mirror(
        self, config_path: Path,
    ) -> tuple[Path, Path]:
        """Create an out-of-date index DB and a mirror directory to delete."""
        import sqlite3

        from pony.config import load_config
        from pony.paths import AppPaths

        config = load_config(config_path)
        account = next(iter(config.accounts))
        app_paths = AppPaths.default()
        app_paths.ensure_runtime_dirs()
        account.mirror.path.mkdir(parents=True, exist_ok=True)
        (account.mirror.path / "INBOX").mkdir(exist_ok=True)

        db = app_paths.index_db_file
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.executescript(
                """
                CREATE TABLE messages (account_name TEXT);
                CREATE TABLE contacts (
                    id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    affix TEXT NOT NULL DEFAULT '[]',
                    organization TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE contact_emails (
                    contact_id INTEGER NOT NULL,
                    email_address TEXT NOT NULL,
                    PRIMARY KEY (email_address)
                );
                CREATE TABLE contact_aliases (
                    contact_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    PRIMARY KEY (contact_id, alias)
                );
                INSERT INTO contacts VALUES
                    (1, 'María', 'López', '[]', '', '', 1,
                     '2026-04-10T12:00:00+00:00',
                     '2026-04-10T12:00:00+00:00',
                     '2026-04-10T12:00:00+00:00');
                INSERT INTO contact_emails VALUES (1, 'maria@example.com');
                PRAGMA user_version = 0;
                """,
            )
            conn.commit()
        finally:
            conn.close()
        return db, account.mirror.path

    def test_prompt_defaults_to_no_and_preserves_state(self) -> None:
        """Pressing Enter (empty input) must abort without deleting anything."""
        with isolated_app_env(), temporary_config() as config_path:
            db, mirror = self._seed_legacy_db_and_mirror(config_path)

            stdin_backup = sys.stdin
            sys.stdin = io.StringIO("\n")  # empty line → default N
            try:
                rc = run_cli_ret(
                    "--config", str(config_path), "search", "anything",
                )
            finally:
                sys.stdin = stdin_backup

            self.assertEqual(rc, 1)
            self.assertTrue(db.exists(), "DB must survive a declined prompt")
            self.assertTrue(mirror.exists(), "mirror must survive too")

    def test_prompt_yes_exports_contacts_and_deletes(self) -> None:
        """Answering y must export contacts and delete DB + mirrors."""
        from pony.bbdb import read_bbdb

        with isolated_app_env() as env_root, temporary_config() as config_path:
            db, mirror = self._seed_legacy_db_and_mirror(config_path)

            stdin_backup = sys.stdin
            sys.stdin = io.StringIO("y\n")
            try:
                captured, rc = run_cli_capture(
                    "--config", str(config_path), "search", "anything",
                )
            finally:
                sys.stdin = stdin_backup

            self.assertEqual(rc, 0)
            self.assertFalse(db.exists(), "DB must be deleted after yes")
            self.assertFalse(mirror.exists(), "mirror must be deleted after yes")

            # The backup BBDB file must contain the one legacy contact.
            backups = list((env_root / "data" / "pony").glob("contacts-backup-*.bbdb"))
            self.assertEqual(len(backups), 1, captured)
            loaded = read_bbdb(backups[0])
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].first_name, "María")
            self.assertIn("pony sync", captured)
            self.assertIn("contacts import", captured)


def _seed_one_message(
    config_path: Path, *, subject: str, body: str,
):
    """Seed one real maildir message + matching index row for the personal account.

    Returns the ``MessageRef`` whose ``message_id`` is the RFC 5322
    header value — matching what IMAP sync populates.  The maildir
    filename (``storage_key``) is a different string.
    """
    import dataclasses
    from email.message import EmailMessage

    from pony.config import load_config
    from pony.domain import FolderRef, MessageRef, MessageStatus
    from pony.index_store import SqliteIndexRepository
    from pony.message_projection import project_rfc822_message
    from pony.paths import AppPaths
    from pony.storage import MaildirMirrorRepository

    config = load_config(config_path)
    account = next(iter(config.accounts))
    paths = AppPaths.default()
    paths.ensure_runtime_dirs()

    rfc5322_id = f"<test-{uuid4().hex}@example.com>"
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = account.email_address
    msg["Subject"] = subject
    msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
    msg["Message-ID"] = rfc5322_id
    msg.set_content(body)
    raw = msg.as_bytes()

    mirror = MaildirMirrorRepository(
        account_name=account.name, root_dir=account.mirror.path,
    )
    folder = FolderRef(account_name=account.name, folder_name="INBOX")
    storage_key = mirror.store_message(folder=folder, raw_message=raw)

    message_ref = MessageRef(
        account_name=account.name,
        folder_name="INBOX",
        rfc5322_id=rfc5322_id,
    )
    projected = project_rfc822_message(
        message_ref=message_ref, raw_message=raw, storage_key=storage_key,
    )
    stored = dataclasses.replace(projected, local_status=MessageStatus.ACTIVE)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    index.upsert_message(message=stored)
    return message_ref


def _seed_message_with_attachments(config_path: Path):
    """Seed a real maildir message using the multipart+attachment fixture.

    Returns the ``MessageRef`` whose ``rfc5322_id`` is the Message-ID
    header the fixture carries (``<att1-fixture@example.com>``).  The
    mirror holds the raw bytes; the index has a matching metadata row.
    """
    import dataclasses

    import corpus

    from pony.config import load_config
    from pony.domain import FolderRef, MessageRef, MessageStatus
    from pony.index_store import SqliteIndexRepository
    from pony.message_projection import project_rfc822_message
    from pony.paths import AppPaths
    from pony.storage import MaildirMirrorRepository

    config = load_config(config_path)
    account = next(iter(config.accounts))
    paths = AppPaths.default()
    paths.ensure_runtime_dirs()

    raw = corpus.multipart_mixed_attachment()
    mirror = MaildirMirrorRepository(
        account_name=account.name, root_dir=account.mirror.path,
    )
    folder = FolderRef(account_name=account.name, folder_name="INBOX")
    storage_key = mirror.store_message(folder=folder, raw_message=raw)

    message_ref = MessageRef(
        account_name=account.name,
        folder_name="INBOX",
        rfc5322_id="<att1-fixture@example.com>",
    )
    projected = project_rfc822_message(
        message_ref=message_ref, raw_message=raw, storage_key=storage_key,
    )
    stored = dataclasses.replace(projected, local_status=MessageStatus.ACTIVE)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    index.upsert_message(message=stored)
    return message_ref


def run_cli(*argv: str) -> str:
    """Capture CLI stdout for one invocation."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        main(argv)
    return buffer.getvalue()


def run_cli_ret(*argv: str) -> int:
    """Run the CLI and return its exit code (no stdout capture)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        return main(argv)


def run_cli_capture(*argv: str) -> tuple[str, int]:
    """Run the CLI, returning (stdout, exit_code)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = main(argv)
    return buffer.getvalue(), rc


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
config_version = 2

[[accounts]]
name = "personal"
email_address = "user@example.com"
imap_host = "imap.example.com"
username = "user"
credentials_source = "plaintext"
password = "test-password"

[accounts.smtp]
host = "smtp.example.com"

[accounts.mirror]
path = "mirrors/personal"
format = "maildir"
trash_retention_days = 30
""".strip()
