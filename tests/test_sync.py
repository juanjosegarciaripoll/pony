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
    FolderQuickStatus,
    FolderRef,
    MessageFlag,
    MessageRef,
    MessageStatus,
    MirrorConfig,
    SmtpConfig,
)
from pony.index_store import SqliteIndexRepository
from pony.message_projection import project_rfc822_message
from pony.protocols import ImapClientSession, MirrorRepository
from pony.storage import MaildirMirrorRepository
from pony.sync import ImapSyncService, format_plan_detail

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
        *,
        condstore: bool = True,
    ) -> None:
        self.folders = folders
        self.uid_validity = uid_validity
        self.condstore = condstore
        self._selected: str = ""
        self.stored_flags: list[tuple[int, frozenset[MessageFlag]]] = []
        self.stored_extra: list[tuple[int, frozenset[str]]] = []
        self.extra_flags: dict[tuple[str, int], frozenset[str]] = {}
        self.deleted_uids: list[int] = []
        # None = folder-wide EXPUNGE, a frozenset = UID-scoped.
        self.expunged_scopes: list[frozenset[int] | None] = []
        self.moves: list[tuple[str, int, str]] = []
        self.created_folders: list[str] = []
        # Ordered log of side-effecting calls (create, move, append, ...)
        # so tests can assert on sequencing — e.g. CREATE before MOVE.
        self.call_log: list[str] = []
        # UIDNEXT per folder — advances on APPEND/MOVE, not on delete.
        # Initialize one past the highest seeded UID so fast-path STATUS
        # compares correctly after an initial sync.
        self._uidnext: dict[str, int] = {
            name: max(f.keys(), default=0) + 1 for name, f in folders.items()
        }
        # HIGHESTMODSEQ per folder + per-(folder,uid) MODSEQ — advanced
        # on every mutation observable through IMAP (APPEND, STORE,
        # EXPUNGE, MOVE).  Starts at 1 so the initial sync sees a
        # non-zero watermark it can store.
        self._highest_modseq: dict[str, int] = dict.fromkeys(folders, 1)
        self._uid_modseq: dict[tuple[str, int], int] = {
            (name, uid): 1 for name, f in folders.items() for uid in f
        }
        # Count of FETCH metadata scans — tests assert the fast-path
        # keeps this at zero when STATUS matches.
        self.scan_count: int = 0
        # Count of cheap CHANGEDSINCE fetches — medium-path indicator.
        self.changedsince_count: int = 0
        # Every UID whose body was downloaded, in order.  A UIDVALIDITY
        # change used to re-download a whole folder of mail that was
        # already on disk, and nothing but this could show it.
        self.body_fetches: list[int] = []

    def _bump_modseq(self, folder_name: str, uid: int) -> None:
        """Advance the folder's HIGHESTMODSEQ and stamp it on *uid*."""
        new = self._highest_modseq.get(folder_name, 0) + 1
        self._highest_modseq[folder_name] = new
        self._uid_modseq[(folder_name, uid)] = new

    def list_folders(self) -> Sequence[str]:
        return list(self.folders.keys())

    def get_uid_validity(self, folder_name: str) -> int:
        self._selected = folder_name
        return self.uid_validity

    def folder_quick_status(self, folder_name: str) -> FolderQuickStatus:
        folder = self.folders.get(folder_name, {})
        return FolderQuickStatus(
            uid_validity=self.uid_validity,
            uidnext=self._uidnext.get(folder_name, 1),
            messages=len(folder),
            highest_modseq=(
                self._highest_modseq.get(folder_name, 0) if self.condstore else None
            ),
        )

    def fetch_uid_to_message_id(
        self, folder_name: str
    ) -> dict[int, tuple[str, tuple[frozenset[MessageFlag], frozenset[str]]]]:
        self.scan_count += 1
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

    def fetch_flags_changed_since(
        self,
        folder_name: str,
        modseq: int,
    ) -> dict[int, tuple[frozenset[MessageFlag], frozenset[str]]]:
        self.changedsince_count += 1
        folder = self.folders.get(folder_name, {})
        result: dict[int, tuple[frozenset[MessageFlag], frozenset[str]]] = {}
        for uid, (_, flags, _) in folder.items():
            if self._uid_modseq.get((folder_name, uid), 0) > modseq:
                extra = self.extra_flags.get((folder_name, uid), frozenset())
                result[uid] = (flags, extra)
        return result

    def fetch_messages_batch(
        self,
        folder_name: str,
        uids: Sequence[int],
    ) -> dict[int, bytes]:
        folder = self.folders.get(folder_name, {})
        self.body_fetches.extend(uid for uid in uids if uid in folder)
        return {uid: folder[uid][2] for uid in uids if uid in folder}

    def fetch_message_bytes(self, folder_name: str, uid: int) -> bytes:
        folder = self.folders.get(folder_name, {})
        if uid not in folder:
            raise KeyError(f"UID {uid} not found in {folder_name!r}")
        self.body_fetches.append(uid)
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
        self._bump_modseq(folder_name, uid)

    def append_message(
        self,
        folder_name: str,
        raw_message: bytes,
        flags: frozenset[MessageFlag],
        extra_imap_flags: frozenset[str] = frozenset(),
    ) -> int | None:
        self.call_log.append(f"append:{folder_name}")
        # Mirror real IMAP: APPEND to a non-existent mailbox is an error.
        if folder_name not in self.folders:
            raise OSError(f"APPEND target folder does not exist: {folder_name!r}")
        folder = self.folders[folder_name]
        new_uid = max(folder.keys(), default=0) + 1000
        # Parse Message-ID from raw for test assertions.
        from email import policy as _policy
        from email.parser import BytesParser

        msg = BytesParser(policy=_policy.default).parsebytes(raw_message)
        mid = msg.get("Message-ID", "")
        folder[new_uid] = (mid, flags, raw_message)
        self.extra_flags[(folder_name, new_uid)] = extra_imap_flags
        self._uidnext[folder_name] = max(
            self._uidnext.get(folder_name, 1),
            new_uid + 1,
        )
        self._bump_modseq(folder_name, new_uid)
        # Simulate APPENDUID (RFC 4315) — real servers usually advertise
        # UIDPLUS, so the planner's fast path is what tests should
        # exercise.  Tests that need the no-UIDPLUS branch can subclass
        # and override.
        return new_uid

    def mark_deleted(self, folder_name: str, uid: int) -> None:
        self.deleted_uids.append(uid)
        self._bump_modseq(folder_name, uid)

    def expunge(
        self,
        folder_name: str,
        *,
        uids: Sequence[int] | None = None,
    ) -> None:
        folder = self.folders.get(folder_name, {})
        scope = set(uids) if uids else None
        self.expunged_scopes.append(None if scope is None else frozenset(scope))
        for uid in list(self.deleted_uids):
            if scope is not None and uid not in scope:
                continue
            if folder.pop(uid, None) is not None:
                self._bump_modseq(folder_name, uid)

    def move_message(
        self,
        source_folder: str,
        uid: int,
        target_folder: str,
    ) -> int | None:
        # Record the call so tests can assert PushMoveOp executed.
        self.call_log.append(f"move:{source_folder}->{target_folder}:{uid}")
        self.moves.append((source_folder, uid, target_folder))
        # Mirror real IMAP: MOVE to a non-existent mailbox is an error.
        # This turns "forgot to CREATE before MOVE" into a test failure.
        if target_folder not in self.folders:
            raise OSError(f"MOVE target folder does not exist: {target_folder!r}")
        source = self.folders.get(source_folder, {})
        entry = source.pop(uid, None)
        if entry is None:
            return None
        target = self.folders[target_folder]
        new_uid = max(target.keys(), default=0) + 2000
        target[new_uid] = entry
        self._uidnext[target_folder] = max(
            self._uidnext.get(target_folder, 1),
            new_uid + 1,
        )
        self._bump_modseq(source_folder, uid)
        self._bump_modseq(target_folder, new_uid)
        # Carry over custom flags when present.
        extra = self.extra_flags.pop((source_folder, uid), frozenset())
        if extra:
            self.extra_flags[(target_folder, new_uid)] = extra
        # Simulate COPYUID — UIDPLUS is universal on modern IMAP stacks.
        return new_uid

    def create_folder(self, folder_name: str) -> None:
        self.call_log.append(f"create:{folder_name}")
        self.created_folders.append(folder_name)
        self.folders.setdefault(folder_name, {})
        self._uidnext.setdefault(folder_name, 1)
        self._highest_modseq.setdefault(folder_name, 1)

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
    date: str = "Fri, 10 Apr 2026 10:00:00 +0000",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = date
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
        smtp=SmtpConfig(host="smtp.example.com"),
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


class SyncedMessageRetrievalTestCase(unittest.TestCase):
    """After sync, bytes must be retrievable via the IndexedMessage.

    Regression test for the mirror identity bug: storage methods take a
    MessageRef but look up files by ``message_ref.message_id``, while for
    IMAP-synced messages that field holds the RFC 5322 Message-ID
    (``<xxx@host>``).  The real maildir filename lives on
    ``IndexedMessage.storage_key``.  Without the fix, every public
    retrieval path (pony message body, MCP get_message_body, pony rescan)
    raises KeyError and silently fails.
    """

    def test_body_retrievable_via_indexed_ref(self) -> None:
        raw = _make_raw_message(
            "Benceno probe",
            # Deliberately shaped like a real RFC 5322 Message-ID that no
            # maildir filename could ever match.
            "<not-a-filename@example.com>",
        )
        service, index, mirror, _ = _setup(
            server_folders={
                "INBOX": {
                    1: ("<not-a-filename@example.com>", frozenset(), raw),
                }
            }
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        # Sanity: the two identifiers really are different — otherwise
        # this test would pass by the same accident that masked the bug.
        self.assertNotEqual(row.message_id, row.storage_key)

        # The public retrieval contract: the mirror takes a FolderRef +
        # storage_key pulled from the IndexedMessage.  This is what
        # run_rescan, run_message_body, and MCP get_message_body all do.
        payload = mirror.get_message_bytes(
            folder=folder,
            storage_key=row.storage_key,
        )
        self.assertEqual(payload, raw)


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
        mids = {r.message_id for r in rows}
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
        self.assertEqual(rows[0].message_id, "<hi@example.com>")
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

        # Server marks as SEEN between syncs.  Also deliver a second
        # message so UIDNEXT / MESSAGES bump, forcing the planner off
        # the STATUS fast-path and into the full flag-reconciliation
        # slow path.
        session.folders["INBOX"][1] = (
            "<phone@example.com>",
            frozenset({MessageFlag.SEEN}),
            raw,
        )
        raw2 = _make_raw_message("Second", "<second@example.com>")
        session.folders["INBOX"][2] = ("<second@example.com>", frozenset(), raw2)
        session._uidnext["INBOX"] = 3
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        phone = next(r for r in rows if r.message_id == "<phone@example.com>")
        self.assertEqual(phone.local_flags, frozenset({MessageFlag.SEEN}))

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
            rows[0],
            local_flags=frozenset({MessageFlag.FLAGGED}),
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

        # Server marks SEEN; local marks FLAGGED independently.  Deliver
        # a fresh message too so UIDNEXT bumps and the planner runs the
        # full slow-path flag reconciliation — the fast path assumes
        # server flags have not changed when STATUS is stable.
        session.folders["INBOX"][1] = (
            "<both@example.com>",
            frozenset({MessageFlag.SEEN}),
            raw,
        )
        raw2 = _make_raw_message("Other", "<other@example.com>")
        session.folders["INBOX"][2] = ("<other@example.com>", frozenset(), raw2)
        session._uidnext["INBOX"] = 3
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)

        updated = dataclasses.replace(
            rows[0],
            local_flags=frozenset({MessageFlag.FLAGGED}),
        )
        index.upsert_message(message=updated)

        service.sync()

        rows = index.list_folder_messages(folder=folder)
        both = next(r for r in rows if r.message_id == "<both@example.com>")
        self.assertIn(MessageFlag.SEEN, both.local_flags)
        self.assertIn(MessageFlag.FLAGGED, both.local_flags)
        self.assertEqual(
            session.folders["INBOX"][1][1],
            frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
        )


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

    def test_server_deleted_row_retained_until_trash_cleanup(self) -> None:
        raw = _make_raw_message("Retain me", "<retain@example.com>")
        service, index, mirror, session = _setup(
            server_folders={"INBOX": {1: ("<retain@example.com>", frozenset(), raw)}}
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        del session.folders["INBOX"][1]
        service.sync()
        service.sync()

        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].local_status, MessageStatus.TRASHED)
        self.assertIn(row.storage_key, mirror.list_messages(folder=folder))


class ServerMoveTestCase(unittest.TestCase):
    """Server-side moves are detected as delete-on-source + fetch-on-target.

    The new identity model has no cross-folder Message-ID tracking.  A
    UID disappearing from one folder and a (different) UID appearing
    in another with the same Message-ID are two independent events:
    the source row is trashed; the target gets a fresh row.  The body
    is re-downloaded once — acceptable for the rare server-side move
    case (e.g. someone moved it via webmail).
    """

    def test_server_side_cross_folder_move_is_delete_plus_fetch(self) -> None:
        raw = _make_raw_message("Move me", "<move@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<move@example.com>", frozenset(), raw)},
                "Archive": {},
            }
        )
        service.sync()  # ingest into INBOX

        # Server-side move: UID disappears from INBOX, appears in Archive.
        del session.folders["INBOX"][1]
        session.folders["Archive"][2] = ("<move@example.com>", frozenset(), raw)
        service.sync()

        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        archive = FolderRef(account_name="personal", folder_name="Archive")
        inbox_active = [
            r
            for r in index.list_folder_messages(folder=inbox)
            if r.local_status == MessageStatus.ACTIVE
        ]
        archive_active = [
            r
            for r in index.list_folder_messages(folder=archive)
            if r.local_status == MessageStatus.ACTIVE
        ]

        # Source row trashed (deferred-destroy retention keeps it for
        # the user to recover); target row fetched fresh.
        self.assertEqual(inbox_active, [])
        self.assertEqual(len(archive_active), 1)
        self.assertEqual(archive_active[0].message_id, "<move@example.com>")
        self.assertEqual(archive_active[0].uid, 2)


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
        plan = service.plan()

        # Planning is read-only; the stale row is cleared only during execution.
        rows_after_plan = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows_after_plan), 1)
        self.assertEqual(rows_after_plan[0].uid, 1)

        service.execute(
            plan,
            confirmed_folders=frozenset(
                (a.account_name, f.folder_name)
                for a in plan.accounts
                for f in a.folders
            ),
        )

        # Message still present in index, now with new UID.
        rows_after = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows_after), 1)
        self.assertEqual(rows_after[0].uid, 10)
        mids = {r.message_id for r in rows_after}
        self.assertIn("<reset@example.com>", mids)


class MissingMessageIdTestCase(unittest.TestCase):
    """Messages without a Message-ID header are accepted with empty mid."""

    def test_missing_message_id_is_empty_string(self) -> None:
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
        # Identity is the row id; an empty message_id is fine and does
        # not block ingestion or push-back.
        self.assertEqual(rows[0].message_id, "")
        self.assertGreater(rows[0].message_ref.id, 0)

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

    def test_push_delete_removes_mirror_file(self) -> None:
        """After PushDeleteOp runs, the message must be gone from the mirror.

        Regression test for the mirror identity bug: before the
        ``storage_key``-based protocol, ``mirror.delete_message`` received
        the index's ``MessageRef`` whose ``message_id`` was the RFC 5322
        header, not the storage key — the delete silently no-opped and
        the mirror grew unbounded.
        """
        raw = _make_raw_message("Erase", "<erase@example.com>")
        service, index, mirror, _ = _setup(
            server_folders={"INBOX": {1: ("<erase@example.com>", frozenset(), raw)}}
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        self.assertIn(
            row.storage_key,
            mirror.list_messages(folder=folder),
            "pre-condition: message must be visible in the mirror",
        )

        index.upsert_message(
            message=dataclasses.replace(row, local_status=MessageStatus.TRASHED)
        )
        service.sync()

        self.assertNotIn(
            row.storage_key,
            mirror.list_messages(folder=folder),
            "PushDeleteOp must remove the message from the mirror",
        )

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
        index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
        index.initialize()

        account = AccountConfig(
            name="personal",
            email_address="bob@example.com",
            imap_host="imap.example.com",
            smtp=SmtpConfig(host="smtp.example.com"),
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
    """Duplicate Message-ID within and across folders is no longer special."""

    def test_duplicate_mid_in_folder_creates_two_rows(self) -> None:
        """Two UIDs with the same Message-ID in one folder yield two rows.

        Identity is per-row, not per-(folder, mid).  Both rows share
        the same display ``message_id``; their ``id`` and ``uid``
        differ.  No synthetic IDs anywhere — that workaround is gone.
        """
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
        # Both rows expose the real Message-ID — no synthetics.
        self.assertEqual(
            {r.message_id for r in rows},
            {"<dup@example.com>"},
        )
        # Identities differ on row id and on UID.
        self.assertEqual(len({r.message_ref.id for r in rows}), 2)
        self.assertEqual({r.uid for r in rows}, {1, 2})

    def test_no_append_loop_on_duplicate_mid(self) -> None:
        """Regression: a folder with two same-mid UIDs converges in one sync.

        The previous synthetic-mid workaround caused a non-converging
        APPEND loop — every sync re-APPENDed the synthetic-id rows.
        With per-row identity, the second sync emits zero ops.
        """
        raw1 = _make_raw_message("First", "<dup@example.com>")
        raw2 = _make_raw_message("Second", "<dup@example.com>")
        service, _, _, session = _setup(
            server_folders={
                "INBOX": {
                    1: ("<dup@example.com>", frozenset(), raw1),
                    2: ("<dup@example.com>", frozenset(), raw2),
                }
            }
        )
        service.sync()
        appends_before = [c for c in session.call_log if c.startswith("append:")]
        service.sync()
        self.assertEqual(
            [c for c in session.call_log if c.startswith("append:")],
            appends_before,
            "second sync must not APPEND anything (no synthetic-mid loop)",
        )

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

        merged = _merge_flags(
            local=frozenset({MessageFlag.SEEN}),
            base=frozenset(),
            remote=frozenset({MessageFlag.DELETED}),
        )
        self.assertNotIn(MessageFlag.DELETED, merged)
        self.assertIn(MessageFlag.SEEN, merged)

    def test_convergent_changes_union_cleanly(self) -> None:
        """3c: when both sides set the same flag, union still yields just that flag."""
        from pony.sync import _merge_flags

        merged = _merge_flags(
            local=frozenset({MessageFlag.SEEN}),
            base=frozenset(),
            remote=frozenset({MessageFlag.SEEN}),
        )
        self.assertEqual(merged, frozenset({MessageFlag.SEEN}))

    def test_divergent_changes_union(self) -> None:
        """When sides set different flags, the merge is the union of both."""
        from pony.sync import _merge_flags

        merged = _merge_flags(
            local=frozenset({MessageFlag.FLAGGED}),
            base=frozenset(),
            remote=frozenset({MessageFlag.SEEN}),
        )
        self.assertEqual(merged, frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}))


class BaseFlagsComparisonTestCase(unittest.TestCase):
    """3d: flag reconciliation must use base_flags from messages table."""

    def test_base_flags_diverged_from_server_flags(self) -> None:
        """When base_flags and server_flags disagree, base_flags governs."""
        raw = _make_raw_message("Diverge", "<div@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<div@example.com>", frozenset({MessageFlag.SEEN}), raw)}
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
        # → PullFlagsOp.  Also deliver a new message so UIDNEXT bumps
        # and the planner takes the full slow path (the STATUS fast-path
        # cannot detect pure server-side flag drift).
        raw2 = _make_raw_message("Second", "<second@example.com>")
        session.folders["INBOX"][2] = ("<second@example.com>", frozenset(), raw2)
        session._uidnext["INBOX"] = 3

        service.sync()

        rows = index.list_folder_messages(folder=folder)
        div = next(r for r in rows if r.message_id == "<div@example.com>")
        self.assertIn(MessageFlag.SEEN, div.local_flags)


class FetchFailureTestCase(unittest.TestCase):
    """5b: per-message fetch failure must not abort the entire folder."""

    def test_bad_fetch_skipped_good_messages_ingested(self) -> None:
        raw_good = _make_raw_message("Good", "<good@example.com>")

        class _FailOnUid2(FakeImapSession):
            def fetch_messages_batch(
                self,
                folder_name: str,
                uids: Sequence[int],
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
        index_repo = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
        index_repo.initialize()

        account = AccountConfig(
            name="personal",
            email_address="bob@example.com",
            imap_host="imap.example.com",
            smtp=SmtpConfig(host="smtp.example.com"),
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
        mids = {r.message_id for r in rows}
        # UID 1 and 3 should have been ingested; UID 2 skipped.
        self.assertIn("<good@example.com>", mids)
        self.assertIn("<also-good@example.com>", mids)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(
            index_repo.get_folder_sync_state(
                account_name="personal",
                folder_name="INBOX",
            )
        )


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
        mids = {r.message_id for r in rows}
        self.assertIn("<good2@example.com>", mids)
        self.assertNotIn("<empty@example.com>", mids)


class ExtraImapFlagsTestCase(unittest.TestCase):
    """3b: custom/unknown IMAP flags are preserved through STORE operations."""

    def test_extra_flags_preserved_on_push(self) -> None:
        """When pushing local flag changes, extra server flags are included."""
        raw = _make_raw_message("Custom flags", "<custom@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<custom@example.com>", frozenset(), raw)}}
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
            server_folders={"INBOX": {1: ("<c1-nomod@example.com>", frozenset(), raw)}}
        )
        service.sync()

        del session.folders["INBOX"][1]
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(rows[0].local_status, MessageStatus.TRASHED)


class C2RestoreTestCase(unittest.TestCase):
    """C-2: local-trash + server-flag-change → restore, cancel deletion."""

    def test_trashed_message_deleted_even_when_server_flags_changed(self) -> None:
        """A locally-trashed message is deleted from the server even if the
        server updated its flags between the local trash and the sync.  The
        user's explicit delete intent takes priority over any flag changes."""
        raw = _make_raw_message("Delete me", "<c2@example.com>")
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

        # Server marks SEEN between syncs.  Deliver a fresh message
        # too so UIDNEXT bumps and the planner runs the full slow-path.
        session.folders["INBOX"][1] = (
            "<c2@example.com>",
            frozenset({MessageFlag.SEEN}),
            raw,
        )
        raw2 = _make_raw_message("Other", "<other@example.com>")
        session.folders["INBOX"][2] = ("<other@example.com>", frozenset(), raw2)
        session._uidnext["INBOX"] = 3
        service.sync()

        # Message must be expunged from the server, not restored.
        self.assertIn(1, session.deleted_uids)

    def test_trashed_message_deleted_when_server_flags_unchanged(self) -> None:
        """When server didn't change flags, normal push-delete applies."""
        raw = _make_raw_message("Really delete", "<c2-nochg@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<c2-nochg@example.com>", frozenset(), raw)}}
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
        service, index, _, session = _setup(server_folders={"INBOX": dict(raws)})
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
        service, index, _, session = _setup(server_folders={"INBOX": dict(raws)})
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
        service, index, _, session = _setup(server_folders={"INBOX": dict(raws)})
        service.sync()

        for uid in range(1, 6):
            del session.folders["INBOX"][uid]

        plan = service.plan()
        # Execute without confirming.
        result = service.execute(plan, confirmed_folders=frozenset())
        # Folder should be in skipped.
        self.assertIn("INBOX", result.accounts[0].skipped_folders)

    def test_plan_records_pending_delete_stats(self) -> None:
        """Flagged folder records delete count + total for UI rendering."""
        raws: dict[int, tuple[str, frozenset[MessageFlag], bytes]] = {
            uid: (
                f"<stat{uid}@example.com>",
                frozenset(),
                _make_raw_message(f"Msg {uid}", f"<stat{uid}@example.com>"),
            )
            for uid in range(1, 11)
        }
        service, _, _, session = _setup(server_folders={"INBOX": dict(raws)})
        service.sync()

        for uid in range(1, 6):
            del session.folders["INBOX"][uid]

        plan = service.plan()
        folder_plan = plan.accounts[0].folders[0]
        self.assertEqual(folder_plan.pending_delete_count, 5)
        self.assertEqual(folder_plan.pending_delete_total, 10)
        self.assertEqual(
            plan.folders_needing_confirmation(),
            frozenset({("personal", "INBOX")}),
        )

    def test_format_plan_detail_shows_confirm_stats(self) -> None:
        """format_plan_detail emits an explicit [CONFIRM: N of M (Z%)] suffix."""
        from pony.sync import format_plan_detail

        raws: dict[int, tuple[str, frozenset[MessageFlag], bytes]] = {
            uid: (
                f"<fmt{uid}@example.com>",
                frozenset(),
                _make_raw_message(f"Msg {uid}", f"<fmt{uid}@example.com>"),
            )
            for uid in range(1, 11)
        }
        service, _, _, session = _setup(server_folders={"INBOX": dict(raws)})
        service.sync()
        for uid in range(1, 6):
            del session.folders["INBOX"][uid]
        plan = service.plan()
        text = format_plan_detail(plan)
        self.assertIn("[CONFIRM: would delete 5 of 10 (50%)]", text)

    def test_a_confirmation_for_one_account_is_not_one_for_another(self) -> None:
        """`INBOX` is not a key: every account has one.

        The halted set was folder names alone, and it was handed back to
        `execute` as the halts the user had waived -- so answering for
        one account's INBOX applied the other's mass deletion too, in
        the CLI, the dialog and the browser alike.
        """
        raws: dict[int, tuple[str, frozenset[MessageFlag], bytes]] = {
            uid: (
                f"<cross{uid}@example.com>",
                frozenset(),
                _make_raw_message(f"Msg {uid}", f"<cross{uid}@example.com>"),
            )
            for uid in range(1, 11)
        }
        service, _, _, session = _setup(server_folders={"INBOX": dict(raws)})
        service.sync()
        for uid in range(1, 6):
            del session.folders["INBOX"][uid]

        plan = service.plan()
        self.assertEqual(
            plan.folders_needing_confirmation(),
            frozenset({("personal", "INBOX")}),
        )
        # Somebody else's INBOX, answered: this one is still held.
        result = service.execute(
            plan,
            confirmed_folders=frozenset({("work", "INBOX")}),
        )

        self.assertIn("INBOX", result.accounts[0].skipped_folders)
        # This one's own, answered: the same plan goes through.
        applied = service.execute(
            plan,
            confirmed_folders=frozenset({("personal", "INBOX")}),
        )
        self.assertNotIn("INBOX", applied.accounts[0].skipped_folders)

    def test_confirmed_folder_executes_deletes(self) -> None:
        """Passing the flagged folder via confirmed_folders applies deletes."""
        raws: dict[int, tuple[str, frozenset[MessageFlag], bytes]] = {
            uid: (
                f"<conf{uid}@example.com>",
                frozenset(),
                _make_raw_message(f"Msg {uid}", f"<conf{uid}@example.com>"),
            )
            for uid in range(1, 11)
        }
        service, index, _, session = _setup(server_folders={"INBOX": dict(raws)})
        service.sync()
        for uid in range(1, 6):
            del session.folders["INBOX"][uid]

        plan = service.plan()
        result = service.execute(
            plan, confirmed_folders=plan.folders_needing_confirmation()
        )
        self.assertNotIn("INBOX", result.accounts[0].skipped_folders)

        # After re-planning the folder should no longer need confirmation.
        plan2 = service.plan()
        self.assertEqual(plan2.folders_needing_confirmation(), frozenset())


class TrashGcTestCase(unittest.TestCase):
    """4d: trash GC purges expired trashed messages."""

    def test_expired_trash_purge_removes_mirror_file(self) -> None:
        """The retention-based purge must remove the message from the mirror.

        Regression test for the mirror identity bug: the purge loop passed
        the index's MessageRef (RFC 5322 id) to ``mirror.delete_message``,
        which looked up files by ``message_id`` and silently failed.  The
        index row was purged but the file lingered forever.
        """
        raw = _make_raw_message("Aged trash", "<aged@example.com>")
        service, index, mirror, session = _setup(
            server_folders={"INBOX": {1: ("<aged@example.com>", frozenset(), raw)}}
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        self.assertIn(row.storage_key, mirror.list_messages(folder=folder))

        from datetime import timedelta

        index.upsert_message(
            message=dataclasses.replace(
                row,
                local_status=MessageStatus.TRASHED,
                trashed_at=row.received_at - timedelta(days=60),
            )
        )
        del session.folders["INBOX"][1]
        service.sync()

        self.assertNotIn(
            row.storage_key,
            mirror.list_messages(folder=folder),
            "expired-trash purge must remove the message from the mirror",
        )

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
            server_folders={"INBOX": {1: ("<old-id@example.com>", frozenset(), raw)}}
        )
        service.sync()

        # Server re-imports the message under a new Message-ID but same UID.
        # Also deliver a fresh message so UIDNEXT bumps and the planner
        # takes the full slow path — the STATUS fast-path sees matching
        # UIDVALIDITY / UIDNEXT / MESSAGES and cannot detect a pure
        # header-level rewrite.
        raw2 = _make_raw_message("Reimported", "<new-id@example.com>")
        session.folders["INBOX"][1] = (
            "<new-id@example.com>",
            frozenset({MessageFlag.SEEN}),
            raw2,
        )
        raw3 = _make_raw_message("Third", "<third@example.com>")
        session.folders["INBOX"][2] = ("<third@example.com>", frozenset(), raw3)
        session._uidnext["INBOX"] = 3
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

    def test_an_account_missing_from_the_config_is_left_alone(self) -> None:
        """Rows of an account the config does not declare survive a sync."""
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
            id=0,
        )
        index.insert_message(
            message=IndexedMessage(
                message_ref=stale_ref,
                message_id="<orphan@example.com>",
                sender="x",
                recipients="y",
                cc="",
                subject="orphan",
                body_text="",
                storage_key="",
                local_flags=frozenset(),
                base_flags=frozenset(),
                local_status=MessageStatus.ACTIVE,
                received_at=datetime.now(tz=UTC),
            )
        )
        self.assertIn("removed-account", index.list_indexed_accounts())

        # A second sync must leave it alone, and say how to remove it.
        with self.assertLogs("pony.sync", level="WARNING") as captured:
            service.sync()
        self.assertIn("removed-account", index.list_indexed_accounts())
        self.assertTrue(
            any(
                "pony reset --account removed-account" in line
                for line in captured.output
            ),
            captured.output,
        )

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

    def test_planning_alone_does_not_drop_stale_folder_state(self) -> None:
        """A plan that is never executed leaves the index untouched."""
        raw = _make_raw_message("Active", "<active2@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<active2@example.com>", frozenset(), raw)},
                "Doomed": {2: ("<doomed@example.com>", frozenset(), raw)},
            }
        )
        service.sync()
        del session.folders["Doomed"]

        service.plan()  # planned, never executed

        states = index.list_folder_sync_states(account_name="personal")
        self.assertIn("Doomed", {s.folder_name for s in states})

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
        """Simulate the TUI ``A`` action at the index level.

        In-place update: same row, new folder, ``PENDING_MOVE`` status
        with ``source_folder`` / ``source_uid`` recording the server
        handle.  No delete + reinsert.
        """
        ref = FolderRef(account_name="personal", folder_name=source)
        rows = index.list_folder_messages(folder=ref)
        row = next(r for r in rows if r.message_id == message_id)
        new_ref = dataclasses.replace(
            row.message_ref,
            folder_name=target,
        )
        new_row = dataclasses.replace(
            row,
            message_ref=new_ref,
            uid=None,
            server_flags=frozenset(),
            extra_imap_flags=frozenset(),
            synced_at=None,
            local_status=MessageStatus.PENDING_MOVE,
            source_folder=source,
            source_uid=row.uid,
        )
        index.update_message(message=new_row)

    def test_archive_uses_uid_move(self) -> None:
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

        # One sync: PushMoveOp runs UID MOVE; the row's uid is set
        # from the COPYUID response in the same op.
        service.sync()

        self.assertIn(
            ("INBOX", 1, "Archive"),
            session.moves,
            "Expected UID MOVE from INBOX to Archive",
        )
        # Server-side: INBOX now empty, Archive has the message.
        self.assertNotIn(1, session.folders["INBOX"])
        self.assertEqual(len(session.folders["Archive"]), 1)
        new_uid = next(iter(session.folders["Archive"]))

        archive = FolderRef(account_name="personal", folder_name="Archive")
        rows = index.list_folder_messages(folder=archive)
        self.assertEqual(len(rows), 1)
        # UID was recovered via COPYUID — no second sync needed.
        self.assertEqual(rows[0].uid, new_uid)
        self.assertEqual(rows[0].local_status, MessageStatus.ACTIVE)
        self.assertIsNone(rows[0].source_folder)
        self.assertIsNone(rows[0].source_uid)
        self.assertEqual(rows[0].message_id, "<archive@example.com>")

        # A repeat sync must be a no-op (no second MOVE, no APPEND).
        moves_before = list(session.moves)
        appends_before = [c for c in session.call_log if c.startswith("append:")]
        service.sync()
        self.assertEqual(session.moves, moves_before)
        self.assertEqual(
            [c for c in session.call_log if c.startswith("append:")],
            appends_before,
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
        new_storage_key = mirror.move_message_to_folder(
            folder=FolderRef(account_name="personal", folder_name="INBOX"),
            storage_key=storage_key,
            target_folder="Archive",
        )
        updated = dataclasses.replace(
            archive_rows[0],
            storage_key=new_storage_key,
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

        # TUI-style archive: move mirror bytes to Archive and update the
        # index row in place (PENDING_MOVE with source pointer).
        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=inbox)
        row = rows[0]
        new_storage_key = mirror.move_message_to_folder(
            folder=inbox,
            storage_key=row.storage_key,
            target_folder="Archive",
        )
        new_row = dataclasses.replace(
            row,
            message_ref=dataclasses.replace(
                row.message_ref,
                folder_name="Archive",
            ),
            storage_key=new_storage_key,
            uid=None,
            server_flags=frozenset(),
            extra_imap_flags=frozenset(),
            synced_at=None,
            local_status=MessageStatus.PENDING_MOVE,
            source_folder="INBOX",
            source_uid=row.uid,
        )
        index.update_message(message=new_row)

        service.sync()

        self.assertIn("Archive", session.created_folders)
        self.assertIn(
            ("INBOX", 1, "Archive"),
            session.moves,
            "Expected UID MOVE after the CREATE",
        )
        self.assertIn("Archive", session.folders)
        self.assertEqual(len(session.folders["Archive"]), 1)
        # CREATE must precede the MOVE in the session call log — otherwise
        # the fake raises OSError on the MOVE.  Verify the ordering directly.
        create_idx = session.call_log.index("create:Archive")
        move_idx = session.call_log.index("move:INBOX->Archive:1")
        self.assertLess(
            create_idx,
            move_idx,
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
            account_name="personal",
            folder_name="Projects",
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
            account_name="personal",
            folder_name="Archive",
        )
        service.sync()

        self.assertNotIn("Archive", session.created_folders)

    def test_brand_new_folder_create_and_append_in_one_sync(self) -> None:
        """A pending row in a local-only folder is CREATEd and APPENDed in
        the same sync — the create runs first, APPEND follows in the
        brand-new folder's own plan."""
        raw = _make_raw_message("Local", "<local@example.com>")
        service, index, mirror, session = _setup(
            server_folders={"INBOX": {}},
        )
        service.sync()  # baseline

        # Stage a purely local message in a purely local folder: no
        # server-side copy and the archive folder doesn't exist server-side.
        inbox_ref = FolderRef(account_name="personal", folder_name="INBOX")
        storage_key = mirror.store_message(folder=inbox_ref, raw_message=raw)
        new_storage_key = mirror.move_message_to_folder(
            folder=inbox_ref,
            storage_key=storage_key,
            target_folder="Archive",
        )
        from datetime import UTC, datetime

        from pony.domain import (
            IndexedMessage,
            MessageRef,
            MessageStatus,
        )

        index.insert_message(
            message=IndexedMessage(
                message_ref=MessageRef(
                    account_name="personal",
                    folder_name="Archive",
                    id=0,
                ),
                message_id="<local@example.com>",
                sender="alice@example.com",
                recipients="bob@example.com",
                cc="",
                subject="Local",
                body_text="",
                storage_key=new_storage_key,
                local_flags=frozenset(),
                base_flags=frozenset(),
                local_status=MessageStatus.ACTIVE,
                received_at=datetime.now(tz=UTC),
                uid=None,
            )
        )

        service.sync()

        # Single sync: Archive CREATEd, message APPENDed.
        self.assertIn("Archive", session.created_folders)
        self.assertIn("Archive", session.folders)
        self.assertEqual(len(session.folders["Archive"]), 1)
        (_uid, (mid, _flags, appended)) = next(iter(session.folders["Archive"].items()))
        self.assertEqual(mid, "<local@example.com>")
        self.assertEqual(appended, raw)
        # CREATE must precede APPEND — otherwise the fake raises OSError.
        create_idx = session.call_log.index("create:Archive")
        append_idx = session.call_log.index("append:Archive")
        self.assertLess(
            create_idx,
            append_idx,
            f"CREATE must precede APPEND but got {session.call_log!r}",
        )

    def test_read_only_source_suppresses_push_move(self) -> None:
        raw = _make_raw_message("Kept", "<kept@example.com>")
        # Set up with a read-only INBOX.
        tmp = TMP_ROOT / "sync" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        mirror = MaildirMirrorRepository(
            account_name="personal",
            root_dir=tmp / "mirror",
        )
        index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
        index.initialize()

        account = AccountConfig(
            name="personal",
            email_address="bob@example.com",
            imap_host="imap.example.com",
            smtp=SmtpConfig(host="smtp.example.com"),
            username="bob",
            credentials_source="plaintext",
            mirror=MirrorConfig(path=tmp / "mirror", format="maildir"),
            folders=FolderConfig(read_only=("INBOX",)),
        )
        config = AppConfig(accounts=(account,))
        session = FakeImapSession(
            folders={
                "INBOX": {1: ("<kept@example.com>", frozenset(), raw)},
                "Archive": {},
            }
        )

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
                    row,
                    message_ref=new_ref,
                    uid=None,
                    server_flags=frozenset(),
                    extra_imap_flags=frozenset(),
                    synced_at=None,
                )
            )

        service.sync()

        # INBOX is read-only → no PushMoveOp; server-side INBOX intact.
        self.assertFalse(session.moves)
        self.assertIn(1, session.folders["INBOX"])

    def test_a_move_the_server_already_made_becomes_an_append(self) -> None:
        """The interrupted-move recovery arm, and what reaches it.

        "Is this PENDING_MOVE row pushable?" is asked twice: once to
        decide which source folders the planner fetches a remote UID
        view for, and once to decide which rows emit an op.  The second
        reads that view to notice that ``source_uid`` is gone from the
        server -- the move happened and the pass died before the row was
        updated -- and appends to the target instead.  If the two
        answers drift, no view is gathered, the arm never fires, and the
        row waits for a MOVE of a message that is not there.
        """
        from pony.sync import PushAppendOp, PushMoveOp

        raw = _make_raw_message("Half moved", "<halfmoved@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {1: ("<halfmoved@example.com>", frozenset(), raw)},
                "Archive": {},
            },
        )
        service.sync()
        self._archive_locally(
            index,
            source="INBOX",
            target="Archive",
            message_id="<halfmoved@example.com>",
        )
        # The server performed the move; the pass that asked for it did
        # not live to update the row.
        del session.folders["INBOX"][1]

        plan = service.plan()
        ops = [op for fp in plan.accounts[0].folders for op in fp.ops]
        self.assertEqual(
            [type(op).__name__ for op in ops if isinstance(op, PushAppendOp)],
            ["PushAppendOp"],
        )
        self.assertFalse([op for op in ops if isinstance(op, PushMoveOp)])


class FastPathTestCase(unittest.TestCase):
    """STATUS-based fast-path gates the full FETCH metadata scan.

    When ``UIDVALIDITY / UIDNEXT / MESSAGES`` all match the stored
    sync watermark and local row count, the planner skips
    ``fetch_uid_to_message_id`` entirely.  These tests verify the gate
    fires when it should and falls through to the slow path when the
    server state has actually diverged.
    """

    def test_unchanged_folder_skips_full_scan(self) -> None:
        raw = _make_raw_message("Hello", "<msg@example.com>")
        service, _, _, session = _setup(
            server_folders={"INBOX": {1: ("<msg@example.com>", frozenset(), raw)}}
        )

        service.sync()
        self.assertEqual(session.scan_count, 1)  # initial ingest

        service.sync()
        # Second sync: STATUS matches → FETCH not reissued.
        self.assertEqual(session.scan_count, 1)

    def test_new_server_message_falls_through(self) -> None:
        raw1 = _make_raw_message("First", "<m1@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<m1@example.com>", frozenset(), raw1)}}
        )
        service.sync()
        self.assertEqual(session.scan_count, 1)

        # New server-side message bumps UIDNEXT and MESSAGES.
        raw2 = _make_raw_message("Second", "<m2@example.com>")
        session.folders["INBOX"][2] = ("<m2@example.com>", frozenset(), raw2)
        session._uidnext["INBOX"] = 3

        service.sync()
        # Fast-path misses → slow path runs → new message ingested.
        self.assertEqual(session.scan_count, 2)
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        mids = {r.message_id for r in rows}
        self.assertIn("<m2@example.com>", mids)

    def test_server_deletion_falls_through(self) -> None:
        raw1 = _make_raw_message("Keep", "<keep@example.com>")
        raw2 = _make_raw_message("Gone", "<gone@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {
                    1: ("<keep@example.com>", frozenset(), raw1),
                    2: ("<gone@example.com>", frozenset(), raw2),
                }
            }
        )
        service.sync()
        self.assertEqual(session.scan_count, 1)

        # Server deletes one — MESSAGES count drops; UIDNEXT stays put.
        del session.folders["INBOX"][2]

        service.sync()
        self.assertEqual(session.scan_count, 2)
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        gone = [r for r in rows if r.message_id == "<gone@example.com>"]
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0].local_status, MessageStatus.TRASHED)

    def test_fast_path_pushes_pending_append(self) -> None:
        """A pending uid=NULL row is APPENDed on the fast path — no FETCH."""
        from datetime import UTC, datetime

        from pony.domain import (
            IndexedMessage as _IndexedMessage,
        )
        from pony.domain import (
            MessageRef as _MessageRef,
        )

        service, index, mirror, session = _setup(server_folders={"INBOX": {}})
        service.sync()  # baseline: empty INBOX
        self.assertEqual(session.scan_count, 1)

        # Stage a purely local message under INBOX (uid=NULL, no server copy).
        raw = _make_raw_message("Local only", "<local@example.com>")
        inbox_ref = FolderRef(account_name="personal", folder_name="INBOX")
        storage_key = mirror.store_message(folder=inbox_ref, raw_message=raw)
        index.insert_message(
            message=_IndexedMessage(
                message_ref=_MessageRef(
                    account_name="personal",
                    folder_name="INBOX",
                    id=0,
                ),
                message_id="<local@example.com>",
                sender="alice@example.com",
                recipients="bob@example.com",
                cc="",
                subject="Local only",
                body_text="",
                storage_key=storage_key,
                local_flags=frozenset(),
                base_flags=frozenset(),
                local_status=MessageStatus.ACTIVE,
                received_at=datetime.now(tz=UTC),
                uid=None,
            )
        )

        result = service.sync()
        # Fast path: no metadata scan, APPEND still fires.
        self.assertEqual(session.scan_count, 1)
        self.assertTrue(
            any(call == "append:INBOX" for call in session.call_log),
            f"expected append to INBOX, got {session.call_log!r}",
        )
        self.assertEqual(len(session.folders["INBOX"]), 1)
        appended = sum(f.appended_to_server for a in result.accounts for f in a.folders)
        self.assertEqual(appended, 1)

    def test_fast_path_pushes_flag_drift(self) -> None:
        """Local flag drift pushes via fast path — no FETCH."""
        raw = _make_raw_message("Flag", "<f@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<f@example.com>", frozenset(), raw)}}
        )
        service.sync()  # baseline
        self.assertEqual(session.scan_count, 1)

        # User flips FLAGGED locally (local_flags != base_flags).
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(
                row,
                local_flags=frozenset({MessageFlag.FLAGGED}),
            )
        )

        service.sync()
        # Fast path: no metadata scan; PushFlagsOp executed.
        self.assertEqual(session.scan_count, 1)
        self.assertTrue(
            any(MessageFlag.FLAGGED in flags for _uid, flags in session.stored_flags),
            f"expected FLAGGED push, got {session.stored_flags!r}",
        )

    def test_fast_path_pushes_trashed_delete(self) -> None:
        """A locally-trashed row is EXPUNGEd on the fast path — no FETCH."""
        raw = _make_raw_message("Toss", "<t@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<t@example.com>", frozenset(), raw)}}
        )
        service.sync()  # baseline
        self.assertEqual(session.scan_count, 1)

        # User trashes the message locally.
        from datetime import UTC, datetime

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(
                row,
                local_status=MessageStatus.TRASHED,
                trashed_at=datetime.now(tz=UTC),
            )
        )

        service.sync()
        # Fast path: no metadata scan; STORE \\Deleted + EXPUNGE ran.
        self.assertEqual(session.scan_count, 1)
        self.assertIn(1, session.deleted_uids)
        self.assertEqual(len(session.folders["INBOX"]), 0)

    def test_condstore_medium_path_pulls_server_flag_change(self) -> None:
        """Phase 2: CONDSTORE medium path pulls a silent server flag change.

        Another client marks SEEN on the server.  UIDNEXT and MESSAGES
        don't change, but HIGHESTMODSEQ does — gate flips to medium
        path, issues CHANGEDSINCE, Step 3 pulls the new flag.
        """
        raw = _make_raw_message("Phone", "<p@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<p@example.com>", frozenset(), raw)}}
        )
        service.sync()

        # Server mutates flags silently — use store_flags so the fake
        # bumps its HIGHESTMODSEQ the way a real server would.
        session.store_flags("INBOX", 1, frozenset({MessageFlag.SEEN}))
        service.sync()

        # Medium path: no metadata scan, one CHANGEDSINCE fetch; SEEN pulled.
        self.assertEqual(session.scan_count, 1)
        self.assertEqual(session.changedsince_count, 1)
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        self.assertIn(MessageFlag.SEEN, row.local_flags)

    def test_condstore_fast_path_when_nothing_changed(self) -> None:
        """Modseq match + UID set stable → fast path, no FETCH at all."""
        raw = _make_raw_message("Still", "<s@example.com>")
        service, _, _, session = _setup(
            server_folders={"INBOX": {1: ("<s@example.com>", frozenset(), raw)}}
        )
        service.sync()
        self.assertEqual(session.scan_count, 1)

        service.sync()
        # Second sync: no changes anywhere → fast path, no FETCH.
        self.assertEqual(session.scan_count, 1)
        self.assertEqual(session.changedsince_count, 0)

    def test_condstore_medium_path_merges_concurrent_flag_changes(self) -> None:
        """Phase 2 closes Phase 1 limitation: concurrent flag changes merge.

        User flips FLAGGED locally, server silently flips SEEN.  Medium
        path's CHANGEDSINCE returns the server change; Step 3 sees both
        changes and emits MergeFlagsOp with the union.
        """
        raw = _make_raw_message("Both", "<b@example.com>")
        service, index, _, session = _setup(
            server_folders={"INBOX": {1: ("<b@example.com>", frozenset(), raw)}}
        )
        service.sync()

        # Server: silent SEEN (bumps HIGHESTMODSEQ).
        session.store_flags("INBOX", 1, frozenset({MessageFlag.SEEN}))

        # Local: user flips FLAGGED.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(
                row,
                local_flags=frozenset({MessageFlag.FLAGGED}),
            )
        )

        service.sync()

        # Medium path: CHANGEDSINCE ran, full metadata fetch skipped.
        self.assertEqual(session.scan_count, 1)
        self.assertEqual(session.changedsince_count, 1)
        [row2] = index.list_folder_messages(folder=folder)
        self.assertIn(MessageFlag.SEEN, row2.local_flags)
        self.assertIn(MessageFlag.FLAGGED, row2.local_flags)
        self.assertEqual(
            session.folders["INBOX"][1][1],
            frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
        )

    def test_burned_uids_converge_to_fast_path(self) -> None:
        """Gate compares server UIDNEXT — not ``max_surviving_uid + 1``.

        Some servers (Dovecot with sieve, Gmail, LMTP-heavy setups)
        advance ``UIDNEXT`` for messages that never land as survivors:
        delivered then immediately moved / expunged.  Before the fix,
        the gate compared ``quick.uidnext == stored.highest_uid + 1``,
        so the first slow-path sync recorded ``highest_uid = max
        surviving`` and every subsequent sync saw
        ``uidnext > max_surviving + 1`` → slow path forever.  The fix
        is to record server ``UIDNEXT`` as the watermark and compare
        exactly.
        """
        raw = _make_raw_message("Kept", "<kept@example.com>")
        service, _, _, session = _setup(
            server_folders={"INBOX": {1: ("<kept@example.com>", frozenset(), raw)}}
        )
        service.sync()  # sync 1: initial ingest; stored.uidnext = 2
        self.assertEqual(session.scan_count, 1)

        # Simulate a burned UID server-side: UIDNEXT bumps but MESSAGES /
        # HIGHESTMODSEQ don't (message delivered then removed before we
        # ever observed it).
        session._uidnext["INBOX"] = 5

        service.sync()  # sync 2: uidnext mismatch → slow path, records 5
        self.assertEqual(session.scan_count, 2)

        service.sync()  # sync 3: uidnext matches (5 == 5) → fast path
        self.assertEqual(session.scan_count, 2)

    def test_no_condstore_keeps_phase1_behavior(self) -> None:
        """Servers without CONDSTORE still take Phase 1 fast path (limitation).

        ``folder_quick_status`` returns ``highest_modseq=None`` when
        the server doesn't advertise CONDSTORE.  The gate then treats
        the modseq condition as satisfied (can't improve on STATUS),
        so silent server flag changes are deferred just like before.
        """
        raw = _make_raw_message("NoCS", "<nc@example.com>")
        tmp = TMP_ROOT / "sync-no-condstore" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        mirror = MaildirMirrorRepository(
            account_name="personal",
            root_dir=tmp / "mirror",
        )
        index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
        index.initialize()
        account = AccountConfig(
            name="personal",
            email_address="bob@example.com",
            imap_host="imap.example.com",
            smtp=SmtpConfig(host="smtp.example.com"),
            username="bob",
            credentials_source="plaintext",
            mirror=MirrorConfig(path=tmp / "mirror", format="maildir"),
        )
        config = AppConfig(accounts=(account,))
        session = FakeImapSession(
            folders={"INBOX": {1: ("<nc@example.com>", frozenset(), raw)}},
            condstore=False,
        )

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

        # Silent server flag change.
        session.store_flags("INBOX", 1, frozenset({MessageFlag.SEEN}))
        service.sync()

        # No CONDSTORE → fast path fires → SEEN not pulled.
        self.assertEqual(session.scan_count, 1)
        self.assertEqual(session.changedsince_count, 0)
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        self.assertNotIn(MessageFlag.SEEN, row.local_flags)


class DedupSyncPropagationTestCase(unittest.TestCase):
    """Marking a duplicate TRASHED causes sync to expunge it from the server."""

    def test_dedup_trash_propagates_via_push_delete(self) -> None:
        """A row trashed by the dedup tool is expunged on the next sync.

        This verifies the full pipeline:
          1. Two server UIDs with the same Message-ID are downloaded.
          2. One is marked TRASHED locally (simulating ``pony folder dedup --apply``).
          3. The next sync emits STORE +FLAGS \\Deleted + EXPUNGE for that UID.
        """
        raw = _make_raw_message("Dup body", "<dedup-prop@example.com>")
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {
                    1: ("<dedup-prop@example.com>", frozenset(), raw),
                    2: ("<dedup-prop@example.com>", frozenset(), raw),
                }
            }
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 2)

        # Simulate the dedup tool: mark UID 2 as TRASHED.
        loser = next(r for r in rows if r.uid == 2)
        from datetime import UTC, datetime

        index.upsert_message(
            message=dataclasses.replace(
                loser,
                local_status=MessageStatus.TRASHED,
                trashed_at=datetime.now(tz=UTC),
            )
        )

        # Second sync should expunge UID 2.
        service.sync()

        self.assertIn(2, session.deleted_uids, "sync must mark UID 2 deleted")
        surviving = index.list_folder_messages(folder=folder)
        uids = {r.uid for r in surviving if r.uid is not None}
        self.assertNotIn(2, uids, "UID 2 row must be gone from the index")
        self.assertIn(1, uids, "UID 1 must survive")


# ---------------------------------------------------------------------------
# SyncPlan utility method tests
# ---------------------------------------------------------------------------


class SyncPlanUtilityTestCase(unittest.TestCase):
    """Tests for SyncPlan methods and format helpers."""

    from pony.domain import MessageRef
    from pony.sync import (
        AccountSyncPlan,
        FetchNewOp,
        FolderSyncPlan,
        MergeFlagsOp,
        PullFlagsOp,
        PurgeLocalOp,
        PushAppendOp,
        PushDeleteOp,
        PushFlagsOp,
        PushMoveOp,
        RestoreOp,
        ReUploadOp,
        ServerDeleteOp,
        SyncPlan,
        UidValidityResetOp,
        _categorize_ops,
        format_plan_detail,
        format_plan_summary,
    )

    def _ref(self, id_: int = 1) -> MessageRef:
        from pony.domain import MessageRef

        return MessageRef(account_name="acct", folder_name="INBOX", id=id_)

    def _empty_folder(self, name: str = "INBOX") -> FolderSyncPlan:
        from pony.sync import FolderSyncPlan

        return FolderSyncPlan(folder_name=name, uid_validity=1, highest_uid=1, ops=())

    def test_is_empty_true_for_empty_plan(self) -> None:
        from pony.sync import AccountSyncPlan, FolderSyncPlan, SyncPlan

        folder = FolderSyncPlan(
            folder_name="INBOX", uid_validity=1, highest_uid=0, ops=()
        )
        acct = AccountSyncPlan(account_name="acct", folders=(folder,))
        plan = SyncPlan(accounts=(acct,))
        self.assertTrue(plan.is_empty())

    def test_is_empty_false_when_creates(self) -> None:
        from pony.sync import AccountSyncPlan, SyncPlan

        acct = AccountSyncPlan(account_name="acct", folders=(), creates=("Archive",))
        plan = SyncPlan(accounts=(acct,))
        self.assertFalse(plan.is_empty())

    def test_is_empty_false_when_ops(self) -> None:
        from pony.sync import AccountSyncPlan, FetchNewOp, FolderSyncPlan, SyncPlan

        op = FetchNewOp(uid=1, message_id="<x@x>", server_flags=frozenset())
        folder = FolderSyncPlan(
            folder_name="INBOX", uid_validity=1, highest_uid=1, ops=(op,)
        )
        acct = AccountSyncPlan(account_name="acct", folders=(folder,))
        plan = SyncPlan(accounts=(acct,))
        self.assertFalse(plan.is_empty())

    def test_count_ops_for_fetch(self) -> None:
        from pony.sync import AccountSyncPlan, FetchNewOp, FolderSyncPlan, SyncPlan

        op = FetchNewOp(uid=1, message_id="<x@x>", server_flags=frozenset())
        folder = FolderSyncPlan(
            folder_name="INBOX", uid_validity=1, highest_uid=1, ops=(op,)
        )
        acct = AccountSyncPlan(account_name="acct", folders=(folder,))
        plan = SyncPlan(accounts=(acct,))
        self.assertEqual(plan.count_ops(FetchNewOp), 1)
        from pony.sync import ServerDeleteOp

        self.assertEqual(plan.count_ops(ServerDeleteOp), 0)

    def test_categorize_restore_op(self) -> None:
        from pony.sync import RestoreOp, _categorize_ops

        op = RestoreOp(message_ref=self._ref())
        counts = _categorize_ops((op,))
        self.assertIn("restore", counts)
        self.assertEqual(counts["restore"], 1)

    def test_categorize_reupload_op(self) -> None:
        from pony.sync import ReUploadOp, _categorize_ops

        op = ReUploadOp(message_ref=self._ref(), local_flags=frozenset())
        counts = _categorize_ops((op,))
        self.assertIn("reupload", counts)

    def test_categorize_purge_op(self) -> None:
        from pony.sync import PurgeLocalOp, _categorize_ops

        op = PurgeLocalOp(message_ref=self._ref(), storage_key="key1")
        counts = _categorize_ops((op,))
        self.assertIn("purge", counts)

    def test_categorize_uidvalidity_reset_op(self) -> None:
        from pony.sync import UidValidityResetOp, _categorize_ops

        op = UidValidityResetOp()
        counts = _categorize_ops((op,))
        self.assertIn("uidvalidity_reset", counts)

    def test_format_plan_detail_with_skipped_folders(self) -> None:
        from pony.sync import AccountSyncPlan, SyncPlan, format_plan_detail

        acct = AccountSyncPlan(
            account_name="acct",
            folders=(),
            skipped_folders=("Spam", "Trash"),
        )
        plan = SyncPlan(accounts=(acct,))
        detail = format_plan_detail(plan)
        self.assertIn("skipped", detail)
        self.assertIn("Spam", detail)

    def test_format_plan_detail_with_creates(self) -> None:
        from pony.sync import AccountSyncPlan, SyncPlan, format_plan_detail

        acct = AccountSyncPlan(
            account_name="acct",
            folders=(),
            creates=("Archive",),
        )
        plan = SyncPlan(accounts=(acct,))
        detail = format_plan_detail(plan)
        self.assertIn("create folder", detail)
        self.assertIn("Archive", detail)

    def test_format_plan_detail_new_folder(self) -> None:
        from pony.sync import (
            AccountSyncPlan,
            FolderSyncPlan,
            SyncPlan,
            format_plan_detail,
        )

        folder = FolderSyncPlan(
            folder_name="NewInbox",
            uid_validity=1,
            highest_uid=0,
            ops=(),
            is_new=True,
        )
        acct = AccountSyncPlan(account_name="acct", folders=(folder,))
        plan = SyncPlan(accounts=(acct,))
        detail = format_plan_detail(plan)
        self.assertIn("new folder", detail)

    def test_format_plan_detail_with_confirmation_needed(self) -> None:
        from pony.sync import (
            AccountSyncPlan,
            FolderSyncPlan,
            ServerDeleteOp,
            SyncPlan,
            format_plan_detail,
        )

        op = ServerDeleteOp(uid=1, message_ref=self._ref())
        folder = FolderSyncPlan(
            folder_name="INBOX",
            uid_validity=1,
            highest_uid=10,
            ops=(op,),
            needs_confirmation=True,
            pending_delete_count=5,
            pending_delete_total=10,
        )
        acct = AccountSyncPlan(account_name="acct", folders=(folder,))
        plan = SyncPlan(accounts=(acct,))
        detail = format_plan_detail(plan)
        self.assertIn("CONFIRM", detail)

    def test_format_plan_summary_with_ops(self) -> None:
        from pony.sync import (
            AccountSyncPlan,
            FetchNewOp,
            FolderSyncPlan,
            SyncPlan,
            format_plan_summary,
        )

        op = FetchNewOp(uid=1, message_id="<x@x>", server_flags=frozenset())
        folder = FolderSyncPlan(
            folder_name="INBOX", uid_validity=1, highest_uid=1, ops=(op,)
        )
        acct = AccountSyncPlan(account_name="acct", folders=(folder,))
        plan = SyncPlan(accounts=(acct,))
        summary = format_plan_summary(plan)
        self.assertIn("download", summary)

    def test_categorize_push_move_op(self) -> None:
        from pony.sync import PushMoveOp, _categorize_ops

        op = PushMoveOp(
            message_ref=self._ref(),
            source_folder="INBOX",
            source_uid=1,
            target_folder="Archive",
        )
        counts = _categorize_ops((op,))
        self.assertIn("push_move", counts)

    def test_categorize_push_append_op(self) -> None:
        from pony.sync import PushAppendOp, _categorize_ops

        op = PushAppendOp(message_ref=self._ref())
        counts = _categorize_ops((op,))
        self.assertIn("push_append", counts)

    def test_categorize_push_delete_op(self) -> None:
        from pony.sync import PushDeleteOp, _categorize_ops

        op = PushDeleteOp(message_ref=self._ref(), server_uid=1, storage_key="k")
        counts = _categorize_ops((op,))
        self.assertIn("push_delete", counts)

    def test_categorize_pull_flags_op(self) -> None:
        from pony.sync import PullFlagsOp, _categorize_ops

        op = PullFlagsOp(uid=1, message_ref=self._ref(), new_flags=frozenset())
        counts = _categorize_ops((op,))
        self.assertIn("pull_flags", counts)

    def test_categorize_push_flags_op(self) -> None:
        from pony.sync import PushFlagsOp, _categorize_ops

        op = PushFlagsOp(uid=1, message_ref=self._ref(), new_flags=frozenset())
        counts = _categorize_ops((op,))
        self.assertIn("push_flags", counts)

    def test_categorize_merge_flags_op(self) -> None:
        from pony.sync import MergeFlagsOp, _categorize_ops

        op = MergeFlagsOp(
            uid=1,
            message_ref=self._ref(),
            merged_flags=frozenset(),
            push_to_server=True,
        )
        counts = _categorize_ops((op,))
        self.assertIn("merge_flags", counts)

    def test_categorize_unknown_op_uses_other_key(self) -> None:
        from unittest.mock import MagicMock

        from pony.sync import _categorize_ops

        # Create a mock object that's not a known op type
        unknown_op = MagicMock(spec=[])
        counts = _categorize_ops((unknown_op,))
        self.assertIn("other", counts)

    def test_format_op_counts_includes_other(self) -> None:
        from pony.sync import _format_op_counts

        counts = {"other": 3}
        parts = _format_op_counts(counts)
        self.assertTrue(any("other" in p for p in parts))

    def test_format_plan_detail_empty_folder_no_ops_no_new(self) -> None:
        """format_plan_detail skips output when folder has no ops and is_new=False."""
        from pony.sync import (
            AccountSyncPlan,
            FolderSyncPlan,
            SyncPlan,
            format_plan_detail,
        )

        folder = FolderSyncPlan(
            folder_name="INBOX",
            uid_validity=1,
            highest_uid=0,
            ops=(),
            is_new=False,
        )
        acct = AccountSyncPlan(account_name="acct", folders=(folder,))
        plan = SyncPlan(accounts=(acct,))
        detail = format_plan_detail(plan)
        # With no ops and not new, the folder line is not added but the header is
        self.assertIn("acct", detail)

    def test_format_plan_detail_needs_confirmation_no_total(self) -> None:
        """Format confirms when needs_confirmation=True but pending_delete_total=0."""
        from pony.domain import MessageRef
        from pony.sync import (
            AccountSyncPlan,
            FolderSyncPlan,
            ServerDeleteOp,
            SyncPlan,
            format_plan_detail,
        )

        op = ServerDeleteOp(
            uid=1,
            message_ref=MessageRef(account_name="acct", folder_name="INBOX", id=1),
        )
        folder = FolderSyncPlan(
            folder_name="INBOX",
            uid_validity=1,
            highest_uid=10,
            ops=(op,),
            needs_confirmation=True,
            pending_delete_count=0,
            pending_delete_total=0,  # no total → "needs confirmation" text
        )
        acct = AccountSyncPlan(account_name="acct", folders=(folder,))
        plan = SyncPlan(accounts=(acct,))
        detail = format_plan_detail(plan)
        self.assertIn("needs confirmation", detail)


# ---------------------------------------------------------------------------
# Connection loss during planning
# ---------------------------------------------------------------------------


def _setup_multi_session(
    *,
    sessions: Sequence[FakeImapSession],
) -> tuple[ImapSyncService, SqliteIndexRepository, MaildirMirrorRepository, list[int]]:
    """Build a service whose session_factory hands out *sessions* in order.

    The last session is reused once the list is exhausted, so a test can
    model "first connection dies, every reconnect works".
    """
    tmp = TMP_ROOT / "sync" / uuid4().hex
    tmp.mkdir(parents=True, exist_ok=True)

    mirror = MaildirMirrorRepository(account_name="personal", root_dir=tmp / "mirror")
    index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
    index.initialize()

    account = AccountConfig(
        name="personal",
        email_address="bob@example.com",
        imap_host="imap.example.com",
        smtp=SmtpConfig(host="smtp.example.com"),
        username="bob",
        credentials_source="plaintext",
        mirror=MirrorConfig(path=tmp / "mirror", format="maildir"),
    )

    class _FixedCredentials:
        def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
            return "test-password"

    handed_out = [0]

    def _session_factory(_acc: AccountConfig, _password: str) -> ImapClientSession:
        idx = min(handed_out[0], len(sessions) - 1)
        handed_out[0] += 1
        return sessions[idx]

    service = ImapSyncService(
        config=AppConfig(accounts=(account,)),
        mirror_factory=lambda _acc: mirror,
        index=index,
        credentials=_FixedCredentials(),
        session_factory=_session_factory,
    )
    return service, index, mirror, handed_out


class _BrokenScanSession(FakeImapSession):
    """A session whose slow-path UID scan raises a connection error."""

    def fetch_uid_to_message_id(
        self,
        folder_name: str,  # noqa: ARG002
    ) -> dict[int, tuple[str, tuple[frozenset[MessageFlag], frozenset[str]]]]:
        raise OSError("connection reset by peer")


class PlanningConnectionLossTestCase(unittest.TestCase):
    """The planner degrades gracefully when the connection drops mid-scan."""

    def _folders(
        self,
    ) -> dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]]:
        return {
            "INBOX": {
                1: (
                    "<a@example.com>",
                    frozenset(),
                    _make_raw_message("A", "<a@example.com>"),
                ),
            }
        }

    def test_scan_failure_without_reconnect_skips_the_folder(self) -> None:
        """With no reconnect callable the folder is dropped from the plan."""
        service, _index, _mirror, _n = _setup_multi_session(
            sessions=[_BrokenScanSession(folders=self._folders())],
        )
        account = service._config.accounts[0]
        assert isinstance(account, AccountConfig)
        session = _BrokenScanSession(folders=self._folders())

        plan = service._plan_folders(account=account, session=session)

        assert plan.folders == ()

    def test_scan_failure_recovers_on_reconnect(self) -> None:
        """A dropped connection is retried once and the scan completes."""
        healthy = FakeImapSession(folders=self._folders())
        service, _index, _mirror, handed_out = _setup_multi_session(
            sessions=[_BrokenScanSession(folders=self._folders()), healthy],
        )

        plan = service.plan()

        assert handed_out[0] == 2  # initial connect + one reconnect
        folder_names = [f.folder_name for a in plan.accounts for f in a.folders]
        assert folder_names == ["INBOX"]

    def test_scan_failure_that_survives_reconnect_skips_the_folder(self) -> None:
        """When the retry fails too, the folder is skipped, not fatal."""
        service, _index, _mirror, handed_out = _setup_multi_session(
            sessions=[
                _BrokenScanSession(folders=self._folders()),
                _BrokenScanSession(folders=self._folders()),
            ],
        )

        plan = service.plan()

        assert handed_out[0] == 2
        assert all(a.folders == () for a in plan.accounts)

    def test_changedsince_failure_falls_through_to_the_slow_path(self) -> None:
        """A CONDSTORE fetch error degrades to a full UID scan."""
        service, index, _mirror, session = _setup(server_folders=self._folders())
        service.sync()

        # Drift the server flags so the UID set is stable but MODSEQ moved,
        # which is exactly the medium-path precondition.
        session.store_flags("INBOX", 1, frozenset({MessageFlag.SEEN}))

        def _boom(
            folder_name: str,  # noqa: ARG001
            modseq: int,  # noqa: ARG001
        ) -> dict[int, tuple[frozenset[MessageFlag], frozenset[str]]]:
            raise OSError("CONDSTORE unavailable")

        session.fetch_flags_changed_since = _boom  # type: ignore[method-assign]

        plan = service.plan()

        # The slow path still produced a usable plan for the folder.
        assert [f.folder_name for a in plan.accounts for f in a.folders] == ["INBOX"]
        rows = index.list_folder_messages(
            folder=FolderRef(account_name="personal", folder_name="INBOX")
        )
        assert len(rows) == 1

    def test_mirror_listing_failure_still_plans(self) -> None:
        """A mirror that cannot list folders yields no CREATE ops."""
        service, _index, mirror, _session = _setup(server_folders=self._folders())

        def _boom(*, account_name: str) -> Sequence[FolderRef]:  # noqa: ARG001
            raise OSError("mirror unreadable")

        mirror.list_folders = _boom  # type: ignore[method-assign]

        plan = service.plan()

        assert all(a.creates == () for a in plan.accounts)


class ExecuteFailureTestCase(unittest.TestCase):
    """A failing op is logged and skipped, never fatal to the run."""

    def _seeded(
        self,
    ) -> tuple[
        ImapSyncService,
        SqliteIndexRepository,
        MaildirMirrorRepository,
        FakeImapSession,
    ]:
        raw = _make_raw_message("Hi", "<hi@example.com>")
        return _setup(
            server_folders={"INBOX": {1: ("<hi@example.com>", frozenset(), raw)}}
        )

    def test_phase1_fetch_failure_is_skipped(self) -> None:
        """An index write blowing up during ingest does not abort the sync."""
        service, index, _mirror, _session = self._seeded()

        def _boom(*, message: object) -> object:  # noqa: ARG001
            raise OSError("index locked")

        index.insert_message = _boom  # type: ignore[method-assign]

        progress: list[str] = []
        plan = service.plan()
        service.execute(
            plan,
            confirmed_folders=frozenset({("personal", "INBOX")}),
            progress=lambda info: progress.append(info.message),
        )

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        assert len(index.list_folder_messages(folder=folder)) == 0
        assert progress  # the run still reported progress

    def test_phase2_push_failure_is_skipped(self) -> None:
        """A STORE that raises leaves the run alive and reports progress."""
        service, index, _mirror, session = self._seeded()
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        [row] = index.list_folder_messages(folder=folder)
        index.upsert_message(
            message=dataclasses.replace(
                row, local_flags=frozenset({MessageFlag.FLAGGED})
            )
        )

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("STORE rejected")

        session.store_flags = _boom  # type: ignore[method-assign]

        progress: list[str] = []
        plan = service.plan()
        result = service.execute(
            plan,
            confirmed_folders=frozenset({("personal", "INBOX")}),
            progress=lambda info: progress.append(info.message),
        )

        assert result is not None
        assert progress

    def test_trashed_row_without_uid_is_purged_locally(self) -> None:
        """A trashed row the server never saw is dropped from index + mirror."""
        from datetime import UTC, datetime

        from pony.domain import IndexedMessage, MessageRef

        service, index, mirror, _session = self._seeded()
        service.sync()

        # A message composed locally and trashed before it ever reached the
        # server: no UID, and no trashed_at to hold it for retention.
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        storage_key = mirror.store_message(
            folder=folder,
            raw_message=_make_raw_message("Local only", "<local@example.com>"),
        )
        index.insert_message(
            message=IndexedMessage(
                message_ref=MessageRef(
                    account_name="personal", folder_name="INBOX", id=0
                ),
                message_id="<local@example.com>",
                sender="bob@example.com",
                recipients="alice@example.com",
                cc="",
                subject="Local only",
                body_text="",
                storage_key=storage_key,
                local_flags=frozenset(),
                base_flags=frozenset(),
                local_status=MessageStatus.TRASHED,
                received_at=datetime.now(tz=UTC),
                uid=None,
                trashed_at=None,
            )
        )

        service.sync()

        remaining = index.list_folder_messages(folder=folder)
        assert all(r.message_id != "<local@example.com>" for r in remaining)
        assert storage_key not in mirror.list_messages(folder=folder)


class PlannerResilienceTestCase(unittest.TestCase):
    """One unhealthy folder must not take down the whole account sync.

    Both the STATUS probe and the per-folder plan are wrapped so that a
    server that fails on a single mailbox — a common state for shared or
    quota-exceeded folders — degrades to skipping that folder rather
    than aborting every other one.
    """

    def _folders(
        self,
    ) -> dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]]:
        return {
            "INBOX": {
                1: (
                    "<healthy@example.com>",
                    frozenset(),
                    _make_raw_message("Healthy", "<healthy@example.com>"),
                )
            },
            "Archive": {
                2: (
                    "<sick@example.com>",
                    frozenset(),
                    _make_raw_message("Sick", "<sick@example.com>"),
                )
            },
        }

    def test_a_folder_whose_status_probe_fails_is_skipped(self) -> None:
        service, index, _mirror, session = _setup(server_folders=self._folders())

        original = session.folder_quick_status

        def _status(folder_name: str) -> FolderQuickStatus:
            if folder_name == "Archive":
                raise OSError("STATUS failed on Archive")
            return original(folder_name)

        session.folder_quick_status = _status  # type: ignore[method-assign]

        result = service.sync()

        self.assertEqual(result.accounts[0].skipped_folders, ("Archive",))
        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        archive = FolderRef(account_name="personal", folder_name="Archive")
        self.assertEqual(len(index.list_folder_messages(folder=inbox)), 1)
        self.assertEqual(len(index.list_folder_messages(folder=archive)), 0)

    def test_a_folder_whose_scan_drops_the_connection_is_skipped(self) -> None:
        """A mid-scan drop is retried once on a fresh session, then given up on.

        The retry runs against the same fake, so it fails again — which is
        the case that matters: the folder is reported as skipped and every
        other folder still syncs.
        """
        service, index, _mirror, session = _setup(server_folders=self._folders())

        original = session.fetch_uid_to_message_id

        def _fetch(folder_name: str, *args: object, **kwargs: object) -> dict[int, str]:
            if folder_name == "Archive":
                raise OSError("connection reset scanning Archive")
            return original(folder_name, *args, **kwargs)  # type: ignore[arg-type]

        session.fetch_uid_to_message_id = _fetch  # type: ignore[method-assign]

        result = service.sync()

        self.assertEqual(result.accounts[0].skipped_folders, ("Archive",))
        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        archive = FolderRef(account_name="personal", folder_name="Archive")
        self.assertEqual(len(index.list_folder_messages(folder=inbox)), 1)
        self.assertEqual(len(index.list_folder_messages(folder=archive)), 0)


class ReadOnlyAppendSuppressionTestCase(unittest.TestCase):
    """A read-only folder must never receive an APPEND."""

    def test_a_local_only_row_is_not_appended_to_a_read_only_folder(self) -> None:
        """A draft saved into a read-only folder stays local, silently.

        ``uid IS NULL`` is the signal the planner uses to emit
        ``PushAppendOp``; the read-only guard has to win over it, or the
        sync would keep retrying an APPEND the server will always refuse.
        """
        from pony.domain import MessageRef
        from pony.message_projection import project_rfc822_message

        tmp = TMP_ROOT / "sync" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)

        mirror = MaildirMirrorRepository(
            account_name="personal", root_dir=tmp / "mirror"
        )
        index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
        index.initialize()

        account = AccountConfig(
            name="personal",
            email_address="bob@example.com",
            imap_host="imap.example.com",
            smtp=SmtpConfig(host="smtp.example.com"),
            username="bob",
            credentials_source="plaintext",
            mirror=MirrorConfig(path=tmp / "mirror", format="maildir"),
            folders=FolderConfig(read_only=("Archive",)),
        )
        config = AppConfig(accounts=(account,))
        session = FakeImapSession(folders={"INBOX": {}, "Archive": {}})

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

        # Save a local-only message into the read-only folder.
        folder = FolderRef(account_name="personal", folder_name="Archive")
        raw = _make_raw_message("Local draft", "<local-draft@example.com>")
        storage_key = mirror.store_message(folder=folder, raw_message=raw)
        index.insert_message(
            message=project_rfc822_message(
                message_ref=MessageRef(
                    account_name="personal", folder_name="Archive", id=0
                ),
                raw_message=raw,
                storage_key=storage_key,
            )
        )

        service.sync()

        # Nothing was uploaded, and the row survives locally.
        self.assertEqual(session.folders["Archive"], {})
        self.assertNotIn("append", " ".join(session.call_log).lower())
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].uid)


class UidValidityResetPreservesMailTestCase(unittest.TestCase):
    """A UIDVALIDITY change means the UIDs are meaningless, not the mail.

    The reset deleted every ACTIVE row and its mirror file. When the
    server folder had also been emptied — which is exactly what a restore
    from backup or a server migration looks like — that destroyed the
    user's only copy, and it did so without the confirmation that the
    same event triggers when UIDVALIDITY is unchanged.
    """

    folder = FolderRef(account_name="personal", folder_name="INBOX")

    def _ten(self) -> dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]]:
        return {
            "INBOX": {
                i: (
                    f"<m{i}@example.com>",
                    frozenset(),
                    _make_raw_message(f"m{i}", f"<m{i}@example.com>"),
                )
                for i in range(1, 11)
            }
        }

    def test_an_emptied_folder_does_not_lose_local_mail(self) -> None:
        service, index, mirror, session = _setup(server_folders=self._ten())
        service.sync()

        session.folders["INBOX"] = {}
        session.uid_validity = 2
        service.sync()

        self.assertEqual(len(index.list_folder_messages(folder=self.folder)), 10)
        self.assertEqual(len(mirror.list_messages(folder=self.folder)), 10)

    def test_the_same_messages_are_adopted_not_re_downloaded(self) -> None:
        service, index, mirror, session = _setup(server_folders=self._ten())
        service.sync()
        before = {
            r.message_ref.id for r in index.list_folder_messages(folder=self.folder)
        }

        session.uid_validity = 2
        service.sync()

        rows = index.list_folder_messages(folder=self.folder)
        self.assertEqual(len(rows), 10, "messages were duplicated")
        self.assertEqual(
            {r.message_ref.id for r in rows}, before, "rows were replaced, not adopted"
        )
        self.assertTrue(all(r.uid is not None for r in rows), "UIDs not re-adopted")

    def test_an_unpushed_local_flag_survives(self) -> None:
        service, index, _mirror, session = _setup(server_folders=self._ten())
        service.sync()
        row = index.list_folder_messages(folder=self.folder)[0]
        index.update_message(
            message=dataclasses.replace(
                row, local_flags=frozenset({MessageFlag.FLAGGED})
            )
        )

        session.uid_validity = 2
        service.sync()

        after = [
            r
            for r in index.list_folder_messages(folder=self.folder)
            if r.message_id == row.message_id
        ]
        self.assertEqual(len(after), 1)
        self.assertIn(MessageFlag.FLAGGED, after[0].local_flags)

    def test_a_pending_deletion_is_not_undone(self) -> None:
        """The row used to lose its UID and resurrect as a second, active row."""
        service, index, _mirror, session = _setup(server_folders=self._ten())
        service.sync()
        row = index.list_folder_messages(folder=self.folder)[0]
        index.update_message(
            message=dataclasses.replace(row, local_status=MessageStatus.TRASHED)
        )

        session.uid_validity = 2
        service.sync()

        same = [
            r
            for r in index.list_folder_messages(folder=self.folder)
            if r.message_id == row.message_id
        ]
        self.assertEqual(len(same), 1, "the message came back as a second row")
        self.assertEqual(same[0].local_status, MessageStatus.TRASHED)
        self.assertIsNotNone(same[0].uid, "no server handle, so the delete is stranded")

        # With a handle again, the next pass actually removes it.
        service.sync()
        remaining = {v[0] for v in session.folders["INBOX"].values()}
        self.assertNotIn(row.message_id, remaining)


class UidValidityResetCostTestCase(unittest.TestCase):
    """A reset re-numbers the mail; it does not have to re-download it.

    The planner emptied ``local_uids`` on a reset, so every UID the
    server reported counted as new and every body came down the wire --
    for a 15,000-message archive, the whole archive -- only for
    ``_adopt_existing_row`` to recognise each message on arrival and
    throw the bytes away.  The headers-only fetch the planner already
    does says which row each UID belongs to, so the pairing happens
    before anything is downloaded.
    """

    folder = FolderRef(account_name="personal", folder_name="INBOX")

    def _ten(
        self,
        *,
        first_uid: int = 1,
    ) -> dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]]:
        return {
            "INBOX": {
                first_uid + i: (
                    f"<m{i}@example.com>",
                    frozenset(),
                    _make_raw_message(f"m{i}", f"<m{i}@example.com>"),
                )
                for i in range(10)
            }
        }

    def test_a_reset_downloads_nothing_it_already_holds(self) -> None:
        service, index, _mirror, session = _setup(server_folders=self._ten())
        service.sync()
        before = {
            r.message_id: r.message_ref.id
            for r in index.list_folder_messages(folder=self.folder)
        }
        self.assertEqual(len(session.body_fetches), 10, "first pass downloads")

        # The server comes back as a restored backup does: same mail,
        # new epoch, new numbers.
        session.folders.update(self._ten(first_uid=500))
        session.uid_validity = 2
        session.body_fetches.clear()
        service.sync()

        self.assertEqual(session.body_fetches, [], "the folder was downloaded again")
        rows = index.list_folder_messages(folder=self.folder)
        self.assertEqual(len(rows), 10, "messages were duplicated")
        self.assertEqual(
            {r.message_id: r.message_ref.id for r in rows},
            before,
            "rows were replaced rather than re-keyed",
        )
        self.assertEqual(
            sorted(r.uid for r in rows if r.uid is not None),
            sorted(session.folders["INBOX"]),
            "the rows do not carry the server's new UIDs",
        )

    def test_the_pass_says_how_many_it_re_keyed(self) -> None:
        service, _index, _mirror, session = _setup(server_folders=self._ten())
        service.sync()

        session.folders.update(self._ten(first_uid=500))
        session.uid_validity = 2
        result = service.sync()

        folders = [
            f for a in result.accounts for f in a.folders if f.folder_name == "INBOX"
        ]
        self.assertEqual([f.rekeyed for f in folders], [10])
        self.assertEqual([f.fetched for f in folders], [0])

    def test_a_shared_message_id_is_downloaded_rather_than_guessed(self) -> None:
        """Two copies of one Message-ID name no row in particular.

        The planner will not pair them: it has only the headers, and a
        Message-ID shared by two UIDs says nothing about which row each
        belongs to.  They are fetched, and the fetch path decides with
        the whole message in hand.
        """
        raw = _make_raw_message("twice", "<twice@example.com>")
        folders: dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]] = {
            "INBOX": {
                1: ("<twice@example.com>", frozenset(), raw),
                2: ("<twice@example.com>", frozenset(), raw),
            }
        }
        service, index, _mirror, session = _setup(server_folders=folders)
        service.sync()

        session.uid_validity = 2
        session.body_fetches.clear()
        result = service.sync()

        self.assertEqual(sorted(session.body_fetches), [1, 2], "paired by guess")
        self.assertEqual(
            [
                f.rekeyed
                for a in result.accounts
                for f in a.folders
                if f.folder_name == "INBOX"
            ],
            [0],
            "nothing here is safe to re-key",
        )

    def test_identical_twins_are_taken_over_rather_than_duplicated(self) -> None:
        """A folder of two identical copies used to come back holding four.

        The planner leaves them to the fetch path, where each arriving
        message matched *both* UID-less rows on sender, subject and date
        -- so neither was taken and a third and fourth row were inserted
        beside them.  The next pass, no longer a reset, then APPENDed the
        two orphans: two copies on the server became four.
        """
        raw = _make_raw_message("twice", "<twice@example.com>")
        folders: dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]] = {
            "INBOX": {
                1: ("<twice@example.com>", frozenset(), raw),
                2: ("<twice@example.com>", frozenset(), raw),
            }
        }
        service, index, mirror, session = _setup(server_folders=folders)
        service.sync()
        before = {
            r.message_ref.id for r in index.list_folder_messages(folder=self.folder)
        }

        session.uid_validity = 2
        service.sync()

        rows = index.list_folder_messages(folder=self.folder)
        self.assertEqual(len(rows), 2, "the twins were duplicated")
        self.assertEqual(
            {r.message_ref.id for r in rows}, before, "rows were replaced, not taken"
        )
        self.assertEqual(sorted(r.uid for r in rows if r.uid is not None), [1, 2])
        self.assertEqual(len(mirror.list_messages(folder=self.folder)), 2)

        # And the pass after it has nothing to upload, which is where the
        # duplication used to reach the server.
        service.sync()
        self.assertEqual(len(session.folders["INBOX"]), 2, "orphans were uploaded")
        self.assertEqual(len(index.list_folder_messages(folder=self.folder)), 2)

    def test_three_identical_copies_each_take_their_own_row(self) -> None:
        """One claim per arriving message, not one row claimed three times."""
        raw = _make_raw_message("thrice", "<thrice@example.com>")
        folders: dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]] = {
            "INBOX": {
                uid: ("<thrice@example.com>", frozenset(), raw) for uid in (1, 2, 3)
            }
        }
        service, index, _mirror, session = _setup(server_folders=folders)
        service.sync()

        session.uid_validity = 2
        service.sync()

        rows = index.list_folder_messages(folder=self.folder)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(r.uid for r in rows if r.uid is not None), [1, 2, 3])

    def test_a_second_copy_arriving_normally_is_still_its_own_row(self) -> None:
        """Taking a row over must not collapse two server copies into one.

        The rows this claims are rows with no UID.  A message arriving
        beside one that already has a handle is new mail, however much
        it looks like what is already there.
        """
        raw = _make_raw_message("again", "<again@example.com>")
        folders: dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]] = {
            "INBOX": {1: ("<again@example.com>", frozenset(), raw)}
        }
        service, index, _mirror, session = _setup(server_folders=folders)
        service.sync()

        session.folders["INBOX"][2] = ("<again@example.com>", frozenset(), raw)
        service.sync()

        rows = index.list_folder_messages(folder=self.folder)
        self.assertEqual(len(rows), 2, "the second copy was swallowed")
        self.assertEqual(sorted(r.uid for r in rows if r.uid is not None), [1, 2])

    def test_a_message_with_no_message_id_is_downloaded(self) -> None:
        folders: dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]] = {
            "INBOX": {
                1: ("", frozenset(), _make_raw_message("anonymous", "")),
                2: (
                    "<named@example.com>",
                    frozenset(),
                    _make_raw_message("named", "<named@example.com>"),
                ),
            }
        }
        service, _index, _mirror, session = _setup(server_folders=folders)
        service.sync()

        session.uid_validity = 2
        session.body_fetches.clear()
        service.sync()

        self.assertEqual(session.body_fetches, [1], "the nameless one was re-keyed")

    def test_a_trashed_row_is_re_keyed_so_the_delete_can_be_pushed(self) -> None:
        service, index, _mirror, session = _setup(server_folders=self._ten())
        service.sync()
        row = index.list_folder_messages(folder=self.folder)[0]
        index.update_message(
            message=dataclasses.replace(row, local_status=MessageStatus.TRASHED)
        )

        session.folders.update(self._ten(first_uid=500))
        session.uid_validity = 2
        session.body_fetches.clear()
        service.sync()

        self.assertEqual(session.body_fetches, [])
        same = [
            r
            for r in index.list_folder_messages(folder=self.folder)
            if r.message_id == row.message_id
        ]
        self.assertEqual(len(same), 1)
        self.assertEqual(same[0].local_status, MessageStatus.TRASHED)
        self.assertIsNotNone(same[0].uid, "no handle, so the delete is stranded")

    def test_the_plan_names_the_re_keying(self) -> None:
        from pony.sync import AdoptUidOp, FetchNewOp

        service, _index, _mirror, session = _setup(server_folders=self._ten())
        service.sync()

        session.folders.update(self._ten(first_uid=500))
        session.uid_validity = 2
        plan = service.plan()

        ops = [op for a in plan.accounts for f in a.folders for op in f.ops]
        self.assertEqual(len([op for op in ops if isinstance(op, AdoptUidOp)]), 10)
        self.assertEqual([op for op in ops if isinstance(op, FetchNewOp)], [])

    def test_a_row_that_moved_between_plan_and_execute_is_left_alone(self) -> None:
        """Planning and execution are separate calls, and the index can move."""
        service, index, _mirror, session = _setup(server_folders=self._ten())
        service.sync()

        session.folders.update(self._ten(first_uid=500))
        session.uid_validity = 2
        plan = service.plan()

        # Somebody deletes the row the plan was going to re-key.
        row = index.list_folder_messages(folder=self.folder)[0]
        index.delete_message(message_ref=row.message_ref)

        session.body_fetches.clear()
        service.execute(plan)

        rows = index.list_folder_messages(folder=self.folder)
        self.assertEqual(len(rows), 9, "the pass invented a row for the dead one")
        self.assertEqual(session.body_fetches, [], "nothing should be downloaded here")

        # The UID that lost its row is simply new mail next time.
        session.body_fetches.clear()
        service.sync()
        self.assertEqual(len(index.list_folder_messages(folder=self.folder)), 10)
        self.assertEqual(len(session.body_fetches), 1)


class StaleUidEpochTestCase(unittest.TestCase):
    """A cleared UID epoch is not a folder full of outgoing mail.

    ``_execute_ops`` records the new ``uid_validity`` only ``if not
    had_failure``, so a pass that detects the reset, clears the UIDs and
    then fails on anything leaves every row ``ACTIVE`` with ``uid IS
    NULL`` against a stored epoch that is still the old one.  The
    APPEND arm was the one arm of ``_pending_push_ops`` that did not
    consult ``uidvalidity_reset``, so the next pass planned an upload
    for each.
    """

    folder = FolderRef(account_name="personal", folder_name="INBOX")

    def _folder_of(
        self, count: int
    ) -> tuple[
        ImapSyncService,
        SqliteIndexRepository,
        MaildirMirrorRepository,
        FakeImapSession,
    ]:
        return _setup(
            server_folders={
                "INBOX": {
                    uid: (
                        f"<stale-{uid}@example.com>",
                        frozenset(),
                        _make_raw_message(f"Stale {uid}", f"<stale-{uid}@example.com>"),
                    )
                    for uid in range(1, count + 1)
                }
            }
        )

    def test_no_upload_is_planned_for_a_row_whose_epoch_ended(self) -> None:
        from pony.sync import PushAppendOp

        service, index, _, session = self._folder_of(6)
        service.sync()
        # The state the interrupted pass leaves behind.
        session.uid_validity = 2
        index.clear_uids_for_folder(account_name="personal", folder_name="INBOX")

        plan = service.plan()
        (folder_plan,) = plan.accounts[0].folders
        appends = [op for op in folder_plan.ops if isinstance(op, PushAppendOp)]
        self.assertEqual(appends, [])

    def test_the_next_pass_re_adopts_instead(self) -> None:
        service, index, _, session = self._folder_of(6)
        service.sync()
        session.uid_validity = 2
        index.clear_uids_for_folder(account_name="personal", folder_name="INBOX")
        session.call_log.clear()

        service.sync()

        self.assertNotIn("append:INBOX", session.call_log)
        self.assertEqual(len(session.folders["INBOX"]), 6)
        rows = index.list_folder_messages(folder=self.folder)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row.uid is not None for row in rows))


class UidValidityAdoptionFallbackTestCase(unittest.TestCase):
    """Adoption must not depend on every message having a unique Message-ID.

    Matching on Message-ID alone leaves two ordinary populations
    unmatched: messages that carry no Message-ID at all, and resends or
    list copies that share one. Each of those was duplicated locally by a
    reset, and the orphaned UID-less row would then have been appended to
    the server on the next pass — turning a local duplicate into a remote
    one.
    """

    folder = FolderRef(account_name="personal", folder_name="INBOX")

    def _message(self, subject: str, message_id: str | None = None) -> bytes:
        message = EmailMessage()
        message["From"] = "alice@example.com"
        message["To"] = "bob@example.com"
        message["Subject"] = subject
        message["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
        if message_id:
            message["Message-ID"] = message_id
        message.set_content("body")
        return message.as_bytes()

    def _reset_and_count(
        self, folders: dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]]
    ) -> tuple[int, int, int]:
        service, index, mirror, session = _setup(server_folders=folders)
        service.sync()
        before = len(index.list_folder_messages(folder=self.folder))
        session.uid_validity = 2
        service.sync()
        rows = index.list_folder_messages(folder=self.folder)
        return before, len(rows), len(mirror.list_messages(folder=self.folder))

    def _dated_message(self, subject: str) -> bytes:
        """A message whose ``Date:`` is not UTC, and with no Message-ID."""
        message = EmailMessage()
        message["From"] = "alice@example.com"
        message["To"] = "bob@example.com"
        message["Subject"] = subject
        message["Date"] = "Fri, 10 Apr 2026 12:00:00 +0200"
        message.set_content("body")
        return message.as_bytes()

    def test_a_date_with_an_offset_is_adopted_not_duplicated(self) -> None:
        """The fallback compares ``received_at`` in SQL, as text.

        Nothing here is in UTC except by the projection's own doing:
        ``_parse_date`` normalises, which is the only reason a header
        offset has never reached the comparison.  One layer down, a row
        written from any other ``IndexedMessage`` would have kept the
        offset while a row read back and rewritten held ``+00:00``, and
        the match would have failed — so a message would be inserted a
        second time and the orphan would become a ``PushAppendOp``,
        which is the duplicate this fallback exists to prevent.  The
        store normalises on the way in now; this walks the whole path
        with an offset in the header, which no test did.
        """
        service, index, mirror, session = _setup(
            server_folders={
                "INBOX": {
                    1: ("", frozenset(), self._dated_message("alpha")),
                    2: ("", frozenset(), self._dated_message("beta")),
                }
            }
        )
        service.sync()
        # What a flag sync does to every UID-bearing row.
        for row in index.list_folder_messages(folder=self.folder):
            index.update_message(message=row)
        session.call_log.clear()

        session.uid_validity = 2
        service.sync()

        rows = index.list_folder_messages(folder=self.folder)
        self.assertEqual(len(rows), 2, "the messages were duplicated, not adopted")
        self.assertTrue(all(row.uid is not None for row in rows))
        self.assertEqual(len(mirror.list_messages(folder=self.folder)), 2)
        self.assertNotIn("append:INBOX", session.call_log)

    def test_messages_without_a_message_id_are_not_duplicated(self) -> None:
        before, after, files = self._reset_and_count(
            {
                "INBOX": {
                    1: ("", frozenset(), self._message("first")),
                    2: ("", frozenset(), self._message("second")),
                }
            }
        )
        self.assertEqual((before, after, files), (2, 2, 2))

    def test_messages_sharing_a_message_id_are_not_duplicated(self) -> None:
        before, after, files = self._reset_and_count(
            {
                "INBOX": {
                    1: (
                        "<dup@example.com>",
                        frozenset(),
                        self._message("copy one", "<dup@example.com>"),
                    ),
                    2: (
                        "<dup@example.com>",
                        frozenset(),
                        self._message("copy two", "<dup@example.com>"),
                    ),
                }
            }
        )
        self.assertEqual((before, after, files), (2, 2, 2))

    def test_every_row_regains_a_uid(self) -> None:
        """An unadopted row would be appended to the server next pass."""
        service, index, _mirror, session = _setup(
            server_folders={
                "INBOX": {
                    1: ("", frozenset(), self._message("alpha")),
                    2: ("", frozenset(), self._message("beta")),
                }
            }
        )
        service.sync()
        session.uid_validity = 2
        service.sync()
        rows = index.list_folder_messages(folder=self.folder)
        self.assertTrue(all(r.uid is not None for r in rows), "row left without a UID")


class MassPushGuardTestCase(unittest.TestCase):
    """C-7: a pass that would upload a whole folder back halts first.

    An index that lost its UIDs looks like a folder full of outgoing
    mail, because ``uid IS NULL`` cannot tell "composed here" from "on
    the server, handle lost".
    """

    def _folder_of(
        self, count: int
    ) -> tuple[
        ImapSyncService,
        SqliteIndexRepository,
        MaildirMirrorRepository,
        FakeImapSession,
    ]:
        return _setup(
            server_folders={
                "INBOX": {
                    uid: (
                        f"<bulk-{uid}@example.com>",
                        frozenset(),
                        _make_raw_message(f"Bulk {uid}", f"<bulk-{uid}@example.com>"),
                    )
                    for uid in range(1, count + 1)
                }
            }
        )

    def test_a_folder_that_lost_its_uids_is_not_uploaded(self) -> None:
        service, index, _, session = self._folder_of(6)
        service.sync()
        index.clear_uids_for_folder(account_name="personal", folder_name="INBOX")
        session.call_log.clear()

        plan = service.plan()
        (folder_plan,) = plan.accounts[0].folders
        self.assertTrue(folder_plan.needs_confirmation)
        self.assertEqual(folder_plan.pending_push_count, 6)
        self.assertEqual(
            plan.folders_needing_confirmation(),
            frozenset({("personal", "INBOX")}),
        )

        # Unattended callers must not waive the halt for themselves.
        result = service.sync()
        self.assertNotIn("append:INBOX", session.call_log)
        self.assertEqual(len(session.folders["INBOX"]), 6)
        # And they must say that they left it: an unattended run that
        # skipped the whole folder reported "Sync complete." and
        # nothing else, so the folder stayed unsynced in silence.
        (account_result,) = result.accounts
        self.assertEqual(account_result.held_folders, ("INBOX",))
        self.assertIn("INBOX", account_result.skipped_folders)
        from pony.sync import describe_sync_result

        said = describe_sync_result(result)
        self.assertIn("Held for confirmation: personal/INBOX.", said)
        self.assertIn("Run `pony sync`", said)

    def test_the_halt_is_described_in_the_plan_summary(self) -> None:
        service, index, _, _ = self._folder_of(6)
        service.sync()
        index.clear_uids_for_folder(account_name="personal", folder_name="INBOX")

        summary = format_plan_detail(service.plan())
        self.assertIn("would upload up to 6 message(s)", summary)

    def test_the_halt_says_the_upload_count_is_an_upper_bound(self) -> None:
        """Confirming mostly does not upload, and the prompt hid that.

        `test_confirming_adopts_the_rows_instead_of_appending_them`
        right below proves it: phase 1 re-attaches every row by its
        Message-ID and `_execute_push_append` then skips the APPEND, so
        all six "uploads" upload nothing.  The halt said
        "would upload 6 message(s)" and asked whether to apply the sync
        anyway -- which made declining, the answer that leaves the index
        without its UIDs, look like the careful one.
        """
        from pony.sync import describe_halt

        service, index, _, _ = self._folder_of(6)
        service.sync()
        index.clear_uids_for_folder(account_name="personal", folder_name="INBOX")

        halt = describe_halt(service.plan())

        self.assertIn("would upload up to 6 message(s)", halt)
        self.assertIn("re-attached by its Message-ID", halt)
        self.assertIn("not uploaded", halt)

    def test_a_deletion_halt_is_not_given_the_upload_caveat(self) -> None:
        """It is only true of uploads, so it is only said about them."""
        from pony.sync import AccountSyncPlan, FolderSyncPlan, SyncPlan, describe_halt

        plan = SyncPlan(
            accounts=(
                AccountSyncPlan(
                    account_name="personal",
                    folders=(
                        FolderSyncPlan(
                            folder_name="INBOX",
                            uid_validity=1,
                            highest_uid=10,
                            ops=(),
                            needs_confirmation=True,
                            pending_delete_count=9,
                            pending_delete_total=10,
                        ),
                    ),
                ),
            ),
        )

        halt = describe_halt(plan)

        self.assertIn("would delete 9 of 10", halt)
        self.assertNotIn("Message-ID", halt)

    def test_confirming_adopts_the_rows_instead_of_appending_them(self) -> None:
        """Even confirmed, the rows are re-adopted, not duplicated.

        Phase 1 re-attaches each UID by Message-ID; the APPEND left in
        the frozen plan is then skipped because the row has a UID.
        """
        service, index, _, session = self._folder_of(6)
        service.sync()
        index.clear_uids_for_folder(account_name="personal", folder_name="INBOX")
        session.call_log.clear()

        plan = service.plan()
        service.execute(plan, confirmed_folders=frozenset({("personal", "INBOX")}))

        self.assertNotIn("append:INBOX", session.call_log)
        self.assertEqual(len(session.folders["INBOX"]), 6)
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = index.list_folder_messages(folder=folder)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row.uid is not None for row in rows))

    def test_a_single_local_draft_still_goes_out(self) -> None:
        """The guard must not stand in the way of ordinary composing."""
        service, index, mirror, session = self._folder_of(6)
        service.sync()
        folder = FolderRef(account_name="personal", folder_name="INBOX")
        raw = _make_raw_message("Mine", "<mine@example.com>")
        key = mirror.store_message(folder=folder, raw_message=raw)
        index.insert_message(
            message=project_rfc822_message(
                message_ref=MessageRef(
                    account_name="personal", folder_name="INBOX", id=0
                ),
                raw_message=raw,
                storage_key=key,
            )
        )
        session.call_log.clear()

        service.sync()

        self.assertIn("append:INBOX", session.call_log)
        self.assertEqual(len(session.folders["INBOX"]), 7)


class MassPushRatioOnEveryPathTestCase(unittest.TestCase):
    """The ratio arm had a baseline on one planning path out of four.

    ``known`` was ``pending_delete_total + fetches``, and only
    ``_plan_slow_path`` sets ``pending_delete_total``.  On the fast
    path, the medium path and a new folder it was 0, so ``over_ratio``
    was never true and 5 to 49 stranded uploads went out unflagged.
    """

    folder = FolderRef(account_name="personal", folder_name="INBOX")

    def _compose(
        self,
        index: SqliteIndexRepository,
        mirror: MaildirMirrorRepository,
        count: int,
    ) -> None:
        for n in range(count):
            raw = _make_raw_message(f"Mine {n}", f"<mine-{n}@example.com>")
            key = mirror.store_message(folder=self.folder, raw_message=raw)
            index.insert_message(
                message=project_rfc822_message(
                    message_ref=MessageRef(
                        account_name="personal",
                        folder_name="INBOX",
                        id=0,
                    ),
                    raw_message=raw,
                    storage_key=key,
                )
            )

    def _ten_on_the_server(
        self,
    ) -> tuple[
        ImapSyncService,
        SqliteIndexRepository,
        MaildirMirrorRepository,
        FakeImapSession,
    ]:
        return _setup(
            server_folders={
                "INBOX": {
                    uid: (
                        f"<there-{uid}@example.com>",
                        frozenset(),
                        _make_raw_message(f"There {uid}", f"<there-{uid}@example.com>"),
                    )
                    for uid in range(1, 11)
                }
            }
        )

    def test_the_fast_path_measures_uploads_against_the_folder(self) -> None:
        service, index, mirror, session = self._ten_on_the_server()
        service.sync()
        # Nothing changed on the server, so this is the fast path: no
        # FETCH, no fresh UID or flag information.
        self._compose(index, mirror, 8)
        session.call_log.clear()

        plan = service.plan()
        (folder_plan,) = plan.accounts[0].folders
        self.assertEqual(folder_plan.pending_push_count, 8)
        self.assertEqual(folder_plan.known_total, 10)
        self.assertTrue(folder_plan.needs_confirmation)

        service.sync()
        self.assertNotIn("append:INBOX", session.call_log)

    def test_half_a_folder_is_the_line_and_not_over_it(self) -> None:
        # Past the minimum, so the ratio arm really is consulted -- and
        # not over it.  Every sent copy filed locally carries
        # uid IS NULL until it is uploaded, so a small Sent folder
        # reaches a large share of itself through ordinary use, and a
        # halted folder is skipped by the background pass, the browser's
        # Sync button and ctrl+g: flagging this would strand the copies
        # until someone ran an interactive sync.
        service, index, mirror, session = self._ten_on_the_server()
        service.sync()
        self._compose(index, mirror, 5)
        session.call_log.clear()

        plan = service.plan()
        (folder_plan,) = plan.accounts[0].folders
        from pony.sync import PushAppendOp

        appends = [op for op in folder_plan.ops if isinstance(op, PushAppendOp)]
        self.assertEqual(len(appends), 5)
        self.assertEqual(folder_plan.known_total, 10)
        self.assertFalse(folder_plan.needs_confirmation)

        service.sync()
        self.assertIn("append:INBOX", session.call_log)
        self.assertEqual(len(session.folders["INBOX"]), 15)

    def test_a_couple_of_drafts_are_still_ordinary_composing(self) -> None:
        service, index, mirror, session = self._ten_on_the_server()
        service.sync()
        self._compose(index, mirror, 2)
        session.call_log.clear()

        plan = service.plan()
        (folder_plan,) = plan.accounts[0].folders
        self.assertFalse(folder_plan.needs_confirmation)

        service.sync()
        self.assertIn("append:INBOX", session.call_log)
        self.assertEqual(len(session.folders["INBOX"]), 12)


class ScopedExpungeTestCase(unittest.TestCase):
    """F4: deleting one message must not expunge another client's work."""

    def test_pushing_one_delete_expunges_only_that_uid(self) -> None:
        service, index, _, session = _setup(
            server_folders={
                "INBOX": {
                    1: (
                        "<keep@example.com>",
                        frozenset(),
                        _make_raw_message("Keep", "<keep@example.com>"),
                    ),
                    2: (
                        "<go@example.com>",
                        frozenset(),
                        _make_raw_message("Go", "<go@example.com>"),
                    ),
                }
            }
        )
        service.sync()

        folder = FolderRef(account_name="personal", folder_name="INBOX")
        rows = {r.message_id: r for r in index.list_folder_messages(folder=folder)}
        index.upsert_message(
            message=dataclasses.replace(
                rows["<go@example.com>"],
                local_status=MessageStatus.TRASHED,
            )
        )
        # Another client has flagged the *other* message \Deleted and
        # not expunged it yet.  A folder-wide EXPUNGE would destroy it.
        session.deleted_uids.append(1)

        service.sync()

        self.assertIn(frozenset({2}), session.expunged_scopes)
        self.assertNotIn(None, session.expunged_scopes)
        self.assertIn(1, session.folders["INBOX"])


class EncodedFolderNameTestCase(unittest.TestCase):
    """F6: the mirror's own name encoding is not a folder to create."""

    def test_a_mirror_dir_that_is_our_encoding_is_not_created(self) -> None:
        service, _index, _mirror, session = _setup(
            server_folders={
                "INBOX": {
                    1: (
                        "<a@example.com>",
                        frozenset(),
                        _make_raw_message("A", "<a@example.com>"),
                    )
                },
                "Archive/2024": {
                    5: (
                        "<nested@example.com>",
                        frozenset(),
                        _make_raw_message("Nested", "<nested@example.com>"),
                    )
                },
            }
        )
        service.sync()
        session.created_folders.clear()

        plan = service.plan()
        self.assertEqual(plan.accounts[0].creates, ())

        service.sync()
        self.assertEqual(session.created_folders, [])


class ProgressCadenceTestCase(unittest.TestCase):
    """Reporting per item makes the terminal the bottleneck.

    The consumer loop pays for each report between one message and the
    next: a flushed terminal write in the CLI, a hop into the event loop
    in the reader.  The engine decides the cadence so neither front end
    has to throttle for itself.
    """

    def test_a_gate_reports_at_most_once_per_interval(self) -> None:
        from pony.outcome import ProgressGate

        gate = ProgressGate(interval=60.0)

        allowed = [gate.due(done) for done in range(1, 1001)]

        self.assertEqual(allowed.count(True), 1)

    def test_a_slow_loop_still_reports_only_once_per_stride(self) -> None:
        """The time cap alone lets a slow loop report on every item."""
        from pony.outcome import ProgressGate

        gate = ProgressGate(interval=0.0, stride=100)

        allowed = [gate.due(done) for done in range(1, 1001)]

        self.assertEqual(allowed.count(True), 10)

    def test_the_last_item_always_reports(self) -> None:
        from pony.outcome import ProgressGate

        gate = ProgressGate(interval=60.0)
        gate.due(1)

        self.assertFalse(gate.due(2))
        self.assertTrue(gate.due(2, final=True))

    def test_a_loop_with_nothing_to_count_is_gated_on_the_clock_alone(self) -> None:
        """An argument-less `due()` was closed forever, whatever elapsed.

        `due(done: int = 0)` compared 0 against the item last reported,
        so `0 - 0 < stride` refused every call -- a gate with no item
        count reported nothing, however long the work took.  All four
        call sites pass `done`, so this was the trap waiting for the
        fifth.
        """
        from pony.outcome import ProgressGate

        gate = ProgressGate(interval=0.0)

        self.assertTrue(gate.due())
        self.assertTrue(gate.due())

    def test_a_countless_gate_still_waits_out_its_interval(self) -> None:
        from pony.outcome import ProgressGate

        gate = ProgressGate(interval=60.0)
        self.assertTrue(gate.due())

        self.assertFalse(gate.due())
        self.assertTrue(gate.due(final=True))

    def test_the_stride_is_derived_from_the_work(self) -> None:
        from pony.outcome import PROGRESS_REPORTS_PER_UNIT, progress_stride

        self.assertEqual(progress_stride(0), 1)
        self.assertEqual(progress_stride(10), 1)
        self.assertEqual(
            progress_stride(140_000),
            140_000 // PROGRESS_REPORTS_PER_UNIT,
        )

    def test_a_long_sync_does_not_report_once_per_message(self) -> None:
        """What the engine emits, not what a front end chooses to print."""
        reports: list[str] = []
        service, index, _mirror, _session = _setup(
            server_folders={
                "INBOX": {
                    uid: (
                        f"<m{uid}@example.com>",
                        frozenset(),
                        _make_raw_message(
                            f"Message {uid}",
                            f"<m{uid}@example.com>",
                        ),
                    )
                    for uid in range(1, 61)
                },
            },
        )

        plan = service.plan()
        service.execute(
            plan,
            confirmed_folders=plan.folders_needing_confirmation(),
            progress=lambda info: reports.append(info.message),
        )

        rows = index.list_folder_messages(
            folder=FolderRef(account_name="personal", folder_name="INBOX"),
        )
        self.assertEqual(len(rows), 60)
        # Sixty messages, far fewer reports — and the final count is one
        # of them, so a finished folder shows its true total.
        self.assertLess(len(reports), 60)
        self.assertTrue(any("60/60" in message for message in reports))


class FetchBackpressureTestCase(unittest.TestCase):
    """The fetch must not outrun the local work.

    The producer thread hands whole message bodies to the consumer.  With
    an unbounded queue a large folder pulled thousands of them into
    memory while the consumer was still writing the first hundred, and
    the kernel killed the sync part-way through the folder.
    """

    def test_the_queue_between_fetch_and_ingest_is_bounded(self) -> None:
        from pony.sync import _FETCH_QUEUE_DEPTH

        self.assertGreater(_FETCH_QUEUE_DEPTH, 0)

    def test_a_slow_consumer_holds_the_fetch_back(self) -> None:
        """What the producer has read, minus what the consumer has taken."""
        from pony.sync import _FETCH_QUEUE_DEPTH

        count = _FETCH_QUEUE_DEPTH * 6
        service, index, _mirror, session = _setup(
            server_folders={
                "INBOX": {
                    uid: (
                        f"<m{uid}@example.com>",
                        frozenset(),
                        _make_raw_message(f"Message {uid}", f"<m{uid}@example.com>"),
                    )
                    for uid in range(1, count + 1)
                },
            },
        )
        fetched: list[int] = []
        original = session.fetch_messages_batch

        def _counting(folder_name: str, uids: list[int]) -> dict[int, bytes]:
            fetched.extend(uids)
            return original(folder_name, uids)

        session.fetch_messages_batch = _counting  # type: ignore[method-assign]

        ingested: list[int] = []
        real_insert = index.insert_message

        def _slow_insert(*, message: object) -> object:
            # Every insert checks how far ahead the fetch has run.
            ingested.append(len(fetched))
            return real_insert(message=message)  # type: ignore[arg-type]

        index.insert_message = _slow_insert  # type: ignore[method-assign]
        plan = service.plan()
        service.execute(plan, confirmed_folders=frozenset())

        # At no point had the fetch read more than the queue depth (plus
        # the batch in flight) beyond what had been ingested.
        worst = max(ahead - done for done, ahead in enumerate(ingested))
        self.assertLessEqual(worst, _FETCH_QUEUE_DEPTH + 2 * 25)
        self.assertEqual(len(ingested), count)


class AdoptionCostTestCase(unittest.TestCase):
    """Matching a fetched message against pending rows must not scan.

    The lookup runs for every fetched message whose Message-ID matched
    no pending row -- in a freshly rebuilt index, that is all of them.
    It used to list the whole folder and filter in Python, so a first
    sync cost one full materialisation of a growing folder per message.
    """

    def test_a_first_sync_does_not_read_the_folder_per_message(self) -> None:
        service, index, _mirror, _session = _setup(
            server_folders={
                "INBOX": {
                    uid: (
                        f"<m{uid}@example.com>",
                        frozenset(),
                        _make_raw_message(f"Message {uid}", f"<m{uid}@example.com>"),
                    )
                    for uid in range(1, 41)
                },
            },
        )
        listings = 0
        real = index.list_folder_messages

        def _counting(*, folder: FolderRef) -> object:
            nonlocal listings
            listings += 1
            return real(folder=folder)

        index.list_folder_messages = _counting  # type: ignore[method-assign]

        plan = service.plan()
        service.execute(plan, confirmed_folders=frozenset())

        index.list_folder_messages = real  # type: ignore[method-assign]
        rows = index.list_folder_messages(
            folder=FolderRef(account_name="personal", folder_name="INBOX"),
        )
        self.assertEqual(len(rows), 40)
        self.assertEqual(listings, 0)

    def test_each_message_is_parsed_once(self) -> None:
        """Adoption and the row want the same projection, not two."""
        from unittest.mock import patch

        from pony import sync as sync_module

        service, index, _mirror, _session = _setup(
            server_folders={
                "INBOX": {
                    uid: (
                        f"<m{uid}@example.com>",
                        frozenset(),
                        _make_raw_message(f"Message {uid}", f"<m{uid}@example.com>"),
                    )
                    for uid in range(1, 21)
                },
            },
        )
        calls = 0
        real_project = sync_module.project_rfc822_message

        def _counting(**kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return real_project(**kwargs)  # type: ignore[arg-type]

        with patch.object(sync_module, "project_rfc822_message", _counting):
            plan = service.plan()
            service.execute(plan, confirmed_folders=frozenset())

        self.assertEqual(calls, 20)


class NonUtcDateIsAdoptedTestCase(unittest.TestCase):
    """A reset adopts offset-dated mail that its Message-ID cannot place.

    Adoption keys on Message-ID first; when that cannot settle it -- a
    resend, a list copy, two messages sharing one id -- it falls back to
    what the message says about itself, which includes its ``Date:``.
    That comparison is SQL *text*, so a header offset that survived into
    the stored row would not match the probe, and the row would be
    duplicated and then appended to the server as an orphan.

    The offset cannot in fact get that far: ``_parse_date``
    (``src/pony/message_projection.py:150-161``) returns
    ``parsed.astimezone(UTC)``, so every ``received_at`` the engine ever
    compares is already UTC whatever the header said.  Verified by
    removing both normalisations -- that one and
    ``pony.calendar.index_store.datetime_to_sql`` -- with this test still
    green.  So this guards the adoption itself, which it does catch:
    with ``_adopt_uid`` returning False the folder ends up with four
    rows.  The text-comparison hazard is pinned where it is reachable,
    at the index layer, by ``UtcNormalisationTest`` in
    ``tests/test_index_store.py``.
    """

    folder = FolderRef(account_name="personal", folder_name="INBOX")
    #: One Message-ID on two messages, so the id cannot pick between
    #: them and the projection fallback is the only way through.
    shared_id = "<resent@example.com>"

    def _server(
        self,
    ) -> dict[str, dict[int, tuple[str, frozenset[MessageFlag], bytes]]]:
        return {
            "INBOX": {
                1: (
                    self.shared_id,
                    frozenset(),
                    _make_raw_message(
                        "Berlin morning",
                        self.shared_id,
                        date="Fri, 10 Apr 2026 12:00:00 +0200",
                    ),
                ),
                2: (
                    self.shared_id,
                    frozenset(),
                    _make_raw_message(
                        "Berlin afternoon",
                        self.shared_id,
                        date="Fri, 10 Apr 2026 16:30:00 +0200",
                    ),
                ),
            }
        }

    def test_a_reset_adopts_rows_whose_date_carried_an_offset(self) -> None:
        service, index, _mirror, session = _setup(server_folders=self._server())
        service.sync()
        # A flag sync rewrites every UID-bearing row, which is what
        # normalised the stored timestamp while the probe kept the
        # header's offset.  Without a pass like this the stored text and
        # the probe agree by accident and the comparison proves nothing.
        session.folders["INBOX"][1] = (
            self.shared_id,
            frozenset({MessageFlag.SEEN}),
            session.folders["INBOX"][1][2],
        )
        service.sync()
        before = {
            row.message_ref.id: row.subject
            for row in index.list_folder_messages(folder=self.folder)
        }
        self.assertEqual(len(before), 2)

        session.uid_validity = 2
        session.call_log.clear()
        service.sync()

        rows = index.list_folder_messages(folder=self.folder)
        self.assertEqual(len(rows), 2, "the offset-dated rows were duplicated")
        self.assertEqual(
            {row.message_ref.id: row.subject for row in rows},
            before,
            "rows were replaced, not adopted",
        )
        self.assertTrue(all(row.uid is not None for row in rows), "UIDs not re-adopted")
        # An unadopted row becomes an upload on the next pass.
        self.assertNotIn("append", " ".join(session.call_log).lower())
