"""Reconciliation tests for the IMAP sync engine.

Uses a FakeImapSession to simulate server state without a real IMAP connection.
All tests verify the sync engine's behaviour by inspecting the local index and
mirror after calling ImapSyncService.sync().
"""

from __future__ import annotations

import dataclasses
import unittest
from collections.abc import Sequence
from email.message import EmailMessage
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import (
    AccountConfig,
    AppConfig,
    FolderConfig,
    FolderRef,
    MessageFlag,
    MessageStatus,
    MirrorConfig,
)
from pony.index_store import SqliteIndexRepository
from pony.protocols import ImapClientSession, MirrorRepository
from pony.storage import MaildirMirrorRepository
from pony.sync import ImapSyncService

# ---------------------------------------------------------------------------
# FakeImapSession
# ---------------------------------------------------------------------------


class FakeImapSession:
    """Simulates an IMAP server folder with a preset set of messages.

    Implements :class:`pony.protocols.ImapClientSession`.

    ``folders`` maps folder_name → dict[uid, (message_id, flags, raw_bytes)].
    ``stored_flags`` records every ``store_flags`` call for assertion in tests.
    ``deleted_uids`` records every ``mark_deleted`` call.
    """

    def __init__(
        self,
        folders: dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]],
        uid_validity: int = 1,
    ) -> None:
        self.folders = folders
        self.uid_validity = uid_validity
        self._selected: str = ""
        self.stored_flags: list[tuple[int, frozenset[MessageFlag]]] = []
        self.stored_extra: list[tuple[int, frozenset[str]]] = []
        self.extra_flags: dict[tuple[str, int], frozenset[str]] = {}
        self.deleted_uids: list[int] = []
        self.moves: list[tuple[str, int, str]] = []
        self.created_folders: list[str] = []
        # Ordered log of side-effecting calls (create, move, append, ...)
        # so tests can assert on sequencing — e.g. CREATE before MOVE.
        self.call_log: list[str] = []

    def list_folders(self) -> Sequence[str]:
        return list(self.folders.keys())

    def get_uid_validity(self, folder_name: str) -> int:
        self._selected = folder_name
        return self.uid_validity

    def fetch_uid_to_message_id(
        self, folder_name: str
    ) -> dict[int, tuple[str, tuple[frozenset[MessageFlag], frozenset[str]]]]:
        folder = self.folders.get(folder_name, {})
        return {
            uid: (mid, (flags, self.extra_flags.get((folder_name, uid), frozenset())))
            for uid, (mid, flags, _) in folder.items()
        }

    def fetch_flags(
        self, folder_name: str, uids: Sequence[int]
    ) -> dict[int, tuple[frozenset[MessageFlag], frozenset[str]]]:
        folder = self.folders.get(folder_name, {})
        result: dict[int, tuple[frozenset[MessageFlag], frozenset[str]]] = {}
        for uid, (_, flags, _) in folder.items():
            if uid in uids:
                extra = self.extra_flags.get((folder_name, uid), frozenset())
                result[uid] = (flags, extra)
        return result

    def fetch_messages_batch(
        self, folder_name: str, uids: Sequence[int],
    ) -> dict[int, bytes]:
        folder = self.folders.get(folder_name, {})
        return {
            uid: folder[uid][2] for uid in uids if uid in folder
        }

    def fetch_message_bytes(self, folder_name: str, uid: int) -> bytes:
        folder = self.folders.get(folder_name, {})
        if uid not in folder:
            raise KeyError(f"UID {uid} not found in {folder_name!r}")
        return folder[uid][2]

    def store_flags(
        self,
        folder_name: str,
        uid: int,
        flags: frozenset[MessageFlag],
        extra_imap_flags: frozenset[str] = frozenset(),
    ) -> None:
        self.stored_flags.append((uid, flags))
        self.stored_extra.append((uid, extra_imap_flags))
        folder = self.folders.get(folder_name, {})
        if uid in folder:
            mid, _, raw = folder[uid]
            folder[uid] = (mid, flags, raw)
        self.extra_flags[(folder_name, uid)] = extra_imap_flags

    def append_message(
        self,
        folder_name: str,
        raw_message: bytes,
        flags: frozenset[MessageFlag],
        extra_imap_flags: frozenset[str] = frozenset(),
    ) -> None:
        folder = self.folders.setdefault(folder_name, {})
        new_uid = max(folder.keys(), default=0) + 1000
        # Parse Message-ID from raw for test assertions.
        from email import policy as _policy
        from email.parser import BytesParser

        msg = BytesParser(policy=_policy.default).parsebytes(raw_message)
        mid = msg.get("Message-ID", "")
        folder[new_uid] = (mid, flags, raw_message)
        self.extra_flags[(folder_name, new_uid)] = extra_imap_flags

    def mark_deleted(self, folder_name: str, uid: int) -> None:  # noqa: ARG002
        self.deleted_uids.append(uid)

    def expunge(self, folder_name: str) -> None:
        folder = self.folders.get(folder_name, {})
        for uid in list(self.deleted_uids):
            folder.pop(uid, None)

    def move_message(
        self, source_folder: str, uid: int, target_folder: str,
    ) -> None:
        # Record the call so tests can assert PushMoveOp executed.
        self.call_log.append(f"move:{source_folder}->{target_folder}:{uid}")
        self.moves.append((source_folder, uid, target_folder))
        # Mirror real IMAP: MOVE to a non-existent mailbox is an error.
        # This turns "forgot to CREATE before MOVE" into a test failure.
        if target_folder not in self.folders:
            raise OSError(
                f"MOVE target folder does not exist: {target_folder!r}"
            )
        source = self.folders.get(source_folder, {})
        entry = source.pop(uid, None)
        if entry is None:
            return
        target = self.folders[target_folder]
        new_uid = max(target.keys(), default=0) + 2000
        target[new_uid] = entry
        # Carry over custom flags when present.
        extra = self.extra_flags.pop((source_folder, uid), frozenset())
        if extra:
            self.extra_flags[(target_folder, new_uid)] = extra

    def create_folder(self, folder_name: str) -> None:
        self.call_log.append(f"create:{folder_name}")
        self.created_folders.append(folder_name)
        self.folders.setdefault(folder_name, {})

    def logout(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_raw_message(
    subject: str,
    message_id: str,
    *,
    sender: str = "alice@example.com",
    recipient: str = "bob@example.com",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
    msg["Message-ID"] = message_id
    msg.set_content(f"Body of: {subject}")
    return msg.as_bytes()


def _setup(
    *,
    server_folders: dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]],
    uid_validity: int = 1,
) -> tuple[
    ImapSyncService, SqliteIndexRepository, MaildirMirrorRepository, FakeImapSession
]:
    """Build a sync service wired to an in-memory fake session."""
    tmp = TMP_ROOT / "sync" / uuid4().hex
    tmp.mkdir(parents=True, exist_ok=True)

    mirror = MaildirMirrorRepository(account_name="personal", root_dir=tmp / "mirror")
    index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
    index.initialize()

    account = AccountConfig(
        name="personal",
        email_address="bob@example.com",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="bob",
        credentials_source="plaintext",
        mirror=MirrorConfig(
            path=tmp / "mirror",
            format="maildir",
        ),
    )
    config = AppConfig(accounts=(account,))

    session = FakeImapSession(folders=server_folders, uid_validity=uid_validity)

    class _FixedCredentials:
        def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
            return "test-password"

    def _mirror_factory(_acc: AccountConfig) -> MirrorRepository:
        return mirror

    def _session_factory(_acc: AccountConfig, _password: str) -> ImapClientSession:
        return session

    service = ImapSyncService(
        config=config,
        mirror_factory=_mirror_factory,
        index=index,
        credentials=_FixedCredentials(),
        session_factory=_session_factory,
    )

    return service, index, mirror, session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class NewMessagesTestCase(unittest.TestCase):
    """New messages on the server are fetched, mirrored, and indexed."""

    def test_new_messages_appear_in_index(self) -> None:
        raw1 = _make_raw_message("Hello", "<msg1@example.com>")
        raw2 = _make_raw_message("World", "<msg2@example.com>")
        service, index, _, _ = _setup(
            server_folders={
                "INBOX": {
                    1: ("<msg1@example.com>", frozenset(), raw1),
                    2: ("<msg2@example.com>", frozenset({MessageFlag.SEEN}), raw2),
                }
            }
        )

        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        mids = {r.message_ref.message_id for r in rows}
        self.assertIn("<msg1@example.com>", mids)
        self.assertIn("<msg2@example.com>", mids)

    def test_server_flags_stored_as_local_and_base(self) -> None:
        raw = _make_raw_message("Flagged", "<flagged@example.com>")
        service, index, _, _ = _setup(
            server_folders={
                "INBOX": {
                    1: (
                        "<flagged@example.com>",
                        frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
                        raw,
                    )
                }
            }
        )

        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            row.local_flags, frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED})
        )
        self.assertEqual(row.base_flags, row.local_flags)
        self.assertEqual(row.local_status, MessageStatus.ACTIVE)

    def test_server_state_recorded(self) -> None:
        raw = _make_raw_message("Hi", "<hi@example.com>")
        service, index, _, _ = _setup(
            server_folders={"INBOX": {42: ("<hi@example.com>", frozenset(), raw)}}
        )

        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].uid, 42)
        self.assertEqual(rows[0].message_ref.message_id, "<hi@example.com>")
        self.assertIsNotNone(rows[0].synced_at)

    def test_sync_is_idempotent(self) -> None:
        raw = _make_raw_message("Once", "<once@example.com>")
        service, index, _, _ = _setup(
            server_folders={"INBOX": {1: ("<once@example.com>", frozenset(), raw)}}
        )

        service.sync()
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)


class FlagReconciliationTestCase(unittest.TestCase):
    """Three-way flag merge between local, base, and server states."""

    def _seed(
        self,
        *,
        server_flags: frozenset[MessageFlag],
        index: SqliteIndexRepository,
    ) -> None:
        """Run an initial sync to establish the base state."""
        pass  # handled by the first sync call in each test

    def test_server_flag_change_pulled_to_local(self) -> None:
        raw = _make_raw_message("Read on phone", "<phone@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<phone@example.com>", frozenset(), raw)}}
        )
        service.sync()  # base sync: no flags

        # Server marks as SEEN between syncs.
        session.folders["INBOX"][1] = (
            "<phone@example.com>",
            frozenset({MessageFlag.SEEN}),
            raw,
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(rows[0].local_flags, frozenset({MessageFlag.SEEN}))

    def test_local_flag_change_pushed_to_server(self) -> None:
        raw = _make_raw_message("Mark me", "<mark@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<mark@example.com>", frozenset(), raw)}}
        )
        service.sync()  # base sync

        # User flags the message locally.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)

        updated = dataclasses.replace(
            rows[0], local_flags=frozenset({MessageFlag.FLAGGED}),
        )
        index.upsert_message(message=updated)

        service.sync()

        # The session should have received a store_flags call.
        pushed = [(uid, f) for uid, f in session.stored_flags]
        self.assertTrue(
            any(MessageFlag.FLAGGED in flags for _, flags in pushed),
            "Expected FLAGGED to be pushed to server",
        )

    def test_flag_conflict_resolved_by_union(self) -> None:
        raw = _make_raw_message("Both sides", "<both@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<both@example.com>", frozenset(), raw)}}
        )
        service.sync()  # base: no flags

        # Server marks SEEN; local marks FLAGGED independently.
        session.folders["INBOX"][1] = (
            "<both@example.com>",
            frozenset({MessageFlag.SEEN}),
            raw,
        )
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)

        updated = dataclasses.replace(
            rows[0], local_flags=frozenset({MessageFlag.FLAGGED}),
        )
        index.upsert_message(message=updated)

        service.sync()

        rows = index.list_folder_messages(folder=folder)
        self.assertIn(MessageFlag.SEEN, rows[0].local_flags)
        self.assertIn(MessageFlag.FLAGGED, rows[0].local_flags)


class ServerDeletionTestCase(unittest.TestCase):
    """Messages deleted on the server are moved to local trash."""

    def test_server_deletion_moves_to_trash(self) -> None:
        raw = _make_raw_message("Delete me", "<del@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<del@example.com>", frozenset(), raw)}}
        )
        service.sync()  # ingest

        # Remove from server.
        del session.folders["INBOX"][1]
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].local_status, MessageStatus.TRASHED)


class ServerMoveTestCase(unittest.TestCase):
    """Messages moved between folders on the server are tracked locally."""

    def test_server_move_updates_local_folder(self) -> None:
        raw = _make_raw_message("Move me", "<move@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<move@example.com>", frozenset(), raw)},
                "Archive": {},
            }
        )
        service.sync()  # ingest into INBOX

        # Move from INBOX to Archive on the server.
        del session.folders["INBOX"][1]
        session.folders["Archive"][2] = ("<move@example.com>", frozenset(), raw)
        service.sync()

        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        archive = FolderRef(account_name="personal", folder_name="Archive")
        inbox_rows = index.list_folder_messages(folder=inbox)
        archive_rows = index.list_folder_messages(folder=archive)

        inbox_mids = {r.message_ref.message_id for r in inbox_rows}
        archive_mids = {r.message_ref.message_id for r in archive_rows}

        self.assertNotIn("<move@example.com>", inbox_mids)
        self.assertIn("<move@example.com>", archive_mids)


class UidValidityResetTestCase(unittest.TestCase):
    """A UIDVALIDITY change triggers a full resync without data loss."""

    def test_uidvalidity_reset_resyncs_cleanly(self) -> None:
        raw = _make_raw_message("Survive reset", "<reset@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<reset@example.com>", frozenset(), raw)}},
            uid_validity=1,
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows_before = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows_before), 1)
        self.assertEqual(rows_before[0].uid, 1)

        # Server resets UIDVALIDITY and re-assigns UIDs.
        session.uid_validity = 2
        session.folders["INBOX"] = {
            10: ("<reset@example.com>", frozenset({MessageFlag.SEEN}), raw)
        }
        service.sync()

        # Message still present in index, now with new UID.
        rows_after = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows_after), 1)
        self.assertEqual(rows_after[0].uid, 10)
        mids = {r.message_ref.message_id for r in rows_after}
        self.assertIn("<reset@example.com>", mids)


class SyntheticMessageIdTestCase(unittest.TestCase):
    """Messages without a Message-ID header receive a stable synthetic ID."""

    def test_missing_message_id_gets_synthetic(self) -> None:
        msg = EmailMessage()
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Subject"] = "No ID"
        msg["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
        msg.set_content("body")
        raw = msg.as_bytes()  # no Message-ID header

        service, index, _, _ = _setup(
            server_folders={"INBOX": {1: ("", frozenset(), raw)}}
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        self.assertTrue(
            rows[0].message_ref.message_id.startswith("<synthetic-"),
            f"Expected synthetic ID, got {rows[0].message_ref.message_id!r}",
        )

    def test_synthetic_id_is_stable_across_syncs(self) -> None:
        msg = EmailMessage()
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Subject"] = "Stable"
        msg["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
        msg.set_content("body")
        raw = msg.as_bytes()

        service, index, _, _ = _setup(
            server_folders={"INBOX": {1: ("", frozenset(), raw)}}
        )
        service.sync()
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)


class HasAttachmentsPreservationTestCase(unittest.TestCase):
    """has_attachments is not lost when sync mutates an indexed message."""

    def _make_attachment_message(self) -> bytes:
        """Simple message with a PDF attachment."""
        from email.mime.application import MIMEApplication
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("mixed")
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "With attachment"
        msg["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
        msg["Message-ID"] = "<att@example.com>"
        msg.attach(MIMEText("See attached.\n", "plain", "utf-8"))
        pdf = MIMEApplication(b"%PDF fake", Name="doc.pdf")
        pdf["Content-Disposition"] = 'attachment; filename="doc.pdf"'
        msg.attach(pdf)
        return msg.as_bytes()

    def test_has_attachments_preserved_through_flag_pull(self) -> None:
        raw = self._make_attachment_message()
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<att@example.com>", frozenset(), raw)}}
        )
        service.sync()

        # Verify has_attachments was set on ingest.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertTrue(rows[0].has_attachments)

        # Server marks SEEN — triggers PullFlagsOp.
        session.folders["INBOX"][1] = (
            "<att@example.com>",
            frozenset({MessageFlag.SEEN}),
            raw,
        )
        service.sync()

        rows = index.list_folder_messages(folder=folder)
        self.assertTrue(
            rows[0].has_attachments,
            "has_attachments must survive a flag pull",
        )

    def test_has_attachments_preserved_through_server_move(self) -> None:
        raw = self._make_attachment_message()
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<att@example.com>", frozenset(), raw)},
                "Archive": {},
            }
        )
        service.sync()

        # Confirm set after initial ingest.
        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=inbox)
        self.assertTrue(rows[0].has_attachments)

        # Move to Archive on the server.
        del session.folders["INBOX"][1]
        session.folders["Archive"][2] = ("<att@example.com>", frozenset(), raw)
        service.sync()

        archive = FolderRef(account_name="personal", folder_name="Archive")
        rows = index.list_folder_messages(folder=archive)
        self.assertEqual(len(rows), 1)
        self.assertTrue(
            rows[0].has_attachments,
            "has_attachments must survive a server move",
        )


class PushDeleteTestCase(unittest.TestCase):
    """Locally-deleted messages are expunged from the server."""

    def test_local_trash_expunged_from_server(self) -> None:
        raw = _make_raw_message("Goodbye", "<bye@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<bye@example.com>", frozenset(), raw)}}
        )
        service.sync()  # ingest

        # Trash the message locally.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(rows[0], local_status=MessageStatus.TRASHED)
        )

        service.sync()

        # Server should have received mark_deleted + expunge.
        self.assertIn(1, session.deleted_uids)
        # Message purged from local index too.
        rows_after = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows_after), 0)

    def test_trashed_message_does_not_generate_spurious_flag_ops(self) -> None:
        """A TRASHED message in a writable folder must not also emit PushFlagsOp."""
        raw = _make_raw_message("Trash me", "<trashme@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<trashme@example.com>", frozenset(), raw)}}
        )
        service.sync()

        # Locally mark as trashed AND change flags.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(
                rows[0],
                local_status=MessageStatus.TRASHED,
                local_flags=frozenset({MessageFlag.SEEN}),
            )
        )

        service.sync()

        # Only the delete should have been pushed — not any flag store.
        self.assertIn(1, session.deleted_uids)
        # store_flags must not have been called (only flag-changes should push flags).
        flag_stores_for_uid_1 = [uid for uid, _ in session.stored_flags if uid == 1]
        self.assertEqual(
            flag_stores_for_uid_1, [], "No flag push for a TRASHED message"
        )


class ReadOnlyFolderTestCase(unittest.TestCase):
    """Read-only folders: no writes pushed to server."""

    def _setup_with_readonly(
        self,
    ) -> tuple[ImapSyncService, SqliteIndexRepository, FakeImapSession]:
        tmp = TMP_ROOT / "sync" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)

        mirror = MaildirMirrorRepository(
            account_name="personal", root_dir=tmp / "mirror"
        )
        index = SqliteIndexRepository(
            database_path=tmp / "index.sqlite3"
        )
        index.initialize()

        account = AccountConfig(
            name="personal",
            email_address="bob@example.com",
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            username="bob",
            credentials_source="plaintext",
            mirror=MirrorConfig(path=tmp / "mirror", format="maildir"),
            folders=FolderConfig(read_only=("Archive",)),
        )
        config = AppConfig(accounts=(account,))

        raw = _make_raw_message("Read-only msg", "<ro@example.com>")
        session = FakeImapSession(
            folders={
                "INBOX": {},
                "Archive": {1: ("<ro@example.com>", frozenset(), raw)},
            }
        )

        class _Creds:
            def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
                return "pw"

        def _mirror_factory(_acc: AccountConfig) -> MirrorRepository:
            return mirror

        def _session_factory(_acc: AccountConfig, _pw: str) -> ImapClientSession:
            return session

        svc = ImapSyncService(
            config=config,
            mirror_factory=_mirror_factory,
            index=index,
            credentials=_Creds(),
            session_factory=_session_factory,
        )
        return svc, index, session

    def test_local_flag_change_not_pushed_in_read_only_folder(self) -> None:
        service, index, session = self._setup_with_readonly()
        service.sync()

        # Locally flag the message.
        folder = FolderRef(account_name="personal", folder_name="Archive")
        rows = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(
                rows[0], local_flags=frozenset({MessageFlag.FLAGGED})
            )
        )

        service.sync()

        # No flags should have been pushed to server.
        self.assertEqual(session.stored_flags, [])

    def test_locally_trashed_message_restored_in_read_only_folder(self) -> None:
        service, index, session = self._setup_with_readonly()
        service.sync()

        # Trash the message locally.
        folder = FolderRef(account_name="personal", folder_name="Archive")
        rows = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(rows[0], local_status=MessageStatus.TRASHED)
        )

        service.sync()

        # Message should be restored (ACTIVE again), not deleted.
        rows_after = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows_after), 1)
        self.assertEqual(rows_after[0].local_status, MessageStatus.ACTIVE)
        # Nothing should have been deleted on the server.
        self.assertEqual(session.deleted_uids, [])


class WatermarkTestCase(unittest.TestCase):
    """Folder sync watermark is updated after each successful pass."""

    def test_watermark_recorded_after_sync(self) -> None:
        raw = _make_raw_message("Watermark", "<wm@example.com>")
        service, index, _, _ = _setup(
            server_folders={
                "INBOX": {
                    1: ("<wm1@example.com>", frozenset(), raw),
                    5: ("<wm5@example.com>", frozenset(), raw),
                }
            },
            uid_validity=42,
        )
        service.sync()

        state = index.get_folder_sync_state(
            account_name="personal", folder_name="INBOX"
        )
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.uid_validity, 42)
        self.assertEqual(state.highest_uid, 5)


class DuplicateMessageIdTestCase(unittest.TestCase):
    """C-7: duplicate Message-ID within and across folders."""

    def test_duplicate_mid_within_folder_both_ingested(self) -> None:
        """Two UIDs with the same Message-ID in one folder both end up indexed."""
        raw1 = _make_raw_message("First", "<dup@example.com>")
        raw2 = _make_raw_message("Second", "<dup@example.com>")
        service, index, _, _ = _setup(
            server_folders={
                "INBOX": {
                    1: ("<dup@example.com>", frozenset(), raw1),
                    2: ("<dup@example.com>", frozenset(), raw2),
                }
            }
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 2)
        mids = {r.message_ref.message_id for r in rows}
        # One keeps the real ID, the other gets a synthetic one.
        self.assertTrue(
            any(mid.startswith("<synthetic-") for mid in mids),
            f"Expected one synthetic ID, got {mids!r}",
        )
        self.assertIn("<dup@example.com>", mids)

    def test_duplicate_mid_across_folders_no_false_move(self) -> None:
        """Same Message-ID in two folders: deletion from one is NOT a move."""
        raw_a = _make_raw_message("In A", "<cross@example.com>")
        raw_b = _make_raw_message("In B", "<cross@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "FolderA": {1: ("<cross@example.com>", frozenset(), raw_a)},
                "FolderB": {1: ("<cross@example.com>", frozenset(), raw_b)},
            }
        )
        service.sync()

        # Delete from FolderA on server.
        del session.folders["FolderA"][1]
        service.sync()

        # Should be trashed in A (server delete), not moved to B.
        a = FolderRef(account_name="personal", folder_name="FolderA")
        a_rows = index.list_folder_messages(folder=a)
        trashed = [r for r in a_rows if r.local_status == MessageStatus.TRASHED]
        self.assertEqual(len(trashed), 1)


class DeletedFlagMergeTestCase(unittest.TestCase):
    """3a: \\Deleted must not be propagated by automatic union merge."""

    def test_deleted_flag_stripped_from_merge(self) -> None:
        from pony.sync import _merge_flags

        merged, conflict = _merge_flags(
            local=frozenset({MessageFlag.SEEN}),
            base=frozenset(),
            remote=frozenset({MessageFlag.DELETED}),
        )
        self.assertNotIn(MessageFlag.DELETED, merged)
        self.assertIn(MessageFlag.SEEN, merged)

    def test_same_change_both_sides_no_conflict(self) -> None:
        """3c: convergent changes are not conflicts."""
        from pony.sync import _merge_flags

        merged, conflict = _merge_flags(
            local=frozenset({MessageFlag.SEEN}),
            base=frozenset(),
            remote=frozenset({MessageFlag.SEEN}),
        )
        self.assertFalse(conflict)
        self.assertEqual(merged, frozenset({MessageFlag.SEEN}))

    def test_real_conflict_still_detected(self) -> None:
        from pony.sync import _merge_flags

        merged, conflict = _merge_flags(
            local=frozenset({MessageFlag.FLAGGED}),
            base=frozenset(),
            remote=frozenset({MessageFlag.SEEN}),
        )
        self.assertTrue(conflict)
        self.assertEqual(merged, frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}))


class BaseFlagsComparisonTestCase(unittest.TestCase):
    """3d: flag reconciliation must use base_flags from messages table."""

    def test_base_flags_diverged_from_server_flags(self) -> None:
        """When base_flags and server_flags disagree, base_flags governs."""
        raw = _make_raw_message("Diverge", "<div@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {
                    1: ("<div@example.com>", frozenset({MessageFlag.SEEN}), raw)
                }
            }
        )
        service.sync()

        # Simulate a crash-induced divergence: update base_flags in the
        # message row to differ from server_flags (which are SEEN).
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)

        # Set base_flags to empty (simulating a crash before base update).
        # local_flags also empty → "no local change" relative to base.
        # Keep uid and server_flags intact so the planner can find this row.
        import dataclasses as _dc
        index.upsert_message(
            message=_dc.replace(
                rows[0],
                local_flags=frozenset(),
                base_flags=frozenset(),
            )
        )

        # Server still has SEEN.  With the fix, base=empty so
        # server_changed=(SEEN != empty)=True, local_changed=(empty != empty)=False
        # → PullFlagsOp.
        service.sync()

        rows = index.list_folder_messages(folder=folder)
        self.assertIn(MessageFlag.SEEN, rows[0].local_flags)


class FetchFailureTestCase(unittest.TestCase):
    """5b: per-message fetch failure must not abort the entire folder."""

    def test_bad_fetch_skipped_good_messages_ingested(self) -> None:
        raw_good = _make_raw_message("Good", "<good@example.com>")

        class _FailOnUid2(FakeImapSession):
            def fetch_messages_batch(
                self, folder_name: str, uids: Sequence[int],
            ) -> dict[int, bytes]:
                # Simulate UID 2 failing — return it with empty bytes.
                result = super().fetch_messages_batch(folder_name, uids)
                if 2 in result:
                    result[2] = b""  # empty → skipped by _ingest_raw
                return result

        folders: dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]] = {
            "INBOX": {
                1: ("<good@example.com>", frozenset(), raw_good),
                2: ("<bad@example.com>", frozenset(), b"dummy"),
                3: ("<also-good@example.com>", frozenset(), raw_good),
            }
        }

        tmp = TMP_ROOT / "sync" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        from pony.storage import MaildirMirrorRepository

        mirror = MaildirMirrorRepository(
            account_name="personal", root_dir=tmp / "mirror"
        )
        index_repo = SqliteIndexRepository(
            database_path=tmp / "index.sqlite3"
        )
        index_repo.initialize()

        account = AccountConfig(
            name="personal",
            email_address="bob@example.com",
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            username="bob",
            credentials_source="plaintext",
            mirror=MirrorConfig(path=tmp / "mirror", format="maildir"),
        )
        config = AppConfig(accounts=(account,))
        session = _FailOnUid2(folders=folders)

        class _Creds:
            def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
                return "pw"

        svc = ImapSyncService(
            config=config,
            mirror_factory=lambda _: mirror,
            index=index_repo,
            credentials=_Creds(),
            session_factory=lambda _a, _p: session,
        )
        svc.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index_repo.list_folder_messages(folder=folder)
        mids = {r.message_ref.message_id for r in rows}
        # UID 1 and 3 should have been ingested; UID 2 skipped.
        self.assertIn("<good@example.com>", mids)
        self.assertIn("<also-good@example.com>", mids)
        self.assertEqual(len(rows), 2)


class EmptyMessageTestCase(unittest.TestCase):
    """8c: zero-byte messages are skipped gracefully."""

    def test_empty_body_skipped(self) -> None:
        raw_good = _make_raw_message("Good", "<good2@example.com>")
        service, index, _, _ = _setup(
            server_folders={
                "INBOX": {
                    1: ("<empty@example.com>", frozenset(), b""),
                    2: ("<good2@example.com>", frozenset(), raw_good),
                }
            }
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        mids = {r.message_ref.message_id for r in rows}
        self.assertIn("<good2@example.com>", mids)
        self.assertNotIn("<empty@example.com>", mids)


class ExtraImapFlagsTestCase(unittest.TestCase):
    """3b: custom/unknown IMAP flags are preserved through STORE operations."""

    def test_extra_flags_preserved_on_push(self) -> None:
        """When pushing local flag changes, extra server flags are included."""
        raw = _make_raw_message("Custom flags", "<custom@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<custom@example.com>", frozenset(), raw)}
            }
        )
        # Set extra flags on the server for this message.
        session.extra_flags[("INBOX", 1)] = frozenset({"$Important", "$Junk"})

        service.sync()  # base sync picks up extra flags

        # User flags the message locally.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(
                rows[0], local_flags=frozenset({MessageFlag.FLAGGED})
            )
        )

        service.sync()  # push local change

        # The STORE should include the extra flags alongside the known ones.
        self.assertTrue(len(session.stored_extra) > 0)
        _, extra = session.stored_extra[-1]
        self.assertIn("$Important", extra)
        self.assertIn("$Junk", extra)


class C1ReUploadTestCase(unittest.TestCase):
    """C-1: server-delete + locally-modified → re-upload to server."""

    def test_locally_modified_message_reuploaded_on_server_delete(self) -> None:
        raw = _make_raw_message("Important edit", "<c1@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<c1@example.com>", frozenset(), raw)}}
        )
        service.sync()

        # User flags locally.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(
                rows[0], local_flags=frozenset({MessageFlag.FLAGGED})
            )
        )

        # Server deletes the message.
        del session.folders["INBOX"][1]
        service.sync()

        # Message should still be ACTIVE locally.
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].local_status, MessageStatus.ACTIVE)

        # Message should have been re-uploaded to the server.
        self.assertTrue(
            len(session.folders["INBOX"]) > 0,
            "Expected message to be re-uploaded to server",
        )

    def test_unmodified_message_trashed_on_server_delete(self) -> None:
        """When local flags haven't changed, normal trash behavior applies."""
        raw = _make_raw_message("No edits", "<c1-nomod@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<c1-nomod@example.com>", frozenset(), raw)}
            }
        )
        service.sync()

        del session.folders["INBOX"][1]
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(rows[0].local_status, MessageStatus.TRASHED)


class C2RestoreTestCase(unittest.TestCase):
    """C-2: local-trash + server-flag-change → restore, cancel deletion."""

    def test_trashed_message_restored_when_server_flags_changed(self) -> None:
        raw = _make_raw_message("Restore me", "<c2@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<c2@example.com>", frozenset(), raw)}}
        )
        service.sync()

        # User trashes locally.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(rows[0], local_status=MessageStatus.TRASHED)
        )

        # Server marks SEEN between syncs.
        session.folders["INBOX"][1] = (
            "<c2@example.com>", frozenset({MessageFlag.SEEN}), raw,
        )
        service.sync()

        # Message should be restored to ACTIVE with new flags.
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].local_status, MessageStatus.ACTIVE)
        self.assertIn(MessageFlag.SEEN, rows[0].local_flags)
        # Server should NOT have received a delete.
        self.assertEqual(session.deleted_uids, [])

    def test_trashed_message_deleted_when_server_flags_unchanged(self) -> None:
        """When server didn't change flags, normal push-delete applies."""
        raw = _make_raw_message("Really delete", "<c2-nochg@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<c2-nochg@example.com>", frozenset(), raw)}
            }
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(rows[0], local_status=MessageStatus.TRASHED)
        )

        service.sync()

        # Normal delete should proceed.
        self.assertIn(1, session.deleted_uids)


class MassDeletionThresholdTestCase(unittest.TestCase):
    """C-6: mass deletion detected → folder needs confirmation."""

    def test_mass_delete_flags_folder(self) -> None:
        """Deleting >20% of messages in a folder sets needs_confirmation."""
        raws: dict[int, tuple[str, frozenset[MessageFlag], bytes]] = {
            uid: (
                f"<mass{uid}@example.com>",
                frozenset(),
                _make_raw_message(f"Msg {uid}", f"<mass{uid}@example.com>"),
            )
            for uid in range(1, 11)
        }
        service, index, _, session = _setup(
            server_folders={"INBOX": dict(raws)}
        )
        service.sync()

        # Delete 5 of 10 (50%) on the server.
        for uid in range(1, 6):
            del session.folders["INBOX"][uid]

        plan = service.plan()
        folder_plan = plan.accounts[0].folders[0]
        self.assertTrue(folder_plan.needs_confirmation)

    def test_small_delete_does_not_flag(self) -> None:
        """Deleting 1 of 10 (10%) does not trigger the threshold."""
        raws: dict[int, tuple[str, frozenset[MessageFlag], bytes]] = {
            uid: (
                f"<small{uid}@example.com>",
                frozenset(),
                _make_raw_message(f"Msg {uid}", f"<small{uid}@example.com>"),
            )
            for uid in range(1, 11)
        }
        service, index, _, session = _setup(
            server_folders={"INBOX": dict(raws)}
        )
        service.sync()

        del session.folders["INBOX"][1]
        plan = service.plan()
        folder_plan = plan.accounts[0].folders[0]
        self.assertFalse(folder_plan.needs_confirmation)

    def test_unconfirmed_folder_skipped_during_execute(self) -> None:
        """Folders needing confirmation are skipped if not confirmed."""
        raws: dict[int, tuple[str, frozenset[MessageFlag], bytes]] = {
            uid: (
                f"<skip{uid}@example.com>",
                frozenset(),
                _make_raw_message(f"Msg {uid}", f"<skip{uid}@example.com>"),
            )
            for uid in range(1, 11)
        }
        service, index, _, session = _setup(
            server_folders={"INBOX": dict(raws)}
        )
        service.sync()

        for uid in range(1, 6):
            del session.folders["INBOX"][uid]

        plan = service.plan()
        # Execute without confirming.
        result = service.execute(plan, confirmed_folders=frozenset())
        # Folder should be in skipped.
        self.assertIn("INBOX", result.accounts[0].skipped_folders)


class TrashGcTestCase(unittest.TestCase):
    """4d: trash GC purges expired trashed messages."""

    def test_expired_trash_purged_on_sync(self) -> None:
        raw = _make_raw_message("Old trash", "<gc@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<gc@example.com>", frozenset(), raw)}}
        )
        service.sync()

        # Trash the message and backdate trashed_at beyond retention.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        from datetime import timedelta

        old_date = rows[0].received_at - timedelta(days=60)
        index.upsert_message(
            message=dataclasses.replace(
                rows[0],
                local_status=MessageStatus.TRASHED,
                trashed_at=old_date,
            )
        )
        # Remove from server so it's a pure local trash.
        del session.folders["INBOX"][1]

        service.sync()

        # Message should have been GC'd.
        rows = index.list_folder_messages(folder=folder)
        trashed = [r for r in rows if r.local_status == MessageStatus.TRASHED]
        self.assertEqual(len(trashed), 0)


class MessageIdChangeTestCase(unittest.TestCase):
    """1c: Message-ID changes between syncs."""

    def test_flag_pull_works_after_message_id_change(self) -> None:
        raw = _make_raw_message("Reimported", "<old-id@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<old-id@example.com>", frozenset(), raw)}
            }
        )
        service.sync()

        # Server re-imports the message under a new Message-ID but same UID.
        raw2 = _make_raw_message("Reimported", "<new-id@example.com>")
        session.folders["INBOX"][1] = (
            "<new-id@example.com>",
            frozenset({MessageFlag.SEEN}),
            raw2,
        )
        service.sync()

        # The flag change should still have been pulled despite the ID change.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        seen_rows = [r for r in rows if MessageFlag.SEEN in r.local_flags]
        self.assertTrue(
            len(seen_rows) >= 1,
            "SEEN flag should have been pulled after Message-ID change",
        )


class CleanupTestCase(unittest.TestCase):
    """DB cleanup: stale accounts, stale folders."""

    def test_stale_account_purged_on_sync(self) -> None:
        """Rows for an account not in config are purged during sync."""
        raw = _make_raw_message("Stale", "<stale-acct@example.com>")
        service, index, _, _ = _setup(
            server_folders={
                "INBOX": {1: ("<stale-acct@example.com>", frozenset(), raw)}
            }
        )
        service.sync()

        # Manually insert a row for a different account.
        from datetime import UTC, datetime

        from pony.domain import IndexedMessage, MessageRef, MessageStatus

        stale_ref = MessageRef(
            account_name="removed-account",
            folder_name="INBOX",
            message_id="<orphan@example.com>",
        )
        index.upsert_message(
            message=IndexedMessage(
                message_ref=stale_ref,
                sender="x", recipients="y", cc="", subject="orphan",
                body_preview="", storage_key="",
                local_flags=frozenset(), base_flags=frozenset(),
                local_status=MessageStatus.ACTIVE,
                received_at=datetime.now(tz=UTC),
            )
        )
        self.assertIn("removed-account", index.list_indexed_accounts())

        # Next sync should purge it.
        service.sync()
        self.assertNotIn("removed-account", index.list_indexed_accounts())

    def test_stale_folder_state_purged_on_plan(self) -> None:
        """Sync state for folders no longer on the server is purged."""
        raw = _make_raw_message("Active", "<active@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<active@example.com>", frozenset(), raw)},
                "OldFolder": {2: ("<old@example.com>", frozenset(), raw)},
            }
        )
        service.sync()

        # Verify both folders have sync state.
        states = index.list_folder_sync_states(account_name="personal")
        folder_names = {s.folder_name for s in states}
        self.assertIn("OldFolder", folder_names)

        # Remove OldFolder from server.
        del session.folders["OldFolder"]
        service.sync()

        # OldFolder's sync state should be gone.
        states = index.list_folder_sync_states(account_name="personal")
        folder_names = {s.folder_name for s in states}
        self.assertNotIn("OldFolder", folder_names)


    # UTF-7 encoding/decoding tests removed — handled by imapclient.


class ProgressCallbackTestCase(unittest.TestCase):
    """Verify that plan() and execute() fire ProgressInfo callbacks."""

    def test_plan_fires_progress(self) -> None:
        from pony.sync import ProgressInfo

        raw = _make_raw_message("Test", "<plan-prog@test>")
        service, *_ = _setup(
            server_folders={
                "INBOX": {1: ("<plan-prog@test>", frozenset(), raw)},
            },
        )
        events: list[ProgressInfo] = []
        service.plan(progress=events.append)

        self.assertGreater(len(events), 0)
        # Should include a folder-scanning event with current/total.
        scan_events = [e for e in events if e.total > 0]
        self.assertGreater(len(scan_events), 0)
        self.assertEqual(scan_events[-1].current, scan_events[-1].total)

    def test_execute_fires_per_op_progress(self) -> None:
        from pony.sync import ProgressInfo

        raw1 = _make_raw_message("Msg1", "<exec-prog1@test>")
        raw2 = _make_raw_message("Msg2", "<exec-prog2@test>")
        service, *_ = _setup(
            server_folders={
                "INBOX": {
                    1: ("<exec-prog1@test>", frozenset(), raw1),
                    2: ("<exec-prog2@test>", frozenset(), raw2),
                },
            },
        )
        plan = service.plan()
        events: list[ProgressInfo] = []
        service.execute(plan, progress=events.append)

        # Should fire at least one per-op progress event.
        per_op = [e for e in events if e.total > 0 and "INBOX" in e.message]
        self.assertGreater(len(per_op), 0)
        # Last per-op event should have current == total.
        self.assertEqual(per_op[-1].current, per_op[-1].total)


class LocalMoveSyncTestCase(unittest.TestCase):
    """Local archive moves are pushed to the server via UID MOVE and then
    the resulting UID is adopted into the existing local row."""

    def _archive_locally(
        self,
        index: SqliteIndexRepository,
        *,
        source: str,
        target: str,
        message_id: str,
    ) -> None:
        """Simulate the TUI ``A`` action at the index level."""
        ref = FolderRef(account_name="personal", folder_name=source)
        rows = index.list_folder_messages(folder=ref)
        row = next(r for r in rows if r.message_ref.message_id == message_id)
        new_ref = dataclasses.replace(
            row.message_ref, folder_name=target,
        )
        new_row = dataclasses.replace(
            row,
            message_ref=new_ref,
            uid=None,
            server_flags=frozenset(),
            extra_imap_flags=frozenset(),
            synced_at=None,
        )
        with index.connection():
            index.delete_message(message_ref=row.message_ref)
            index.upsert_message(message=new_row)

    def test_archive_push_move_and_link_over_two_syncs(self) -> None:
        raw = _make_raw_message("Archive me", "<archive@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<archive@example.com>", frozenset(), raw)},
                "Archive": {},
            },
        )
        service.sync()  # ingest into INBOX

        # User archives locally.
        self._archive_locally(
            index,
            source="INBOX",
            target="Archive",
            message_id="<archive@example.com>",
        )

        # First post-archive sync: emits PushMoveOp (UID MOVE on server).
        service.sync()

        self.assertIn(
            ("INBOX", 1, "Archive"), session.moves,
            "Expected UID MOVE from INBOX to Archive",
        )
        # Server-side: INBOX now empty, Archive has the message.
        self.assertNotIn(1, session.folders["INBOX"])
        self.assertEqual(len(session.folders["Archive"]), 1)
        new_uid = next(iter(session.folders["Archive"]))

        # Local Archive row still has uid=NULL at this point
        # (PushMoveOp does not update it; next sync adopts the uid).
        archive = FolderRef(account_name="personal", folder_name="Archive")
        rows = index.list_folder_messages(folder=archive)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].uid)

        # Second sync: LinkLocalOp adopts the new UID into the pending row.
        service.sync()

        rows = index.list_folder_messages(folder=archive)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].uid, new_uid)
        self.assertEqual(
            rows[0].message_ref.message_id, "<archive@example.com>",
        )

    def test_archive_appended_when_server_has_no_copy(self) -> None:
        raw = _make_raw_message("Orphan", "<orphan@example.com>")
        service, index, mirror, session = _setup(
            server_folders={
                "INBOX": {1: ("<orphan@example.com>", frozenset(), raw)},
                "Archive": {},
            },
        )
        service.sync()
        self._archive_locally(
            index,
            source="INBOX",
            target="Archive",
            message_id="<orphan@example.com>",
        )

        # After local move there is no INBOX row, but the Maildir file is
        # still in INBOX's directory.  Relocate it to simulate the TUI.
        archive = FolderRef(account_name="personal", folder_name="Archive")
        archive_rows = index.list_folder_messages(folder=archive)
        self.assertEqual(len(archive_rows), 1)
        storage_key = archive_rows[0].storage_key
        from pony.domain import MessageRef
        new_ref = mirror.move_message_to_folder(
            message_ref=MessageRef(
                account_name="personal",
                folder_name="INBOX",
                message_id=storage_key,
            ),
            target_folder="Archive",
        )
        # Index row's storage_key updates to the new ref's message_id.
        updated = dataclasses.replace(
            archive_rows[0], storage_key=new_ref.message_id,
        )
        index.upsert_message(message=updated)

        # Simulate: between archive and sync, another client purged the
        # message from INBOX.  The server now has it nowhere.
        session.folders["INBOX"].pop(1, None)

        service.sync()

        # Expected: APPEND to Archive.  No UID MOVE executed.
        self.assertFalse(session.moves)
        self.assertEqual(len(session.folders["Archive"]), 1)
        appended_uid, (appended_mid, _, appended_raw) = next(
            iter(session.folders["Archive"].items())
        )
        self.assertEqual(appended_mid, "<orphan@example.com>")
        self.assertEqual(appended_raw, raw)
        self.assertGreater(appended_uid, 0)

    def test_idempotent_when_nothing_to_push(self) -> None:
        raw = _make_raw_message("Quiet", "<quiet@example.com>")
        service, _, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<quiet@example.com>", frozenset(), raw)},
                "Archive": {},
            },
        )
        service.sync()
        service.sync()

        self.assertFalse(session.moves)
        self.assertEqual(len(session.folders["INBOX"]), 1)

    def test_archive_to_brand_new_folder_creates_it_server_side(self) -> None:
        """Archiving to a folder that doesn't exist on the server yet causes
        the sync engine to CREATE it before executing the PushMoveOp."""
        raw = _make_raw_message("Pioneer", "<pioneer@example.com>")
        service, index, mirror, session = _setup(
            server_folders={
                "INBOX": {1: ("<pioneer@example.com>", frozenset(), raw)},
                # no Archive folder on the server yet
            },
        )
        service.sync()

        # TUI-style archive: move mirror bytes to Archive (creates the
        # mirror dir via _ensure_folder_dirs) and shift the index row.
        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=inbox)
        row = rows[0]
        from pony.domain import MessageRef
        new_mirror_ref = mirror.move_message_to_folder(
            message_ref=MessageRef(
                account_name="personal",
                folder_name="INBOX",
                message_id=row.storage_key,
            ),
            target_folder="Archive",
        )
        new_row = dataclasses.replace(
            row,
            message_ref=dataclasses.replace(
                row.message_ref, folder_name="Archive",
            ),
            storage_key=new_mirror_ref.message_id,
            uid=None,
            server_flags=frozenset(),
            extra_imap_flags=frozenset(),
            synced_at=None,
        )
        with index.connection():
            index.delete_message(message_ref=row.message_ref)
            index.upsert_message(message=new_row)

        service.sync()

        self.assertIn("Archive", session.created_folders)
        self.assertIn(
            ("INBOX", 1, "Archive"), session.moves,
            "Expected UID MOVE after the CREATE",
        )
        self.assertIn("Archive", session.folders)
        self.assertEqual(len(session.folders["Archive"]), 1)
        # CREATE must precede the MOVE in the session call log — otherwise
        # the fake raises OSError on the MOVE.  Verify the ordering directly.
        create_idx = session.call_log.index("create:Archive")
        move_idx = session.call_log.index("move:INBOX->Archive:1")
        self.assertLess(
            create_idx, move_idx,
            f"CREATE must precede MOVE but got {session.call_log!r}",
        )

    def test_tui_created_folder_propagates_upstream(self) -> None:
        """Creating an empty folder in the mirror results in a server CREATE
        on the next sync, even with no messages to push."""
        service, _, mirror, session = _setup(
            server_folders={"INBOX": {}},
        )
        service.sync()  # establish baseline

        mirror.create_folder(
            account_name="personal", folder_name="Projects",
        )
        service.sync()

        self.assertIn("Projects", session.created_folders)
        self.assertIn("Projects", session.folders)

    def test_create_folder_skipped_when_server_already_has_it(self) -> None:
        """If the server already has the folder, no CREATE is issued."""
        service, _, mirror, session = _setup(
            server_folders={"INBOX": {}, "Archive": {}},
        )
        service.sync()

        # Create the folder locally — it's already on the server too.
        mirror.create_folder(
            account_name="personal", folder_name="Archive",
        )
        service.sync()

        self.assertNotIn("Archive", session.created_folders)

    def test_read_only_source_suppresses_push_move(self) -> None:
        raw = _make_raw_message("Kept", "<kept@example.com>")
        # Set up with a read-only INBOX.
        tmp = TMP_ROOT / "sync" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        mirror = MaildirMirrorRepository(
            account_name="personal", root_dir=tmp / "mirror",
        )
        index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
        index.initialize()

        account = AccountConfig(
            name="personal",
            email_address="bob@example.com",
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            username="bob",
            credentials_source="plaintext",
            mirror=MirrorConfig(path=tmp / "mirror", format="maildir"),
            folders=FolderConfig(read_only=("INBOX",)),
        )
        config = AppConfig(accounts=(account,))
        session = FakeImapSession(folders={
            "INBOX": {1: ("<kept@example.com>", frozenset(), raw)},
            "Archive": {},
        })

        class _Creds:
            def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
                return "pw"

        service = ImapSyncService(
            config=config,
            mirror_factory=lambda _acc: mirror,
            index=index,
            credentials=_Creds(),
            session_factory=lambda _acc, _pw: session,
        )
        service.sync()

        # Simulate the "archive" at the index level.  The TUI refuses this
        # for a read-only source, but the planner must also be safe.
        ref = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=ref)
        row = rows[0]
        new_ref = dataclasses.replace(row.message_ref, folder_name="Archive")
        with index.connection():
            index.delete_message(message_ref=row.message_ref)
            index.upsert_message(
                message=dataclasses.replace(
                    row, message_ref=new_ref, uid=None,
                    server_flags=frozenset(),
                    extra_imap_flags=frozenset(), synced_at=None,
                )
            )

        service.sync()

        # INBOX is read-only → no PushMoveOp; server-side INBOX intact.
        self.assertFalse(session.moves)
        self.assertIn(1, session.folders["INBOX"])
