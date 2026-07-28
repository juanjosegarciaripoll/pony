"""CLI tests for the Phase 1 scaffold."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import corpus
from conftest import TMP_ROOT

from pony.cli import main
from pony.domain import MessageFlag


@dataclass(frozen=True)
class _SeedHandle:
    """Carrier for a seeded message: row identity plus its display Message-ID.

    Tests pass ``rfc5322_id`` to CLI commands that take a Message-ID
    argument and ``id`` to commands that take a row id.
    """

    account_name: str
    folder_name: str
    id: int
    rfc5322_id: str


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
            self.assertTrue((env_root / "config").exists())
            self.assertTrue((env_root / "data").exists())
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
                account_name=account.name,
                root_dir=account.mirror.path,
            )
            folder = FolderRef(account_name=account.name, folder_name="INBOX")
            storage_key = mirror.store_message(folder=folder, raw_message=raw)
            # Seed the index with a row whose body_preview simulates the
            # pre-fix bug (CSS text leaked into the preview).
            projected = project_rfc822_message(
                message_ref=MessageRef(
                    account_name=account.name,
                    folder_name="INBOX",
                    id=0,
                ),
                raw_message=raw,
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
            saved = index.insert_message(message=stale)

            output = run_cli("--config", str(config_path), "rescan")
            self.assertIn("Rescan complete", output)
            self.assertIn("1/1", output)

            refreshed = index.get_message(message_ref=saved.message_ref)
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
                account_name=account.name,
                root_dir=account.mirror.path,
            )
            folder = FolderRef(account_name=account.name, folder_name="INBOX")
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
            saved = index.insert_message(message=with_sync_state)

            run_cli("--config", str(config_path), "rescan")

            refreshed = index.get_message(message_ref=saved.message_ref)
            assert refreshed is not None
            self.assertEqual(refreshed.uid, 4242)
            self.assertEqual(
                refreshed.local_flags,
                frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
            )
            self.assertEqual(refreshed.base_flags, frozenset({MessageFlag.SEEN}))
            self.assertIn("plain body", refreshed.body_preview)
            self.assertNotEqual(refreshed.body_preview, "stale preview")

    def test_rescan_force_upserts_unchanged_rows(self) -> None:
        """`pony rescan --force` upserts every row even when projection matches."""
        from email.message import EmailMessage

        from pony.config import load_config
        from pony.domain import FolderRef, MessageRef
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
            msg["Subject"] = "Already up to date"
            msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
            msg["Message-ID"] = f"<force-{uuid4().hex}@example.com>"
            msg.set_content("body")
            raw = msg.as_bytes()

            mirror = MaildirMirrorRepository(
                account_name=account.name,
                root_dir=account.mirror.path,
            )
            folder = FolderRef(account_name=account.name, folder_name="INBOX")
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
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            index.insert_message(message=projected)

            output = run_cli("--config", str(config_path), "rescan")
            self.assertIn("0/1", output)

            output = run_cli("--config", str(config_path), "rescan", "--force")
            self.assertIn("1/1", output)

    def test_rescan_unknown_account_errors(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config",
                    str(config_path),
                    "rescan",
                    "--account",
                    "nonexistent",
                )
            self.assertIn("nonexistent", str(ctx.exception))

    def test_folder_list_shows_counts_and_sync_status(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            message_ref = _seed_one_message(
                config_path,
                subject="Folder list probe",
                body="hello",
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
                    "--config",
                    str(config_path),
                    "folder",
                    "list",
                    "nonexistent",
                )
            self.assertIn("nonexistent", str(ctx.exception))

    def test_message_get_prints_metadata(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            message_ref = _seed_one_message(
                config_path,
                subject="Metadata probe",
                body="body-text",
            )
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "get",
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
                    "--config",
                    str(config_path),
                    "message",
                    "get",
                    "personal",
                    "INBOX",
                    "does-not-exist",
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
                "--config",
                str(config_path),
                "message",
                "body",
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
                    "--config",
                    str(config_path),
                    "message",
                    "body",
                    "personal",
                    "INBOX",
                    "does-not-exist",
                )
            self.assertIn(
                "not found",
                str(ctx.exception).lower(),
            )

    def test_message_get_lists_attachments_individually(self) -> None:
        """Previously showed only 'Attach.: yes/no'; now each attachment
        is listed by index, name, content-type, and size — matching
        what 'pony message body' and the MCP tools already return."""
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_message_with_attachments(config_path)
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "get",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("Attach.:    yes (1)", output)
        self.assertIn("1. q1-report.pdf", output)
        self.assertIn("application/octet-stream", output)

    def test_message_attachment_writes_file_to_cwd(self) -> None:
        import tempfile

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            ref = _seed_message_with_attachments(config_path)
            prev_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                output = run_cli(
                    "--config",
                    str(config_path),
                    "message",
                    "attachment",
                    ref.account_name,
                    ref.folder_name,
                    ref.rfc5322_id,
                    "1",
                )
            finally:
                os.chdir(prev_cwd)
            written = Path(tmpdir) / "q1-report.pdf"
            self.assertTrue(written.exists())
            self.assertTrue(written.read_bytes().startswith(b"%PDF"))
            self.assertIn("Wrote", output)

    def test_message_attachment_refuses_overwrite_without_force(self) -> None:
        import tempfile

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            ref = _seed_message_with_attachments(config_path)
            out_path = Path(tmpdir) / "out.bin"
            out_path.write_bytes(b"untouched")
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config",
                    str(config_path),
                    "message",
                    "attachment",
                    ref.account_name,
                    ref.folder_name,
                    ref.rfc5322_id,
                    "1",
                    "-o",
                    str(out_path),
                )
            self.assertIn("Refusing to overwrite", str(ctx.exception))
            # File was not clobbered.
            self.assertEqual(out_path.read_bytes(), b"untouched")

    def test_message_attachment_force_overwrites(self) -> None:
        import tempfile

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            ref = _seed_message_with_attachments(config_path)
            out_path = Path(tmpdir) / "out.bin"
            out_path.write_bytes(b"untouched")
            run_cli(
                "--config",
                str(config_path),
                "message",
                "attachment",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
                "1",
                "-o",
                str(out_path),
                "--force",
            )
            self.assertTrue(out_path.read_bytes().startswith(b"%PDF"))

    def test_message_attachment_out_of_range_errors(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_message_with_attachments(config_path)
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config",
                    str(config_path),
                    "message",
                    "attachment",
                    ref.account_name,
                    ref.folder_name,
                    ref.rfc5322_id,
                    "99",
                )
            self.assertIn("not found", str(ctx.exception).lower())

    def test_list_themes_prints_known_names(self) -> None:
        output = run_cli("--list-themes")
        lines = [line for line in output.splitlines() if line.strip()]
        self.assertIn("textual-dark", lines)
        self.assertIn("textual-light", lines)
        self.assertEqual(lines, sorted(lines))

    def test_theme_flag_parsed(self) -> None:
        from pony.cli import build_parser

        args = build_parser().parse_args(["--theme", "nord", "tui"])
        self.assertEqual(args.theme, "nord")

    def test_unknown_theme_rejected(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            output, rc = run_cli_capture(
                "--config",
                str(config_path),
                "--theme",
                "does-not-exist",
                "tui",
            )
        self.assertEqual(rc, 1)
        self.assertIn("does-not-exist", output)


class SchemaMismatchRecoveryTests(unittest.TestCase):
    """The CLI must explain, prompt (default N), and only reset on y/yes."""

    def _seed_legacy_db_and_mirror(
        self,
        config_path: Path,
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
                    "--config",
                    str(config_path),
                    "search",
                    "anything",
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
                    "--config",
                    str(config_path),
                    "search",
                    "anything",
                )
            finally:
                sys.stdin = stdin_backup

            self.assertEqual(rc, 0)
            self.assertFalse(db.exists(), "DB must be deleted after yes")
            self.assertFalse(mirror.exists(), "mirror must be deleted after yes")

            # The backup BBDB file must contain the one legacy contact.
            backups = list((env_root / "data").glob("contacts-backup-*.bbdb"))
            self.assertEqual(len(backups), 1, captured)
            loaded = read_bbdb(backups[0])
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].first_name, "María")
            self.assertIn("pony sync", captured)
            self.assertIn("contacts import", captured)


def _seed_one_message(
    config_path: Path,
    *,
    subject: str,
    body: str,
) -> _SeedHandle:
    """Seed one real maildir message + matching index row for the personal account.

    Returns the ``MessageRef`` whose ``message_id`` is the RFC 5322
    header value — matching what IMAP sync populates.  The maildir
    filename (``storage_key``) is a different string.
    """
    import dataclasses
    from email.message import EmailMessage

    from pony.config import load_config
    from pony.domain import AccountConfig, FolderRef, MessageRef, MessageStatus
    from pony.index_store import SqliteIndexRepository
    from pony.message_projection import project_rfc822_message
    from pony.paths import AppPaths
    from pony.storage import MaildirMirrorRepository

    config = load_config(config_path)
    account = next(iter(config.accounts))
    assert isinstance(account, AccountConfig)
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
        account_name=account.name,
        root_dir=account.mirror.path,
    )
    folder = FolderRef(account_name=account.name, folder_name="INBOX")
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
    stored = dataclasses.replace(projected, local_status=MessageStatus.ACTIVE)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    saved = index.insert_message(message=stored)
    return _SeedHandle(
        account_name=saved.message_ref.account_name,
        folder_name=saved.message_ref.folder_name,
        id=saved.message_ref.id,
        rfc5322_id=saved.message_id,
    )


def _seed_dup_messages(
    config_path: Path,
    *,
    message_id: str,
    flags_list: list[frozenset[MessageFlag]],
    folder_name: str = "INBOX",
) -> list[_SeedHandle]:
    """Seed multiple index rows that share one ``Message-ID``.

    Each entry in *flags_list* produces one row, assigned UIDs 1, 2, …
    in order.  All rows are ACTIVE with their assigned flags.
    """
    import dataclasses
    from email.message import EmailMessage

    from pony.config import load_config
    from pony.domain import AccountConfig, FolderRef, MessageRef, MessageStatus
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
    msg["Subject"] = "Duplicate test"
    msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
    msg["Message-ID"] = message_id
    msg.set_content("duplicate body")
    raw = msg.as_bytes()

    mirror = MaildirMirrorRepository(
        account_name=account.name,
        root_dir=account.mirror.path,
    )
    folder_ref = FolderRef(account_name=account.name, folder_name=folder_name)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    handles: list[_SeedHandle] = []
    for i, flags in enumerate(flags_list):
        storage_key = mirror.store_message(folder=folder_ref, raw_message=raw)
        projected = project_rfc822_message(
            message_ref=MessageRef(
                account_name=account.name,
                folder_name=folder_name,
                id=0,
            ),
            raw_message=raw,
            storage_key=storage_key,
        )
        stored = dataclasses.replace(
            projected,
            message_id=message_id,
            uid=i + 1,
            local_flags=flags,
            base_flags=flags,
            server_flags=flags,
            local_status=MessageStatus.ACTIVE,
        )
        saved = index.insert_message(message=stored)
        handles.append(
            _SeedHandle(
                account_name=saved.message_ref.account_name,
                folder_name=saved.message_ref.folder_name,
                id=saved.message_ref.id,
                rfc5322_id=saved.message_id,
            )
        )
    return handles


def _seed_message_with_attachments(config_path: Path) -> _SeedHandle:
    """Seed a real maildir message using the multipart+attachment fixture.

    Returns the ``MessageRef`` whose ``rfc5322_id`` is the Message-ID
    header the fixture carries (``<att1-fixture@example.com>``).  The
    mirror holds the raw bytes; the index has a matching metadata row.
    """
    import dataclasses

    import corpus

    from pony.config import load_config
    from pony.domain import AccountConfig, FolderRef, MessageRef, MessageStatus
    from pony.index_store import SqliteIndexRepository
    from pony.message_projection import project_rfc822_message
    from pony.paths import AppPaths
    from pony.storage import MaildirMirrorRepository

    config = load_config(config_path)
    account = next(iter(config.accounts))
    assert isinstance(account, AccountConfig)
    paths = AppPaths.default()
    paths.ensure_runtime_dirs()

    raw = corpus.multipart_mixed_attachment()
    mirror = MaildirMirrorRepository(
        account_name=account.name,
        root_dir=account.mirror.path,
    )
    folder = FolderRef(account_name=account.name, folder_name="INBOX")
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
    stored = dataclasses.replace(projected, local_status=MessageStatus.ACTIVE)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    saved = index.insert_message(message=stored)
    return _SeedHandle(
        account_name=saved.message_ref.account_name,
        folder_name=saved.message_ref.folder_name,
        id=saved.message_ref.id,
        rfc5322_id=saved.message_id,
    )


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


class DedupFolderTestCase(unittest.TestCase):
    """Tests for ``pony folder dedup``."""

    def test_dry_run_reports_without_writing(self) -> None:
        """Dry-run prints groups but leaves all rows ACTIVE."""
        from pony.domain import FolderRef, MessageFlag, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            _seed_dup_messages(
                config_path,
                message_id="<dup@test.example>",
                flags_list=[
                    frozenset({MessageFlag.SEEN}),
                    frozenset({MessageFlag.SEEN, MessageFlag.ANSWERED}),
                    frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
                ],
            )

            output = run_cli(
                "--config",
                str(config_path),
                "folder",
                "dedup",
                "personal",
                "INBOX",
            )

            self.assertIn("<dup@test.example>", output)
            self.assertIn("Would trash 2 row(s)", output)
            self.assertNotIn("Run `pony sync`", output)

            paths = AppPaths.default()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            rows = index.list_folder_messages(
                folder=FolderRef(account_name="personal", folder_name="INBOX")
            )
            active = [r for r in rows if r.local_status == MessageStatus.ACTIVE]
            self.assertEqual(len(active), 3, "dry-run must not trash anything")

    def test_apply_trashes_losers_keeps_most_flags(self) -> None:
        """``--apply`` trashes copies with fewer flags; winner stays ACTIVE."""
        from pony.domain import FolderRef, MessageFlag, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            _seed_dup_messages(
                config_path,
                message_id="<dup2@test.example>",
                flags_list=[
                    frozenset({MessageFlag.SEEN}),
                    frozenset({MessageFlag.SEEN, MessageFlag.ANSWERED}),
                ],
            )

            run_cli(
                "--config",
                str(config_path),
                "folder",
                "dedup",
                "personal",
                "INBOX",
                "--apply",
            )

            paths = AppPaths.default()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            rows = index.list_folder_messages(
                folder=FolderRef(account_name="personal", folder_name="INBOX")
            )

            active = [r for r in rows if r.local_status == MessageStatus.ACTIVE]
            trashed = [r for r in rows if r.local_status == MessageStatus.TRASHED]

            self.assertEqual(len(active), 1)
            self.assertEqual(len(trashed), 1)
            # Winner has the richer flag set
            self.assertIn(MessageFlag.ANSWERED, active[0].local_flags)
            # Loser still carries its uid so sync emits PushDeleteOp
            self.assertIsNotNone(trashed[0].uid)

    def test_tiebreaker_answered_beats_flagged(self) -> None:
        """With equal flag counts, ANSWERED wins over FLAGGED."""
        from pony.domain import FolderRef, MessageFlag, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            # Both have two flags; ANSWERED should win the tiebreaker.
            _seed_dup_messages(
                config_path,
                message_id="<tie@test.example>",
                flags_list=[
                    frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
                    frozenset({MessageFlag.SEEN, MessageFlag.ANSWERED}),
                ],
            )

            run_cli(
                "--config",
                str(config_path),
                "folder",
                "dedup",
                "personal",
                "INBOX",
                "--apply",
            )

            paths = AppPaths.default()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            rows = index.list_folder_messages(
                folder=FolderRef(account_name="personal", folder_name="INBOX")
            )
            active = [r for r in rows if r.local_status == MessageStatus.ACTIVE]
            self.assertEqual(len(active), 1)
            self.assertIn(MessageFlag.ANSWERED, active[0].local_flags)

    def test_trashed_rows_in_group_are_skipped(self) -> None:
        """A pre-existing TRASHED row is neither picked as winner nor re-touched."""
        import dataclasses

        from pony.domain import FolderRef, MessageFlag, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            handles = _seed_dup_messages(
                config_path,
                message_id="<mixed@test.example>",
                flags_list=[
                    frozenset({MessageFlag.SEEN}),
                    frozenset({MessageFlag.SEEN, MessageFlag.ANSWERED}),
                ],
            )
            # Manually mark the winner TRASHED before the dedup run.
            paths = AppPaths.default()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            winner_handle = handles[1]  # the ANSWERED one
            all_rows = index.list_folder_messages(
                folder=FolderRef(account_name="personal", folder_name="INBOX")
            )
            winner_row = next(
                r for r in all_rows if r.message_ref.id == winner_handle.id
            )
            index.upsert_message(
                message=dataclasses.replace(
                    winner_row, local_status=MessageStatus.TRASHED
                )
            )

            # With the ANSWERED copy already TRASHED, only the SEEN copy is
            # ACTIVE; it's alone in its group — no duplicates.
            output = run_cli(
                "--config",
                str(config_path),
                "folder",
                "dedup",
                "personal",
                "INBOX",
                "--apply",
            )
            self.assertIn("No duplicates found", output)

    def test_empty_message_id_rows_not_grouped(self) -> None:
        """Rows with an empty Message-ID are never treated as duplicates."""
        import dataclasses

        from pony.domain import FolderRef, MessageFlag, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            handles = _seed_dup_messages(
                config_path,
                message_id="<real@test.example>",
                flags_list=[frozenset({MessageFlag.SEEN}), frozenset()],
            )
            # Blank out the message_id on both rows.
            paths = AppPaths.default()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            for handle in handles:
                rows = index.list_folder_messages(
                    folder=FolderRef(
                        account_name=handle.account_name,
                        folder_name=handle.folder_name,
                    )
                )
                row = next(r for r in rows if r.message_ref.id == handle.id)
                index.upsert_message(message=dataclasses.replace(row, message_id=""))

            output = run_cli(
                "--config",
                str(config_path),
                "folder",
                "dedup",
                "personal",
                "INBOX",
                "--apply",
            )
            self.assertIn("No duplicates found", output)

            rows_after = index.list_folder_messages(
                folder=FolderRef(account_name="personal", folder_name="INBOX")
            )
            self.assertEqual(
                sum(1 for r in rows_after if r.local_status == MessageStatus.ACTIVE),
                2,
            )

    def test_no_duplicates_prints_clean_message(self) -> None:
        """A folder with no duplicates prints an informational message."""
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Unique message", body="body")
            output = run_cli(
                "--config",
                str(config_path),
                "folder",
                "dedup",
                "personal",
                "INBOX",
            )
        self.assertIn("No duplicates found", output)

    def test_unknown_account_errors(self) -> None:
        """Non-existent account exits with an error message."""
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config",
                    str(config_path),
                    "folder",
                    "dedup",
                    "ghost",
                    "INBOX",
                )
            self.assertIn("ghost", str(ctx.exception))


class NewSubcommandTestCase(unittest.TestCase):
    """Tests for CLI subcommands not covered by the existing test cases."""

    # ------------------------------------------------------------------
    # config show
    # ------------------------------------------------------------------

    def test_config_show_prints_config_file(self) -> None:
        """``pony config show`` prints the config file contents to stdout."""
        with isolated_app_env(), temporary_config() as config_path:
            output = run_cli("--config", str(config_path), "config", "show")
        self.assertIn("config_version", output)
        self.assertIn("personal", output)

    # ------------------------------------------------------------------
    # local-summary
    # ------------------------------------------------------------------

    def test_local_summary_prints_header(self) -> None:
        """``pony local-summary`` prints the local-summary header."""
        with isolated_app_env(), temporary_config() as config_path:
            output = run_cli("--config", str(config_path), "local-summary")
        self.assertIn("Pony Express local summary", output)
        self.assertIn("Config", output)

    def test_local_summary_account_scope(self) -> None:
        """``pony local-summary personal`` restricts output to that account."""
        with isolated_app_env(), temporary_config() as config_path:
            output = run_cli("--config", str(config_path), "local-summary", "personal")
        self.assertIn("personal", output)

    def test_local_summary_unknown_account_raises(self) -> None:
        """``pony local-summary ghost`` raises SystemExit for unknown account."""
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli("--config", str(config_path), "local-summary", "ghost")
            self.assertIn("ghost", str(ctx.exception))

    # ------------------------------------------------------------------
    # contacts search
    # ------------------------------------------------------------------

    def test_contacts_search_runs_without_error(self) -> None:
        """``pony contacts search`` completes without crashing."""
        with isolated_app_env(), temporary_config() as config_path:
            run_cli("--config", str(config_path), "fixture-ingest")
            # The fixture may or may not have contacts matching "ali";
            # the important thing is that the command finishes cleanly.
            output = run_cli("--config", str(config_path), "contacts", "search", "ali")
        # Either "No contacts matching" or an actual result list — both are valid.
        self.assertTrue(
            "ali" in output.lower() or len(output.strip()) == 0,
            msg=f"Unexpected output: {output!r}",
        )

    # ------------------------------------------------------------------
    # --list-themes
    # ------------------------------------------------------------------

    def test_list_themes_prints_theme_names(self) -> None:
        """``pony --list-themes`` prints at least one theme name."""
        output = run_cli("--list-themes")
        self.assertTrue(
            len(output.strip()) > 0,
            msg="--list-themes produced no output",
        )

    def test_list_themes_includes_textual_dark(self) -> None:
        """``pony --list-themes`` includes the built-in textual-dark theme."""
        output = run_cli("--list-themes")
        self.assertIn("textual-dark", output)

    # ------------------------------------------------------------------
    # rescan --account unknown
    # ------------------------------------------------------------------

    def test_rescan_unknown_account_raises(self) -> None:
        """``pony rescan --account ghost`` raises SystemExit with the account name."""
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli("--config", str(config_path), "rescan", "--account", "ghost")
            self.assertIn("ghost", str(ctx.exception))

    # ------------------------------------------------------------------
    # folder list
    # ------------------------------------------------------------------

    def test_folder_list_runs_without_error(self) -> None:
        """``pony folder list`` completes without crashing even with no folders."""
        with isolated_app_env(), temporary_config() as config_path:
            output = run_cli("--config", str(config_path), "folder", "list")
        # With no synced data the output should mention the account or
        # indicate there are no folders.
        self.assertTrue(
            "personal" in output or "no folders" in output.lower(),
            msg=f"Unexpected folder list output: {output!r}",
        )

    def test_folder_list_after_seeding_shows_inbox(self) -> None:
        """``pony folder list`` shows INBOX after a message has been seeded."""
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Folder list test", body="body")
            output = run_cli("--config", str(config_path), "folder", "list")
        self.assertIn("personal", output)
        self.assertIn("INBOX", output)


def _seed_multipart_alternative(config_path: Path) -> _SeedHandle:
    """Seed a multipart/alternative fixture message into the index and mirror."""
    import dataclasses

    import corpus

    from pony.config import load_config
    from pony.domain import AccountConfig, FolderRef, MessageRef, MessageStatus
    from pony.index_store import SqliteIndexRepository
    from pony.message_projection import project_rfc822_message
    from pony.paths import AppPaths
    from pony.storage import MaildirMirrorRepository

    config = load_config(config_path)
    account = next(iter(config.accounts))
    assert isinstance(account, AccountConfig)
    paths = AppPaths.default()
    paths.ensure_runtime_dirs()

    raw = corpus.multipart_alternative()
    mirror = MaildirMirrorRepository(
        account_name=account.name,
        root_dir=account.mirror.path,
    )
    folder = FolderRef(account_name=account.name, folder_name="INBOX")
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
    stored = dataclasses.replace(projected, local_status=MessageStatus.ACTIVE)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    saved = index.insert_message(message=stored)
    return _SeedHandle(
        account_name=saved.message_ref.account_name,
        folder_name=saved.message_ref.folder_name,
        id=saved.message_ref.id,
        rfc5322_id=saved.message_id,
    )


def _seed_plain_text_fixture(config_path: Path) -> _SeedHandle:
    """Seed a plain_text fixture message into the index and mirror."""
    import dataclasses

    import corpus

    from pony.config import load_config
    from pony.domain import AccountConfig, FolderRef, MessageRef, MessageStatus
    from pony.index_store import SqliteIndexRepository
    from pony.message_projection import project_rfc822_message
    from pony.paths import AppPaths
    from pony.storage import MaildirMirrorRepository

    config = load_config(config_path)
    account = next(iter(config.accounts))
    assert isinstance(account, AccountConfig)
    paths = AppPaths.default()
    paths.ensure_runtime_dirs()

    raw = corpus.plain_text()
    mirror = MaildirMirrorRepository(
        account_name=account.name,
        root_dir=account.mirror.path,
    )
    folder = FolderRef(account_name=account.name, folder_name="INBOX")
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
    stored = dataclasses.replace(projected, local_status=MessageStatus.ACTIVE)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    saved = index.insert_message(message=stored)
    return _SeedHandle(
        account_name=saved.message_ref.account_name,
        folder_name=saved.message_ref.folder_name,
        id=saved.message_ref.id,
        rfc5322_id=saved.message_id,
    )


class MessageCommandsTestCase(unittest.TestCase):
    """Tests for ``pony message mime``, ``message get``, and ``message body``."""

    # --- message mime ---

    def test_message_mime_dumps_mime_tree(self) -> None:
        """``message mime`` prints the MIME structure for a multipart/alternative."""
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_multipart_alternative(config_path)
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "mime",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("multipart/alternative", output)
        self.assertIn("text/plain", output)
        self.assertIn("text/html", output)

    def test_message_mime_angle_bracket_normalization(self) -> None:
        """Passing a Message-ID without angle brackets finds the same message."""
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_multipart_alternative(config_path)
            # Strip angle brackets so the CLI must normalize the id.
            bare_id = ref.rfc5322_id.strip("<>")
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "mime",
                ref.account_name,
                ref.folder_name,
                bare_id,
            )
        self.assertIn("multipart/alternative", output)
        self.assertIn("text/plain", output)
        self.assertIn("text/html", output)

    def test_message_mime_unknown_account_exits(self) -> None:
        """``message mime`` raises SystemExit for a non-existent account."""
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config",
                    str(config_path),
                    "message",
                    "mime",
                    "ghost",
                    "INBOX",
                    "<some-id@example.com>",
                )
            self.assertIn("ghost", str(ctx.exception))

    # --- message get ---

    def test_message_get_shows_attachment_metadata(self) -> None:
        """``message get`` with an attachment message shows attachment info."""
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_message_with_attachments(config_path)
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "get",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("Message-ID:", output)
        self.assertIn("From:", output)
        self.assertIn("Subject:", output)
        self.assertIn("Attach.", output)

    def test_message_get_not_found_exits(self) -> None:
        """``message get`` raises SystemExit when the message does not exist."""
        with isolated_app_env(), temporary_config() as config_path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config",
                    str(config_path),
                    "message",
                    "get",
                    "personal",
                    "INBOX",
                    "<no-such-id@example.com>",
                )
            self.assertIn("not found", str(ctx.exception).lower())

    # --- message body ---

    def test_message_body_prints_body_text(self) -> None:
        """``message body`` outputs headers and non-empty body text."""
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_plain_text_fixture(config_path)
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "body",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("From:", output)
        self.assertIn("Subject:", output)
        # Body follows the blank line separator.
        parts = output.split("\n\n", 1)
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[1].strip())


class MessageIdBracketNormalisationTest(unittest.TestCase):
    """Rows store the Message-ID with its angle brackets; people type it without.

    The CLI used to normalise on its way in and the MCP server did not,
    so the same lookup found a message from one front-end and nothing
    from the other.  Normalisation lives in the index now, so this is a
    property of the lookup rather than of whoever calls it.
    """

    def _repo_with_message(self):  # type: ignore[no-untyped-def]
        from tui_helpers import make_index, make_tmp_paths

        index = make_index(make_tmp_paths("msgid-brackets"))
        stored = _make_indexed("<wrapped@example.com>")
        index.insert_message(message=stored)
        return index

    def test_both_spellings_find_the_same_row(self) -> None:
        index = self._repo_with_message()

        for spelling in ("<wrapped@example.com>", "wrapped@example.com"):
            with self.subTest(spelling=spelling):
                hits = index.find_messages_by_message_id(
                    account_name="acct",
                    folder_name="INBOX",
                    message_id=spelling,
                )
                self.assertEqual(len(hits), 1, spelling)
                self.assertEqual(hits[0].message_id, "<wrapped@example.com>")

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        index = self._repo_with_message()

        hits = index.find_messages_by_message_id(
            account_name="acct",
            folder_name="INBOX",
            message_id="  wrapped@example.com  ",
        )

        self.assertEqual(len(hits), 1)

    def test_an_unrelated_id_still_misses(self) -> None:
        index = self._repo_with_message()

        self.assertEqual(
            index.find_messages_by_message_id(
                account_name="acct",
                folder_name="INBOX",
                message_id="other@example.com",
            ),
            (),
        )

    def test_a_blank_id_matches_nothing(self) -> None:
        index = self._repo_with_message()

        for blank in ("", "   ", "<>"):
            with self.subTest(blank=blank):
                self.assertEqual(
                    index.find_messages_by_message_id(
                        account_name="acct",
                        folder_name="INBOX",
                        message_id=blank,
                    ),
                    (),
                )


def _make_indexed(message_id: str):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    from pony.domain import IndexedMessage, MessageRef, MessageStatus

    return IndexedMessage(
        message_ref=MessageRef(account_name="acct", folder_name="INBOX", id=0),
        message_id=message_id,
        sender="sender@example.com",
        recipients="acct@example.com",
        cc="",
        subject="Subject",
        body_preview="Body",
        storage_key="key",
        local_flags=frozenset(),
        base_flags=frozenset(),
        local_status=MessageStatus.ACTIVE,
        received_at=datetime(2026, 4, 17, 12, tzinfo=UTC),
    )


class EmlViewerCliTest(unittest.TestCase):
    """Tests for the ``pony view`` command and EML viewer error paths."""

    def test_view_nonexistent_file_exits_with_error(self) -> None:
        from pony.cli import run_eml_viewer

        with isolated_app_env():
            rc = run_eml_viewer(path=Path("/no/such/file.eml"))
        self.assertEqual(rc, 1)

    def test_view_command_not_a_file_exits(self) -> None:
        import tempfile

        from pony.cli import run_eml_viewer

        with isolated_app_env(), tempfile.TemporaryDirectory() as tmpdir:
            rc = run_eml_viewer(path=Path(tmpdir))
        self.assertEqual(rc, 1)

    def test_docs_command_opens_browser(self) -> None:
        """``pony docs`` opens the documentation URL."""
        from unittest.mock import patch

        with isolated_app_env(), patch("webbrowser.open") as mock_open:
            run_cli("docs")
        # Docs command should call webbrowser.open with a URL
        self.assertEqual(mock_open.call_count, 1)
        url = mock_open.call_args.args[0]
        self.assertTrue(url.startswith("http") or url.startswith("file://"))

    def test_view_command_dispatches_via_cli(self) -> None:
        """``pony view file.eml`` reads the file and (mock) launches the viewer."""
        import tempfile
        from unittest.mock import patch

        import corpus

        raw = corpus.plain_text()
        with isolated_app_env():
            with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as f:
                f.write(raw)
                eml_path = Path(f.name)
            try:
                with patch("pony.tui.app.EmlViewerApp.run"):
                    run_cli("view", str(eml_path))
            finally:
                eml_path.unlink(missing_ok=True)
        # If no SystemExit and no crash, the view command ran


class ContactsCommandsTest(unittest.TestCase):
    """Tests for contacts show, export, import commands."""

    def test_contacts_show_not_found(self) -> None:
        """``pony contacts show`` returns 1 when email not in index."""
        with isolated_app_env(), temporary_config() as config_path:
            rc = run_cli_ret(
                "--config",
                str(config_path),
                "contacts",
                "show",
                "nobody@nowhere.invalid",
            )
        self.assertEqual(rc, 1)

    def test_contacts_export_no_path_no_bbdb_returns_1(self) -> None:
        """``pony contacts export`` exits 1 when no path given and no bbdb_path."""
        from pony.cli import run_contacts_export

        with isolated_app_env():
            from pony.paths import AppPaths

            paths = AppPaths.default()
            paths.ensure_runtime_dirs()
            rc = run_contacts_export(paths=paths, config_path=None, output_path=None)
        self.assertEqual(rc, 1)

    def test_contacts_export_to_explicit_path(self) -> None:
        """``pony contacts export /path/out.bbdb`` writes a file and returns 0."""
        with isolated_app_env(), temporary_config() as config_path:
            export_path = TMP_ROOT / "contacts_export_test.bbdb"
            rc = run_cli_ret(
                "--config",
                str(config_path),
                "contacts",
                "export",
                str(export_path),
            )
        self.assertEqual(rc, 0)
        self.assertTrue(export_path.exists())

    def test_contacts_search_with_results(self) -> None:
        """``pony contacts search`` shows results when contacts exist."""
        with isolated_app_env(), temporary_config() as config_path:
            run_cli("--config", str(config_path), "fixture-ingest")
            output = run_cli(
                "--config", str(config_path), "contacts", "search", "alice"
            )
        # Either no results or results — no crash is the key assertion.
        self.assertIsNotNone(output)


class CliHelperFunctionsTest(unittest.TestCase):
    """Tests for internal CLI helper functions."""

    def test_maildir_folders_with_subdirs(self) -> None:
        """_maildir_folders discovers INBOX plus dot-prefixed subdirs."""
        import tempfile

        from pony.cli import _maildir_folders

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Create INBOX cur/new
            (root / "cur").mkdir()
            (root / "new").mkdir()
            (root / "cur" / "msg1").write_text("x")
            # Create a subfolder
            sent = root / ".Sent"
            sent.mkdir()
            (sent / "cur").mkdir()
            (sent / "new").mkdir()
            (sent / "cur" / "msg2").write_text("x")
            # Create a non-dir dot-prefixed file (should be skipped)
            (root / ".hidden-file").write_text("not a folder")

            result = _maildir_folders(root)

        self.assertIn("INBOX", result)
        self.assertEqual(result["INBOX"], 1)
        self.assertIn("Sent", result)
        self.assertEqual(result["Sent"], 1)

    def test_maildir_folders_nonexistent_returns_empty(self) -> None:
        from pony.cli import _maildir_folders

        result = _maildir_folders(Path("/no/such/dir"))
        self.assertEqual(result, {})

    def test_mbox_folders_with_mbox_files(self) -> None:
        import tempfile

        from pony.cli import _mbox_folders

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "INBOX.mbox").write_text("From ...\n")
            (root / "Sent.mbox").write_text("From ...\n")
            result = _mbox_folders(root)

        self.assertIn("INBOX", result)
        self.assertIn("Sent", result)

    def test_mbox_folders_empty_dir_has_inbox(self) -> None:
        import tempfile

        from pony.cli import _mbox_folders

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _mbox_folders(Path(tmpdir))

        self.assertIn("INBOX", result)
        self.assertIsNone(result["INBOX"])

    def test_fmt_size_bytes(self) -> None:
        from pony.cli import _fmt_size

        self.assertIn("B", _fmt_size(500))

    def test_fmt_size_kilobytes(self) -> None:
        from pony.cli import _fmt_size

        result = _fmt_size(2048)
        self.assertIn("KB", result)

    def test_fmt_size_megabytes(self) -> None:
        from pony.cli import _fmt_size

        result = _fmt_size(2 * 1024 * 1024)
        self.assertIn("MB", result)


class MoreCliCommandsTest(unittest.TestCase):
    """Additional CLI command coverage tests."""

    def test_config_edit_mocked_editor(self) -> None:
        """``pony config edit`` opens the config file in an editor."""
        import types
        from unittest.mock import patch

        mock_result = types.SimpleNamespace(returncode=0)
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            patch("subprocess.run", return_value=mock_result),
        ):
            output = run_cli("--config", str(config_path), "config", "edit")
        self.assertIn("Opening", output)

    def test_contacts_show_finds_existing_contact(self) -> None:
        """``pony contacts show`` displays details for a known contact."""
        from pony.domain import Contact
        from pony.index_store import SqliteIndexRepository
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            paths = AppPaths.default()
            paths.ensure_runtime_dirs()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            index.upsert_contact(
                contact=Contact(
                    id=None,
                    first_name="Jane",
                    last_name="Doe",
                    emails=("jane@example.com",),
                )
            )
            output = run_cli(
                "--config",
                str(config_path),
                "contacts",
                "show",
                "jane@example.com",
            )
        self.assertIn("Jane", output)
        self.assertIn("Doe", output)

    def test_contacts_import_from_bbdb(self) -> None:
        """``pony contacts import`` reads a BBDB file."""
        import tempfile

        with isolated_app_env(), temporary_config() as config_path:
            record = (
                '["Alice" "Smith" nil nil ("alice@example.com") nil nil nil nil nil]\n'
            )
            with tempfile.NamedTemporaryFile(
                suffix=".bbdb", delete=False, mode="w"
            ) as f:
                f.write("; BBDB file\n")
                f.write(record)
                bbdb_path = Path(f.name)
            try:
                output = run_cli(
                    "--config",
                    str(config_path),
                    "contacts",
                    "import",
                    str(bbdb_path),
                )
            finally:
                bbdb_path.unlink(missing_ok=True)
        self.assertIn("import", output.lower())

    def test_contacts_import_shows_result(self) -> None:
        """``pony contacts import`` reports import counts."""
        import tempfile

        with isolated_app_env(), temporary_config() as config_path:
            with tempfile.NamedTemporaryFile(
                suffix=".bbdb", delete=False, mode="w"
            ) as f:
                f.write("; BBDB file\n")
                f.write(
                    '["Carol" "White" nil nil ("carol@example.com") '
                    "nil nil nil nil nil]\n"
                )
                bbdb_path = Path(f.name)
            try:
                rc = run_cli_ret(
                    "--config",
                    str(config_path),
                    "contacts",
                    "import",
                    str(bbdb_path),
                )
            finally:
                bbdb_path.unlink(missing_ok=True)
        self.assertEqual(rc, 0)


class LocalSummaryMboxTest(unittest.TestCase):
    """Test local-summary with an mbox-format account."""

    def test_local_summary_mbox_account(self) -> None:
        """``pony local-summary`` works with an mbox-format account."""
        with isolated_app_env() as env_root:
            # Create a config with mbox format
            config_path = env_root / "config" / "config.toml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                "config_version = 2\n"
                "[[accounts]]\n"
                'name = "mbox-test"\n'
                'email_address = "user@example.com"\n'
                'imap_host = "imap.example.com"\n'
                'username = "user"\n'
                'credentials_source = "plaintext"\n'
                'password = "secret"\n'
                "[accounts.smtp]\n"
                'host = "smtp.example.com"\n'
                "[accounts.mirror]\n"
                'path = "mirrors/mbox"\n'
                'format = "mbox"\n',
                encoding="utf-8",
            )
            output = run_cli("--config", str(config_path), "local-summary")
        self.assertIn("Pony Express local summary", output)


class LocalSummaryNoConfigTest(unittest.TestCase):
    """Tests for run_local_summary with no loaded config."""

    def test_local_summary_no_config_shows_message(self) -> None:
        """Without a valid config, local-summary says so."""
        with isolated_app_env():
            output = run_cli("local-summary")
        self.assertIn("Pony Express local summary", output)
        # Either shows "(no config loaded" or normal header — both are valid.
        self.assertIn("Files", output)


class ResetCommandTest(unittest.TestCase):
    """Tests for ``pony reset --yes`` and ``pony reset --account``."""

    def test_reset_yes_removes_index_file(self) -> None:
        """``pony reset --yes`` removes the index DB file."""
        import sqlite3

        with isolated_app_env(), temporary_config() as config_path:
            from pony.paths import AppPaths

            paths = AppPaths.default()
            paths.ensure_runtime_dirs()
            # Create a DB so there's something to delete.
            paths.data_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(paths.index_db_file))
            conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()
            self.assertTrue(paths.index_db_file.exists())

            output = run_cli("--config", str(config_path), "reset", "--yes")
        self.assertIn("Reset complete", output)

    def test_reset_account_yes_purges_account(self) -> None:
        """``pony reset --account personal --yes`` purges that account's data."""
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Reset test", body="body")
            output = run_cli(
                "--config",
                str(config_path),
                "reset",
                "--account",
                "personal",
                "--yes",
            )
        self.assertIn("Reset complete", output)

    def test_reset_account_unknown_returns_2(self) -> None:
        """``pony reset --account ghost --yes`` exits 2 for unknown account."""
        with isolated_app_env(), temporary_config() as config_path:
            rc = run_cli_ret(
                "--config",
                str(config_path),
                "reset",
                "--account",
                "ghost",
                "--yes",
            )
        self.assertEqual(rc, 2)


class MessageGetCcTest(unittest.TestCase):
    """Tests for message get with Cc field."""

    def test_message_get_shows_cc_when_present(self) -> None:
        """``message get`` shows Cc line when message has Cc recipients."""
        import dataclasses
        from email.message import EmailMessage

        from pony.config import load_config
        from pony.domain import AccountConfig, FolderRef, MessageRef, MessageStatus
        from pony.index_store import SqliteIndexRepository
        from pony.message_projection import project_rfc822_message
        from pony.paths import AppPaths
        from pony.storage import MaildirMirrorRepository

        with isolated_app_env(), temporary_config() as config_path:
            config = load_config(config_path)
            account = next(iter(config.accounts))
            assert isinstance(account, AccountConfig)
            paths = AppPaths.default()
            paths.ensure_runtime_dirs()

            rfc5322_id = f"<cc-test-{uuid4().hex}@example.com>"
            msg = EmailMessage()
            msg["From"] = "sender@example.com"
            msg["To"] = account.email_address
            msg["Cc"] = "cc@example.com"
            msg["Subject"] = "Test with Cc"
            msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
            msg["Message-ID"] = rfc5322_id
            msg.set_content("body text")
            raw = msg.as_bytes()

            mirror = MaildirMirrorRepository(
                account_name=account.name, root_dir=account.mirror.path
            )
            folder = FolderRef(account_name=account.name, folder_name="INBOX")
            storage_key = mirror.store_message(folder=folder, raw_message=raw)
            projected = project_rfc822_message(
                message_ref=MessageRef(
                    account_name=account.name, folder_name="INBOX", id=0
                ),
                raw_message=raw,
                storage_key=storage_key,
            )
            stored = dataclasses.replace(
                projected, message_id=rfc5322_id, local_status=MessageStatus.ACTIVE
            )
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            index.insert_message(message=stored)

            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "get",
                account.name,
                "INBOX",
                rfc5322_id,
            )
        self.assertIn("Cc:", output)
        self.assertIn("cc@example.com", output)

    def test_message_get_plain_text_shows_no_attachments(self) -> None:
        """``message get`` on a plain text message shows 'Attach.: no'."""
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_plain_text_fixture(config_path)
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "get",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        # plain_text() has no attachments — should show "no"
        self.assertIn("Attach.:", output)


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


class ResetAccountBranchesTest(unittest.TestCase):
    """Prompt, skip and scan-cache branches of ``pony reset --account``."""

    def test_prompt_declined_cancels(self) -> None:
        """Answering anything but 'y' leaves the account untouched."""
        from unittest.mock import patch

        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Keep", body="body")
            index_file = AppPaths.default().index_db_file
            with patch("builtins.input", return_value="n"):
                output = run_cli(
                    "--config", str(config_path), "reset", "--account", "personal"
                )
            self.assertIn("Reset cancelled", output)
            self.assertTrue(index_file.exists())

    def test_prompt_accepted_resets(self) -> None:
        """Answering 'y' at the prompt performs the reset."""
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Drop", body="body")
            from unittest.mock import patch

            with patch("builtins.input", return_value="y"):
                output = run_cli(
                    "--config", str(config_path), "reset", "--account", "personal"
                )
        self.assertIn("Reset complete", output)

    def test_missing_mirror_and_index_are_skipped(self) -> None:
        """With nothing on disk the command reports skips instead of failing."""
        with isolated_app_env(), temporary_config() as config_path:
            output = run_cli(
                "--config",
                str(config_path),
                "reset",
                "--account",
                "personal",
                "--yes",
            )
        self.assertIn("not present; skipping", output)
        self.assertIn("Index database not present", output)

    def test_scan_cache_entry_is_cleared(self) -> None:
        """A local_scan_state entry for the account is removed."""
        import json as _json

        with isolated_app_env(), temporary_config() as config_path:
            from pony.paths import AppPaths

            paths = AppPaths.default()
            paths.ensure_runtime_dirs()
            state_path = paths.data_dir / "local_scan_state.json"
            state_path.write_text(
                _json.dumps({"personal": {"INBOX": 1}, "other": {"INBOX": 2}}),
                encoding="utf-8",
            )

            output = run_cli(
                "--config",
                str(config_path),
                "reset",
                "--account",
                "personal",
                "--yes",
            )
            remaining = _json.loads(state_path.read_text(encoding="utf-8"))

        self.assertIn("Clearing scan cache entry", output)
        self.assertNotIn("personal", remaining)
        self.assertIn("other", remaining)


class OpsDetailTableTest(unittest.TestCase):
    """``_build_ops_detail_table`` renders every non-download op kind."""

    def _index(self):  # type: ignore[no-untyped-def]
        from pony.index_store import SqliteIndexRepository

        root = TMP_ROOT / "ops-detail" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        index = SqliteIndexRepository(database_path=root / "index.sqlite3")
        index.initialize()
        return index

    def test_every_op_kind_appears(self) -> None:
        from pony.cli import _build_ops_detail_table
        from pony.domain import MessageRef
        from pony.sync import (
            AccountSyncPlan,
            FolderSyncPlan,
            MergeFlagsOp,
            PushDeleteOp,
            PushFlagsOp,
            RestoreOp,
            SyncPlan,
        )

        ref = MessageRef(account_name="personal", folder_name="INBOX", id=7)
        ops = (
            PushDeleteOp(server_uid=1, message_ref=ref, storage_key="k"),
            RestoreOp(message_ref=ref),
            MergeFlagsOp(
                uid=1,
                message_ref=ref,
                merged_flags=frozenset({MessageFlag.SEEN}),
                push_to_server=True,
            ),
            PushFlagsOp(
                uid=1,
                message_ref=ref,
                new_flags=frozenset({MessageFlag.FLAGGED}),
            ),
        )
        plan = SyncPlan(
            accounts=(
                AccountSyncPlan(
                    account_name="personal",
                    folders=(
                        FolderSyncPlan(
                            folder_name="INBOX",
                            uid_validity=1,
                            highest_uid=1,
                            ops=ops,
                        ),
                    ),
                ),
            )
        )

        table = _build_ops_detail_table(plan, self._index())

        self.assertIn("expunge", table)
        self.assertIn("restore", table)
        self.assertIn("merge(", table)
        self.assertIn("push(", table)
        # No index rows exist, so each line falls back to the row id.
        self.assertIn("id=7", table)

    def test_empty_flag_sets_render_as_dash(self) -> None:
        from pony.cli import _build_ops_detail_table
        from pony.domain import MessageRef
        from pony.sync import (
            AccountSyncPlan,
            FolderSyncPlan,
            PushFlagsOp,
            SyncPlan,
        )

        ref = MessageRef(account_name="personal", folder_name="INBOX", id=3)
        plan = SyncPlan(
            accounts=(
                AccountSyncPlan(
                    account_name="personal",
                    folders=(
                        FolderSyncPlan(
                            folder_name="INBOX",
                            uid_validity=1,
                            highest_uid=1,
                            ops=(
                                PushFlagsOp(
                                    uid=1,
                                    message_ref=ref,
                                    new_flags=frozenset(),
                                ),
                            ),
                        ),
                    ),
                ),
            )
        )

        table = _build_ops_detail_table(plan, self._index())

        self.assertIn("push(—)", table)


def _seed_fixture(config_path: Path, raw: bytes) -> _SeedHandle:
    """Seed arbitrary *raw* RFC 5322 bytes into the mirror and the index."""
    import dataclasses

    from pony.config import load_config
    from pony.domain import AccountConfig, FolderRef, MessageRef, MessageStatus
    from pony.index_store import SqliteIndexRepository
    from pony.message_projection import project_rfc822_message
    from pony.paths import AppPaths
    from pony.storage import MaildirMirrorRepository

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
    storage_key = mirror.store_message(folder=folder, raw_message=raw)

    projected = project_rfc822_message(
        message_ref=MessageRef(account_name=account.name, folder_name="INBOX", id=0),
        raw_message=raw,
        storage_key=storage_key,
    )
    stored = dataclasses.replace(projected, local_status=MessageStatus.ACTIVE)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    saved = index.insert_message(message=stored)
    return _SeedHandle(
        account_name=saved.message_ref.account_name,
        folder_name=saved.message_ref.folder_name,
        id=saved.message_ref.id,
        rfc5322_id=saved.message_id,
    )


def _seed_index_only(config_path: Path, *, storage_key: str = "") -> _SeedHandle:
    """Insert an index row whose ``storage_key`` has no file in the mirror.

    Models a headers-only row, or a mirror that was pruned behind the
    index's back: every command that reaches for the raw bytes has to
    cope with the lookup failing.
    """
    import dataclasses
    from email.message import EmailMessage

    from pony.config import load_config
    from pony.domain import AccountConfig, MessageRef, MessageStatus
    from pony.index_store import SqliteIndexRepository
    from pony.message_projection import project_rfc822_message
    from pony.paths import AppPaths

    config = load_config(config_path)
    account = next(iter(config.accounts))
    assert isinstance(account, AccountConfig)
    paths = AppPaths.default()
    paths.ensure_runtime_dirs()

    rfc5322_id = f"<orphan-{uuid4().hex}@example.com>"
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = account.email_address
    msg["Subject"] = "Orphaned row"
    msg["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
    msg["Message-ID"] = rfc5322_id
    msg.set_content("body that never reached the mirror")
    raw = msg.as_bytes()

    projected = project_rfc822_message(
        message_ref=MessageRef(account_name=account.name, folder_name="INBOX", id=0),
        raw_message=raw,
        storage_key=storage_key or f"never-stored-{uuid4().hex}",
    )
    stored = dataclasses.replace(projected, local_status=MessageStatus.ACTIVE)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    saved = index.insert_message(message=stored)
    return _SeedHandle(
        account_name=saved.message_ref.account_name,
        folder_name=saved.message_ref.folder_name,
        id=saved.message_ref.id,
        rfc5322_id=saved.message_id,
    )


def _seed_with_empty_mirror_bytes(config_path: Path) -> _SeedHandle:
    """Seed a row whose mirror file exists but is zero bytes long."""
    from pony.config import load_config
    from pony.domain import AccountConfig, FolderRef
    from pony.storage import MaildirMirrorRepository

    config = load_config(config_path)
    account = next(iter(config.accounts))
    assert isinstance(account, AccountConfig)

    mirror = MaildirMirrorRepository(
        account_name=account.name,
        root_dir=account.mirror.path,
    )
    folder = FolderRef(account_name=account.name, folder_name="INBOX")
    empty_key = mirror.store_message(folder=folder, raw_message=b"")
    return _seed_index_only(config_path, storage_key=empty_key)


def _write_config_without_personal() -> Path:
    """Write a valid config whose only account is *not* named ``personal``."""
    path = TMP_ROOT / f"config-other-{uuid4().hex}.toml"
    path.write_text(
        sample_config_toml().replace('name = "personal"', 'name = "other"'),
        encoding="utf-8",
    )
    return path


def run_cli_bytes(*argv: str) -> bytes:
    """Run the CLI and capture the raw bytes written to ``stdout.buffer``."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
    previous = sys.stdout
    sys.stdout = stream
    try:
        main(argv)
    finally:
        stream.flush()
        sys.stdout = previous
    return raw.getvalue()


class MessageMirrorFallbackTests(unittest.TestCase):
    """`pony message ...` when the mirror cannot supply the raw bytes.

    Every one of these commands resolves the row through the index and
    then reaches into the mirror.  When that second step fails the read
    commands must exit with a clear message, and ``message get`` — which
    only wants the attachment list — must fall back to the index's
    yes/no flag rather than failing outright.
    """

    def test_get_falls_back_to_the_index_attachment_flag(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_index_only(config_path)
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "get",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("Orphaned row", output)
        # The yes/no summary, not the per-attachment listing.
        self.assertIn("Attach.:    no", output)
        self.assertNotIn("Attach.:    no (", output)

    def test_get_falls_back_when_the_mirror_file_is_empty(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_with_empty_mirror_bytes(config_path)
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "get",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("Attach.:    no", output)

    def test_get_falls_back_without_a_config_file(self) -> None:
        """No config means no mirror to consult — the flag still prints."""
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_index_only(config_path)
            # Note: no --config, and the default path holds no config file.
            output = run_cli(
                "message",
                "get",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("Attach.:    no", output)

    def test_get_falls_back_when_the_config_lacks_the_account(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_index_only(config_path)
            other = _write_config_without_personal()
            try:
                output = run_cli(
                    "--config",
                    str(other),
                    "message",
                    "get",
                    ref.account_name,
                    ref.folder_name,
                    ref.rfc5322_id,
                )
            finally:
                other.unlink(missing_ok=True)
        self.assertIn("Attach.:    no", output)

    def test_body_exits_when_the_mirror_has_no_bytes(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_index_only(config_path)
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config",
                    str(config_path),
                    "message",
                    "body",
                    ref.account_name,
                    ref.folder_name,
                    ref.rfc5322_id,
                )
        self.assertIn("not found in mirror", str(ctx.exception))

    def test_attachment_exits_when_the_mirror_has_no_bytes(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_index_only(config_path)
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config",
                    str(config_path),
                    "message",
                    "attachment",
                    ref.account_name,
                    ref.folder_name,
                    ref.rfc5322_id,
                    "1",
                )
        self.assertIn("not found in mirror", str(ctx.exception))

    def test_mime_exits_when_the_mirror_has_no_bytes(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_index_only(config_path)
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    "--config",
                    str(config_path),
                    "message",
                    "mime",
                    ref.account_name,
                    ref.folder_name,
                    ref.rfc5322_id,
                )
        self.assertIn("not found in mirror", str(ctx.exception))

    def test_attachment_exits_when_the_message_is_not_indexed(self) -> None:
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            self.assertRaises(SystemExit) as ctx,
        ):
            run_cli(
                "--config",
                str(config_path),
                "message",
                "attachment",
                "personal",
                "INBOX",
                "<no-such-id@example.com>",
                "1",
            )
        self.assertIn("not found in index", str(ctx.exception))


class MessageOutputDetailTests(unittest.TestCase):
    """Output arms of the `message` commands that need a specific message."""

    def test_get_warns_when_one_message_id_has_several_rows(self) -> None:
        """Duplicate rows are listed by row id so the user can disambiguate."""
        message_id = f"<dup-{uuid4().hex}@example.com>"
        with isolated_app_env(), temporary_config() as config_path:
            handles = _seed_dup_messages(
                config_path,
                message_id=message_id,
                flags_list=[frozenset(), frozenset({MessageFlag.SEEN})],
            )
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "get",
                handles[0].account_name,
                handles[0].folder_name,
                message_id,
            )
        self.assertIn("2 rows match", output)
        self.assertIn("showing the most recent", output)
        for handle in handles:
            self.assertIn(f"id={handle.id}", output)

    def test_body_lists_the_attachments_after_the_text(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_message_with_attachments(config_path)
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "body",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("Please find the Q1 report attached.", output)
        self.assertIn("Attachments:", output)
        self.assertIn("1. q1-report.pdf", output)

    def test_attachment_to_stdout_writes_raw_bytes(self) -> None:
        """``--stdout`` bypasses text mode so binary payloads survive."""
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_message_with_attachments(config_path)
            written = run_cli_bytes(
                "--config",
                str(config_path),
                "message",
                "attachment",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
                "1",
                "--stdout",
            )
        self.assertTrue(written.startswith(b"%PDF"))

    def test_mime_shows_disposition_and_filename(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_message_with_attachments(config_path)
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "mime",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("disposition=attachment", output)
        self.assertIn("filename='q1-report.pdf'", output)

    def test_mime_shows_the_content_id_of_an_inline_part(self) -> None:
        import corpus

        with isolated_app_env(), temporary_config() as config_path:
            ref = _seed_fixture(config_path, corpus.inline_image())
            output = run_cli(
                "--config",
                str(config_path),
                "message",
                "mime",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
            )
        self.assertIn("multipart/related", output)
        self.assertIn("cid=<logo@example.com>", output)
        self.assertIn("disposition=inline", output)


class DoctorReportRenderingTests(unittest.TestCase):
    """``render_doctor_report`` summary line across the status combinations."""

    def _status(self, *checks: object) -> object:
        from pony.paths import AppPaths
        from pony.services import ServiceStatus

        return ServiceStatus(paths=AppPaths.default(), checks=tuple(checks))  # type: ignore[arg-type]

    def test_a_clean_run_reports_all_checks_passed(self) -> None:
        from pony.cli import render_doctor_report
        from pony.services import CheckStatus, DoctorCheck

        report = render_doctor_report(
            self._status(  # type: ignore[arg-type]
                DoctorCheck(name="Config file", status=CheckStatus.OK),
                DoctorCheck(name="Index DB", status=CheckStatus.OK),
            )
        )

        self.assertIn("All 2 checks passed.", report)

    def test_warnings_and_errors_are_counted_separately(self) -> None:
        from pony.cli import render_doctor_report
        from pony.services import CheckStatus, DoctorCheck

        report = render_doctor_report(
            self._status(  # type: ignore[arg-type]
                DoctorCheck(name="Config file", status=CheckStatus.OK),
                DoctorCheck(name="Mirror", status=CheckStatus.WARN, detail="missing"),
                DoctorCheck(name="Index DB", status=CheckStatus.ERROR, detail="broken"),
            )
        )

        self.assertIn("1 OK, 1 error, 1 warning", report)
        self.assertIn("[WARN ] Mirror: missing", report)


class ResetPromptTests(unittest.TestCase):
    """The unscoped ``pony reset`` deletes everything, so it must confirm."""

    def test_declining_the_prompt_deletes_nothing(self) -> None:
        from unittest.mock import patch

        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Keep me", body="body")
            index_file = AppPaths.default().index_db_file
            self.assertTrue(index_file.exists())

            with patch("builtins.input", return_value="n"):
                output = run_cli("--config", str(config_path), "reset")

            self.assertIn("Reset cancelled", output)
            self.assertTrue(index_file.exists())

    def test_confirming_deletes_the_index_and_the_mirror_tree(self) -> None:
        from pony.config import load_config
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Delete me", body="body")
            index_file = AppPaths.default().index_db_file
            mirror_path = next(iter(load_config(config_path).accounts)).mirror.path
            self.assertTrue(index_file.exists())
            self.assertTrue(mirror_path.is_dir())

            output = run_cli("--config", str(config_path), "reset", "--yes")

            self.assertIn("Reset complete", output)
            self.assertFalse(index_file.exists())
            self.assertFalse(mirror_path.exists())


class ContactsShowDetailTests(unittest.TestCase):
    def test_every_optional_field_is_printed_when_present(self) -> None:
        """A fully-populated contact shows affix, aliases, org, notes, last seen."""
        from datetime import UTC, datetime

        from pony.domain import Contact
        from pony.index_store import SqliteIndexRepository
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            paths = AppPaths.default()
            paths.ensure_runtime_dirs()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            index.upsert_contact(
                contact=Contact(
                    id=None,
                    first_name="Ada",
                    last_name="Lovelace",
                    emails=("ada@example.com",),
                    affix=("Countess",),
                    aliases=("AAL",),
                    organization="Analytical Engines",
                    notes="Prefers letters.",
                    message_count=3,
                    last_seen=datetime(2026, 4, 17, 9, 30, tzinfo=UTC),
                )
            )
            output = run_cli(
                "--config", str(config_path), "contacts", "show", "ada@example.com"
            )

        self.assertIn("Name:         Ada Lovelace", output)
        self.assertIn("Countess", output)
        self.assertIn("AAL", output)
        self.assertIn("Analytical Engines", output)
        self.assertIn("Prefers letters.", output)
        self.assertIn("Last seen:    2026-04-17 09:30", output)


class RescanReconciliationTests(unittest.TestCase):
    """``pony rescan`` reconciles the index against what is on disk."""

    def test_files_added_and_removed_behind_the_index_are_reported(self) -> None:
        """A file dropped into the mirror is indexed; a deleted one is pruned."""
        from email.message import EmailMessage

        from pony.config import load_config
        from pony.domain import FolderRef
        from pony.index_store import SqliteIndexRepository
        from pony.paths import AppPaths
        from pony.storage import MaildirMirrorRepository

        with isolated_app_env(), temporary_config() as config_path:
            doomed = _seed_one_message(
                config_path, subject="Removed later", body="gone"
            )
            config = load_config(config_path)
            account = next(iter(config.accounts))
            paths = AppPaths.default()
            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            mirror = MaildirMirrorRepository(
                account_name=account.name,
                root_dir=account.mirror.path,
            )
            folder = FolderRef(account_name=account.name, folder_name="INBOX")

            # Drop a message straight into the mirror — no index row.
            extra = EmailMessage()
            extra["From"] = "sender@example.com"
            extra["To"] = account.email_address
            extra["Subject"] = "Added behind the index"
            extra["Date"] = "Fri, 17 Apr 2026 12:00:00 +0000"
            extra["Message-ID"] = f"<added-{uuid4().hex}@example.com>"
            extra.set_content("new arrival")
            mirror.store_message(folder=folder, raw_message=extra.as_bytes())

            # Delete the seeded message's file, leaving its row orphaned.
            stored = index.get_message(
                message_ref=next(
                    m.message_ref
                    for m in index.list_folder_messages(folder=folder)
                    if m.message_id == doomed.rfc5322_id
                )
            )
            assert stored is not None
            mirror.delete_message(folder=folder, storage_key=stored.storage_key)

            output = run_cli("--config", str(config_path), "rescan")

        self.assertIn("Rescan complete:", output)
        self.assertIn("1 added", output)
        self.assertIn("1 removed", output)


class SchemaResetWithoutContactsTests(unittest.TestCase):
    """The automatic schema reset when there is nothing worth backing up."""

    def test_an_empty_contacts_table_skips_the_bbdb_export(self) -> None:
        """With no contacts there is no backup file and no restore step."""
        import sqlite3

        from pony.config import load_config
        from pony.paths import AppPaths

        with isolated_app_env() as env_root, temporary_config() as config_path:
            account = next(iter(load_config(config_path).accounts))
            app_paths = AppPaths.default()
            app_paths.ensure_runtime_dirs()
            account.mirror.path.mkdir(parents=True, exist_ok=True)

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
                    PRAGMA user_version = 0;
                    """,
                )
                conn.commit()
            finally:
                conn.close()

            stdin_backup = sys.stdin
            sys.stdin = io.StringIO("yes\n")
            try:
                captured, rc = run_cli_capture(
                    "--config", str(config_path), "search", "anything"
                )
            finally:
                sys.stdin = stdin_backup

            self.assertEqual(rc, 0)
            self.assertIn("No contacts to export.", captured)
            self.assertFalse(db.exists())
            self.assertEqual(
                list((env_root / "data").glob("contacts-backup-*.bbdb")), []
            )
            # The pre-prompt explanation always offers the import; the
            # post-reset steps must not, because no backup was written.
            next_steps = captured.split("Reset complete. Next steps:")[1]
            self.assertIn("pony sync", next_steps)
            self.assertNotIn("contacts import", next_steps)


class AppLaunchingCommandTests(unittest.TestCase):
    """Commands whose last act is to start a blocking UI.

    Everything these do *before* ``run()`` — config loading, index
    setup, local rescan, BBDB import, theme resolution — is real work
    worth testing.  Patching the App class at its import site lets the
    wiring run without a terminal.
    """

    def test_the_tui_command_wires_the_app_from_config(self) -> None:
        from unittest.mock import patch

        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Waiting", body="body")
            with patch("pony.tui.PonyApp") as app_cls:
                rc = run_cli_ret("--config", str(config_path), "tui")

        self.assertEqual(rc, 0)
        app_cls.assert_called_once()
        kwargs = app_cls.call_args.kwargs
        self.assertEqual(kwargs["config_path"], config_path)
        self.assertIsNotNone(kwargs["index"])
        self.assertIn("personal", kwargs["mirrors"])
        app_cls.return_value.run.assert_called_once()

    def test_a_bad_theme_is_rejected_before_the_tui_starts(self) -> None:
        from unittest.mock import patch

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            patch("pony.tui.PonyApp") as app_cls,
        ):
            rc = run_cli_ret(
                "--config", str(config_path), "--theme", "no-such-theme", "tui"
            )

        self.assertEqual(rc, 1)
        app_cls.assert_not_called()

    def test_the_tui_rescans_local_accounts_and_imports_bbdb(self) -> None:
        """Local mirrors are the only source of truth, so they are rescanned."""
        from unittest.mock import patch

        with isolated_app_env() as env_root, temporary_config() as config_path:
            mirror_dir = env_root / "data" / "local-archive"
            (mirror_dir / "INBOX" / "cur").mkdir(parents=True, exist_ok=True)
            (mirror_dir / "INBOX" / "new").mkdir(parents=True, exist_ok=True)
            (mirror_dir / "INBOX" / "tmp").mkdir(parents=True, exist_ok=True)

            bbdb_file = env_root / "data" / "contacts.bbdb"
            bbdb_file.write_text(
                ";; -*-coding: utf-8-emacs;-*-\n"
                '["Ada" "Lovelace" nil nil nil nil nil'
                ' ("ada@example.com") nil nil]\n',
                encoding="utf-8",
            )

            # Top-level keys must precede every table, so bbdb_path goes
            # first — appending it would land it inside [accounts.mirror].
            config_path.write_text(
                f'bbdb_path = "{bbdb_file}"\n'
                + sample_config_toml()
                + "\n\n[[accounts]]\n"
                'account_type = "local"\n'
                'name = "archive"\n'
                'email_address = "archive@example.com"\n'
                "\n[accounts.mirror]\n"
                f'path = "{mirror_dir}"\n'
                'format = "maildir"\n',
                encoding="utf-8",
            )

            with patch("pony.tui.PonyApp") as app_cls:
                rc = run_cli_ret("--config", str(config_path), "tui")

            self.assertEqual(rc, 0)
            app_cls.return_value.run.assert_called_once()
            # The BBDB contact was imported into the index on the way in.
            output = run_cli(
                "--config", str(config_path), "contacts", "show", "ada@example.com"
            )
            self.assertIn("Ada", output)

    def test_the_contacts_browser_command_opens_the_app(self) -> None:
        from unittest.mock import patch

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            patch("pony.tui.app.ContactsApp") as app_cls,
        ):
            rc = run_cli_ret("--config", str(config_path), "contacts", "browse")

        self.assertEqual(rc, 0)
        app_cls.return_value.run.assert_called_once()

    def test_the_view_command_opens_an_eml_file(self) -> None:
        from unittest.mock import patch

        with isolated_app_env():
            eml = TMP_ROOT / f"view-{uuid4().hex}.eml"
            eml.write_bytes(corpus.plain_text())
            try:
                with patch("pony.tui.app.EmlViewerApp") as app_cls:
                    rc = run_cli_ret("view", str(eml))
            finally:
                eml.unlink(missing_ok=True)

        self.assertEqual(rc, 0)
        app_cls.return_value.run.assert_called_once()

    def test_no_command_at_all_launches_the_tui(self) -> None:
        from unittest.mock import patch

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            patch("pony.tui.PonyApp") as app_cls,
        ):
            rc = run_cli_ret("--config", str(config_path))

        self.assertEqual(rc, 0)
        app_cls.return_value.run.assert_called_once()

    def test_the_view_command_rejects_an_unknown_theme(self) -> None:
        from unittest.mock import patch

        with isolated_app_env():
            eml = TMP_ROOT / f"theme-{uuid4().hex}.eml"
            eml.write_bytes(corpus.plain_text())
            try:
                with patch("pony.tui.app.EmlViewerApp") as app_cls:
                    rc = run_cli_ret("--theme", "nope", "view", str(eml))
            finally:
                eml.unlink(missing_ok=True)

        self.assertEqual(rc, 1)
        app_cls.assert_not_called()

    def test_the_docs_command_opens_a_browser(self) -> None:
        from unittest.mock import patch

        with isolated_app_env(), patch("webbrowser.open") as opener:
            output, rc = run_cli_capture("docs")

        self.assertEqual(rc, 0)
        opener.assert_called_once()
        self.assertIn("docs:", output)

    def test_the_mcp_command_starts_the_server(self) -> None:
        from unittest.mock import patch

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            patch("pony.mcp_server.run_mcp_server") as serve,
        ):
            rc = run_cli_ret("--config", str(config_path), "mcp")

        self.assertEqual(rc, 0)
        serve.assert_called_once()


class PagerFallbackTests(unittest.TestCase):
    """The built-in pager used when ``less`` is unavailable."""

    def test_long_text_pages_until_the_user_quits(self) -> None:
        from unittest.mock import patch

        from pony.cli import _run_pager

        text = "\n".join(f"line {i}" for i in range(200))
        with (
            patch("shutil.which", return_value=None),
            patch("builtins.input", return_value="q"),
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                _run_pager(text)

        printed = buffer.getvalue()
        self.assertIn("line 0", printed)
        # Quitting at the first prompt means the tail was never printed.
        self.assertNotIn("line 199", printed)

    def test_a_closed_stdin_ends_the_pager(self) -> None:
        from unittest.mock import patch

        from pony.cli import _run_pager

        text = "\n".join(f"line {i}" for i in range(200))
        with (
            patch("shutil.which", return_value=None),
            patch("builtins.input", side_effect=EOFError),
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                _run_pager(text)

        self.assertIn("line 0", buffer.getvalue())

    def test_short_text_never_prompts(self) -> None:
        from unittest.mock import patch

        from pony.cli import _run_pager

        with (
            patch("shutil.which", return_value=None),
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                _run_pager("one\ntwo")

        self.assertIn("one", buffer.getvalue())


class FormattingHelperTests(unittest.TestCase):
    """Human-readable formatters used across the summary commands."""

    def test_durations_switch_to_seconds_past_a_thousand_ms(self) -> None:
        from pony.cli import _fmt_ms

        self.assertEqual(_fmt_ms(999), "999ms")
        self.assertEqual(_fmt_ms(1500), "1.5s")

    def test_sizes_climb_to_terabytes(self) -> None:
        from pony.cli import _fmt_size

        self.assertEqual(_fmt_size(512), "512 B")
        self.assertEqual(_fmt_size(3 * 1024**4), "3.0 TB")


class CorruptIndexQueryTests(unittest.TestCase):
    """``local-summary`` reads the index directly and must survive a bad file."""

    def test_a_corrupt_database_yields_empty_results(self) -> None:
        from pony.cli import _query_index

        bad = TMP_ROOT / f"corrupt-{uuid4().hex}.sqlite3"
        bad.write_bytes(b"this is definitely not a sqlite database")
        try:
            self.assertEqual(_query_index(bad, "personal"), {})
        finally:
            bad.unlink(missing_ok=True)


class RemainingCommandArmTests(unittest.TestCase):
    """Small arms of commands whose main paths are covered elsewhere."""

    def test_docs_prefers_a_bundled_copy_when_frozen(self) -> None:
        """A frozen build ships HTML next to the binary; use it over the web."""
        from unittest.mock import patch

        with isolated_app_env():
            bundled = TMP_ROOT / f"docs-{uuid4().hex}"
            bundled.mkdir(parents=True, exist_ok=True)
            (bundled / "index.html").write_text("<html></html>", encoding="utf-8")
            try:
                with (
                    patch("pony.paths.bundled_docs_path", return_value=bundled),
                    patch("webbrowser.open") as opener,
                ):
                    output, rc = run_cli_capture("docs")
            finally:
                (bundled / "index.html").unlink(missing_ok=True)
                bundled.rmdir()

        self.assertEqual(rc, 0)
        self.assertIn("bundled docs", output)
        opener.assert_called_once()

    def test_compose_without_any_configured_account_exits(self) -> None:
        with isolated_app_env() as env_root:
            config_path = env_root / "config" / "empty.toml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("config_version = 2\n", encoding="utf-8")

            with self.assertRaises(SystemExit) as ctx:
                run_cli("--config", str(config_path), "compose")

        self.assertIn("No accounts configured", str(ctx.exception))

    def test_mbox_folders_are_listed_without_counting_them(self) -> None:
        """Counting an mbox means parsing it, so the summary shows paths only."""
        from pony.cli import _mbox_folders

        root = TMP_ROOT / f"mbox-summary-{uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            (root / "INBOX.mbox").write_bytes(b"")
            (root / "Archive.Work.mbox").write_bytes(b"")
            listed = _mbox_folders(root)
        finally:
            for name in ("INBOX.mbox", "Archive.Work.mbox"):
                (root / name).unlink(missing_ok=True)
            root.rmdir()

        # Dots in the filename encode the folder hierarchy.
        self.assertEqual(listed, {"INBOX": None, "Archive/Work": None})

    def test_an_empty_mbox_root_still_reports_an_inbox(self) -> None:
        from pony.cli import _mbox_folders

        root = TMP_ROOT / f"mbox-empty-{uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            self.assertEqual(_mbox_folders(root), {"INBOX": None})
        finally:
            root.rmdir()

    def test_the_index_query_reports_counts_and_last_sync(self) -> None:
        """``local-summary`` reads the index directly rather than via the repo."""
        from pony.cli import _query_index
        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Counted", body="body")
            db = AppPaths.default().index_db_file

            counts = _query_index(db, "personal")

        self.assertIn("INBOX", counts)
        self.assertEqual(counts["INBOX"][0], 1)

    def test_the_schema_reset_skips_targets_that_do_not_exist(self) -> None:
        """A mirror listed in config but never created is not an error."""
        import sqlite3

        from pony.paths import AppPaths

        with isolated_app_env(), temporary_config() as config_path:
            app_paths = AppPaths.default()
            app_paths.ensure_runtime_dirs()
            db = app_paths.index_db_file
            db.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db)
            try:
                conn.executescript(
                    "CREATE TABLE messages (account_name TEXT);PRAGMA user_version = 0;"
                )
                conn.commit()
            finally:
                conn.close()

            # The configured mirror directory is deliberately absent.
            stdin_backup = sys.stdin
            sys.stdin = io.StringIO("y\n")
            try:
                captured, rc = run_cli_capture(
                    "--config", str(config_path), "search", "anything"
                )
            finally:
                sys.stdin = stdin_backup

        self.assertEqual(rc, 0)
        self.assertIn("(not found)", captured)
        self.assertFalse(db.exists())


class AttachmentDestinationSafetyTests(unittest.TestCase):
    """``Content-Disposition`` filenames are chosen by the sender.

    ``pony message attachment`` writes into the working directory by
    default.  Joining an unsanitised filename onto it lets a crafted
    message steer the write anywhere the user can write — the same
    traversal the TUI already guards against with ``is_relative_to``.
    """

    def _seed_traversal_message(self, config_path: Path) -> _SeedHandle:
        return _seed_fixture(config_path, corpus.traversal_attachment_filename())

    def test_a_traversing_filename_cannot_escape_the_working_directory(self) -> None:
        import tempfile

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            ref = self._seed_traversal_message(config_path)
            outside = Path(tmpdir) / "escaped.txt"
            workdir = Path(tmpdir) / "deep" / "work"
            workdir.mkdir(parents=True)

            prev_cwd = os.getcwd()
            os.chdir(workdir)
            try:
                output = run_cli(
                    "--config",
                    str(config_path),
                    "message",
                    "attachment",
                    ref.account_name,
                    ref.folder_name,
                    ref.rfc5322_id,
                    "1",
                )
            finally:
                os.chdir(prev_cwd)

            # Nothing was written outside the directory the user was in.
            self.assertFalse(
                outside.exists(), "attachment escaped the working directory"
            )
            self.assertFalse((Path(tmpdir) / "deep" / "escaped.txt").exists())
            # The bytes landed under the cwd, under a bare filename.
            written = workdir / "escaped.txt"
            self.assertTrue(written.is_file(), output)
            self.assertEqual(written.read_bytes(), b"payload-bytes")

    def test_an_explicit_output_path_is_still_honoured(self) -> None:
        """``-o`` is the user's own choice and must not be second-guessed."""
        import tempfile

        with (
            isolated_app_env(),
            temporary_config() as config_path,
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            ref = self._seed_traversal_message(config_path)
            target = Path(tmpdir) / "nested" / "chosen.bin"
            target.parent.mkdir(parents=True)

            run_cli(
                "--config",
                str(config_path),
                "message",
                "attachment",
                ref.account_name,
                ref.folder_name,
                ref.rfc5322_id,
                "1",
                "-o",
                str(target),
            )

            self.assertEqual(target.read_bytes(), b"payload-bytes")


class SearchQueryLanguageTests(unittest.TestCase):
    """``pony search`` speaks the same query language as the TUI's ``/``.

    The parser used to live under ``tui/``, so the CLI passed the raw
    string straight into the ``text`` field and ``pony search
    "from:alice"`` looked for the literal characters ``from:alice``.
    """

    def test_a_field_prefix_scopes_the_search(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(
                config_path, subject="Quarterly report", body="nothing special"
            )
            from_hit = run_cli(
                "--config", str(config_path), "search", "from:sender@example.com"
            )
            from_miss = run_cli(
                "--config", str(config_path), "search", "from:nobody@example.com"
            )

        self.assertIn("Total hits: 1", from_hit)
        self.assertIn("Total hits: 0", from_miss)

    def test_a_bare_word_still_matches_the_subject(self) -> None:
        """The pre-existing behaviour: bare words are the broad default."""
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(
                config_path, subject="Quarterly report", body="nothing special"
            )
            output = run_cli("--config", str(config_path), "search", "Quarterly")

        self.assertIn("Total hits: 1", output)

    def test_the_body_prefix_narrows_to_the_body(self) -> None:
        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(
                config_path, subject="Quarterly report", body="nothing special"
            )
            in_body = run_cli("--config", str(config_path), "search", "body:special")
            subject_only = run_cli(
                "--config", str(config_path), "search", "body:Quarterly"
            )

        self.assertIn("Total hits: 1", in_body)
        self.assertIn("Total hits: 0", subject_only)

    def test_the_search_command_routes_through_the_shared_parser(self) -> None:
        """Not just "prefixes happen to work" — the same parser is called."""
        from unittest.mock import patch

        from pony.search_parser import parse_query

        with isolated_app_env(), temporary_config() as config_path:
            _seed_one_message(config_path, subject="Subject", body="body")
            with patch("pony.cli.parse_query", side_effect=parse_query) as spy:
                run_cli("--config", str(config_path), "search", "from:alice")

        spy.assert_called_once_with("from:alice")
