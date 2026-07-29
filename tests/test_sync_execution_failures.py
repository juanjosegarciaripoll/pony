"""Execution-time failures and mid-sync races in the sync engine.

The planner decides what to do while holding a consistent view; the
executor then runs those ops against a mirror and a server that can
both fail underneath it.  Every op returns a success flag, and a false
flag must degrade to "this folder had a failure" rather than abort the
account or, worse, record a sync watermark that claims work was done.

The other half of this file covers the race the executor defends
against: ``plan()`` and ``execute()`` are separate public calls, so an
index row can disappear between them — a concurrent TUI action, or a
second Pony process.  Every op that re-reads its row must tolerate
finding nothing.
"""

from __future__ import annotations

import dataclasses
import unittest
from uuid import uuid4

from conftest import TMP_ROOT
from test_sync import FakeImapSession, _make_raw_message, _setup

from pony.domain import (
    FolderRef,
    MessageFlag,
    MessageStatus,
)
from pony.sync import (
    PushAppendOp,
    RestoreOp,
    ServerDeleteOp,
)

_PERSONAL_INBOX = FolderRef(account_name="personal", folder_name="INBOX")


def _server_message(uid: int, subject: str) -> dict[int, tuple[str, frozenset, bytes]]:
    message_id = f"<{subject.lower().replace(' ', '-')}@example.com>"
    return {uid: (message_id, frozenset(), _make_raw_message(subject, message_id))}


class IngestFailureTestCase(unittest.TestCase):
    """A mirror that cannot store the fetched bytes."""

    def test_a_mirror_write_failure_leaves_the_row_unindexed(self) -> None:
        """Failing to store must not create an index row pointing nowhere."""
        service, index, mirror, _session = _setup(
            server_folders={"INBOX": _server_message(1, "Doomed")}
        )

        def _boom(**_kwargs: object) -> str:
            raise OSError("disk full")

        mirror.store_message_async = _boom  # type: ignore[method-assign]
        mirror.store_message = _boom  # type: ignore[method-assign]

        result = service.sync()

        self.assertEqual(len(index.list_folder_messages(folder=_PERSONAL_INBOX)), 0)
        self.assertEqual(result.accounts[0].folders[0].fetched, 0)

    def test_a_failed_folder_does_not_record_a_sync_watermark(self) -> None:
        """Recording one would make the next sync skip the messages it missed."""
        service, index, mirror, _session = _setup(
            server_folders={"INBOX": _server_message(1, "Doomed")}
        )

        def _boom(**_kwargs: object) -> str:
            raise OSError("disk full")

        mirror.store_message_async = _boom  # type: ignore[method-assign]
        mirror.store_message = _boom  # type: ignore[method-assign]
        service.sync()

        states = index.list_folder_sync_states(account_name="personal")
        inbox = [s for s in states if s.folder_name == "INBOX"]
        self.assertEqual(inbox, [], "a failed folder must not be watermarked")


class BatchFetchFailureTestCase(unittest.TestCase):
    """The batched FETCH is best-effort; a failure degrades, not aborts."""

    def test_a_failed_batch_fetch_skips_those_messages(self) -> None:
        service, index, _mirror, session = _setup(
            server_folders={
                "INBOX": {
                    **_server_message(1, "One"),
                    **_server_message(2, "Two"),
                }
            }
        )

        def _boom(*_args: object, **_kwargs: object) -> dict[int, bytes]:
            raise OSError("FETCH exploded")

        session.fetch_messages_batch = _boom  # type: ignore[method-assign]

        service.sync()

        # Nothing ingested, but the sync returned normally.
        self.assertEqual(len(index.list_folder_messages(folder=_PERSONAL_INBOX)), 0)

    def test_more_messages_than_one_batch_are_flushed_in_chunks(self) -> None:
        """The producer flushes every 25 fetches; 30 forces a second flush."""
        folders = {"INBOX": {}}
        for uid in range(1, 31):
            folders["INBOX"].update(_server_message(uid, f"Message {uid}"))
        service, index, _mirror, _session = _setup(server_folders=folders)

        service.sync()

        self.assertEqual(len(index.list_folder_messages(folder=_PERSONAL_INBOX)), 30)


class VanishedRowTestCase(unittest.TestCase):
    """Rows deleted between ``plan()`` and ``execute()``.

    The TUI can trash a message, or a second Pony process can prune it,
    while a sync is in flight.  Each op re-reads its row and must simply
    skip when it is gone.
    """

    def _synced_service(self):  # type: ignore[no-untyped-def]
        service, index, mirror, session = _setup(
            server_folders={"INBOX": _server_message(1, "Present")}
        )
        service.sync()
        return service, index, mirror, session

    def test_a_server_delete_for_an_already_deleted_row_is_skipped(self) -> None:
        service, index, _mirror, session = self._synced_service()
        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]

        # The server drops the message, so planning emits ServerDeleteOp…
        session.folders["INBOX"] = {}
        plan = service.plan()
        ops = [
            op
            for account in plan.accounts
            for folder in account.folders
            for op in folder.ops
        ]
        self.assertTrue(any(isinstance(op, ServerDeleteOp) for op in ops))

        # …but the row is gone locally by the time we execute.
        index.delete_message(message_ref=row.message_ref)
        service.execute(plan)

        self.assertEqual(len(index.list_folder_messages(folder=_PERSONAL_INBOX)), 0)

    def test_a_restore_for_an_already_deleted_row_is_skipped(self) -> None:
        service, index, _mirror, _session = self._synced_service()
        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]
        from pony.domain import AccountConfig, FolderConfig

        account = service._config.accounts[0]
        assert isinstance(account, AccountConfig)
        service._config = dataclasses.replace(
            service._config,
            accounts=(
                dataclasses.replace(
                    account, folders=FolderConfig(read_only=("INBOX",))
                ),
            ),
        )

        # A trashed row in a read-only folder plans a RestoreOp.
        index.update_message(
            message=dataclasses.replace(row, local_status=MessageStatus.TRASHED)
        )
        plan = service.plan()
        ops = [
            op for acc in plan.accounts for folder in acc.folders for op in folder.ops
        ]
        self.assertTrue(any(isinstance(op, RestoreOp) for op in ops))

        index.delete_message(message_ref=row.message_ref)
        service.execute(plan)

        self.assertEqual(len(index.list_folder_messages(folder=_PERSONAL_INBOX)), 0)

    def test_an_append_for_an_already_deleted_row_reports_failure(self) -> None:
        """The draft is gone, so there is nothing to upload."""
        service, index, mirror, _session = self._synced_service()

        raw = _make_raw_message("Local draft", "<draft@example.com>")
        storage_key = mirror.store_message(folder=_PERSONAL_INBOX, raw_message=raw)
        from pony.domain import MessageRef
        from pony.message_projection import project_rfc822_message

        saved = index.insert_message(
            message=project_rfc822_message(
                message_ref=MessageRef(
                    account_name="personal", folder_name="INBOX", id=0
                ),
                raw_message=raw,
                storage_key=storage_key,
            )
        )

        plan = service.plan()
        ops = [
            op for acc in plan.accounts for folder in acc.folders for op in folder.ops
        ]
        self.assertTrue(any(isinstance(op, PushAppendOp) for op in ops))

        index.delete_message(message_ref=saved.message_ref)
        result = service.execute(plan)

        self.assertEqual(result.accounts[0].folders[0].appended_to_server, 0)

    def test_an_append_whose_mirror_bytes_vanished_reports_failure(self) -> None:
        """The row survives but its file does not — no APPEND, no crash."""
        service, index, mirror, session = self._synced_service()

        raw = _make_raw_message("Orphan draft", "<orphan-draft@example.com>")
        storage_key = mirror.store_message(folder=_PERSONAL_INBOX, raw_message=raw)
        from pony.domain import MessageRef
        from pony.message_projection import project_rfc822_message

        index.insert_message(
            message=project_rfc822_message(
                message_ref=MessageRef(
                    account_name="personal", folder_name="INBOX", id=0
                ),
                raw_message=raw,
                storage_key=storage_key,
            )
        )
        mirror.delete_message(folder=_PERSONAL_INBOX, storage_key=storage_key)

        before = dict(session.folders["INBOX"])
        result = service.execute(service.plan())

        self.assertEqual(result.accounts[0].folders[0].appended_to_server, 0)
        # The server is untouched — only the already-synced message remains.
        self.assertEqual(session.folders["INBOX"], before)


class CleanupTestCase(unittest.TestCase):
    """Trash retention runs before every execute and must never raise."""

    def test_local_accounts_and_zero_retention_are_skipped(self) -> None:
        """Local accounts have no server; retention 0 means keep forever."""
        from pony.domain import (
            AccountConfig,
            AppConfig,
            LocalAccountConfig,
            MirrorConfig,
            SmtpConfig,
        )
        from pony.index_store import SqliteIndexRepository
        from pony.storage import MaildirMirrorRepository
        from pony.sync import ImapSyncService

        tmp = TMP_ROOT / "sync" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        mirror = MaildirMirrorRepository(account_name="keeper", root_dir=tmp / "mirror")
        index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
        index.initialize()

        keeper = AccountConfig(
            name="keeper",
            email_address="keeper@example.com",
            imap_host="imap.example.com",
            smtp=SmtpConfig(host="smtp.example.com"),
            username="keeper",
            credentials_source="plaintext",
            mirror=MirrorConfig(
                path=tmp / "mirror",
                format="maildir",
                trash_retention_days=0,
            ),
            password="pw",
        )
        local = LocalAccountConfig(
            name="local",
            email_address="local@example.com",
            mirror=MirrorConfig(path=tmp / "local-mirror", format="maildir"),
        )

        class _Creds:
            def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
                return "pw"

        service = ImapSyncService(
            config=AppConfig(accounts=(keeper, local)),
            mirror_factory=lambda _acc: mirror,
            index=index,
            credentials=_Creds(),
            session_factory=lambda _acc, _pw: FakeImapSession(folders={"INBOX": {}}),
        )

        # Runs cleanup for both accounts; neither may raise.
        service._run_cleanup()

    def test_a_cleanup_failure_is_swallowed(self) -> None:
        """A mirror that cannot purge must not block the sync."""
        service, index, _mirror, _session = _setup(
            server_folders={"INBOX": _server_message(1, "Kept")}
        )
        service.sync()
        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]
        index.update_message(
            message=dataclasses.replace(
                row,
                local_status=MessageStatus.TRASHED,
                local_flags=frozenset({MessageFlag.DELETED}),
            )
        )

        def _boom(**_kwargs: object) -> None:
            raise OSError("cannot purge")

        service._index.purge_trashed_before = _boom  # type: ignore[method-assign]

        service._run_cleanup()


class SlowPathReconciliationTestCase(unittest.TestCase):
    """Arms only the slow path reaches.

    The fast and medium paths short-circuit on STATUS and CONDSTORE.
    A server without CONDSTORE forces the full scan, which is where
    read-only restore and flag reconciliation live.
    """

    def _no_condstore(self, folders):  # type: ignore[no-untyped-def]
        service, index, mirror, session = _setup(server_folders=folders)
        session.condstore = False
        return service, index, mirror, session

    def _make_read_only(self, service) -> None:  # type: ignore[no-untyped-def]
        from pony.domain import AppConfig, FolderConfig

        account = service._config.accounts[0]
        service._config = AppConfig(
            accounts=(
                dataclasses.replace(
                    account, folders=FolderConfig(read_only=("INBOX",))
                ),
            )
        )

    def test_a_trashed_row_in_a_read_only_folder_is_restored_with_server_flags(
        self,
    ) -> None:
        """Read-only means the delete can never be pushed, so undo it locally.

        The row is restored to ACTIVE *and* the server's current flags
        are pulled — otherwise the next sync would see drift and try to
        push the delete again.
        """
        raw = _make_raw_message("Kept", "<kept@example.com>")
        service, index, _mirror, session = self._no_condstore(
            {"INBOX": {1: ("<kept@example.com>", frozenset(), raw)}}
        )
        service.sync()
        self._make_read_only(service)

        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]
        index.update_message(
            message=dataclasses.replace(row, local_status=MessageStatus.TRASHED)
        )
        # The server flags it, and a second message forces a rescan.
        session.folders["INBOX"][1] = (
            "<kept@example.com>",
            frozenset({MessageFlag.FLAGGED}),
            raw,
        )
        session.folders["INBOX"][2] = (
            "<other@example.com>",
            frozenset(),
            _make_raw_message("Other", "<other@example.com>"),
        )

        service.sync()

        restored = index.get_message(message_ref=row.message_ref)
        assert restored is not None
        self.assertEqual(restored.local_status, MessageStatus.ACTIVE)
        self.assertIn(MessageFlag.FLAGGED, restored.local_flags)
        self.assertEqual(session.deleted_uids, [])

    def test_a_local_only_flag_change_is_pushed_on_the_slow_path(self) -> None:
        """Local drift with an unchanged server flag set pushes, not merges."""
        service, index, _mirror, session = self._no_condstore(
            {
                "INBOX": {
                    1: (
                        "<drift@example.com>",
                        frozenset(),
                        _make_raw_message("Drift", "<drift@example.com>"),
                    )
                }
            }
        )
        service.sync()

        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]
        index.update_message(
            message=dataclasses.replace(
                row, local_flags=frozenset({MessageFlag.FLAGGED})
            )
        )
        # A new server message forces the folder off the fast path.
        session.folders["INBOX"][9] = (
            "<new@example.com>",
            frozenset(),
            _make_raw_message("New", "<new@example.com>"),
        )

        service.sync()

        self.assertIn(1, [uid for uid, _flags in session.stored_flags])


class UidValidityResetTestCase(unittest.TestCase):
    """A server-side UIDVALIDITY change invalidates every stored UID."""

    def test_a_trashed_row_is_not_deleted_against_a_stale_uid(self) -> None:
        """Its UID belongs to the old epoch — issuing STORE would hit a stranger."""
        service, index, _mirror, session = _setup(
            server_folders={
                "INBOX": {
                    1: (
                        "<stale@example.com>",
                        frozenset(),
                        _make_raw_message("Stale", "<stale@example.com>"),
                    )
                }
            }
        )
        service.sync()
        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]
        index.update_message(
            message=dataclasses.replace(row, local_status=MessageStatus.TRASHED)
        )

        session.uid_validity = 77
        service.sync()

        self.assertEqual(session.deleted_uids, [])

    def test_a_zero_uid_validity_warns_and_resyncs(self) -> None:
        """UIDVALIDITY 0 means the server will not promise UID stability.

        Pony does not refuse the folder — it warns and treats the value
        as a reset, which re-scans everything rather than trusting UIDs
        it cannot rely on.
        """
        service, index, _mirror, session = _setup(
            server_folders={
                "INBOX": {
                    1: (
                        "<zero@example.com>",
                        frozenset(),
                        _make_raw_message("Zero", "<zero@example.com>"),
                    )
                }
            }
        )
        service.sync()

        session.uid_validity = 0
        with self.assertLogs("pony.sync", level="WARNING") as captured:
            service.sync()

        self.assertTrue(
            any("UIDVALIDITY 0" in line for line in captured.output),
            captured.output,
        )
        # The message survives the forced re-scan — no duplicate row.
        self.assertEqual(len(index.list_folder_messages(folder=_PERSONAL_INBOX)), 1)

    def test_a_pending_move_from_a_reset_folder_waits_rather_than_duplicating(
        self,
    ) -> None:
        """Appending would leave the original where it is.

        The source handle is a UID from the epoch that just ended, so the
        move cannot be pushed yet.  Uploading to the target instead —
        which is what this used to do — left the message in the source
        folder as well, so it existed twice on the server and no later
        sync removed either copy.  The move waits for the source UID to
        be re-adopted and completes on the next pass.
        """
        from pony.domain import MessageRef

        raw = _make_raw_message("Moving", "<moving@example.com>")
        service, index, mirror, session = _setup(
            server_folders={
                "INBOX": {1: ("<moving@example.com>", frozenset(), raw)},
                "Archive": {},
            }
        )
        service.sync()

        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]
        archive = FolderRef(account_name="personal", folder_name="Archive")
        new_key = mirror.store_message(folder=archive, raw_message=raw)
        index.update_message(
            message=dataclasses.replace(
                row,
                message_ref=MessageRef(
                    account_name="personal",
                    folder_name="Archive",
                    id=row.message_ref.id,
                ),
                local_status=MessageStatus.PENDING_MOVE,
                storage_key=new_key,
                uid=None,
                source_folder="INBOX",
                source_uid=1,
            )
        )

        session.uid_validity = 99
        service.execute(service.plan())

        # Nothing pushed yet: no copy in Archive, original untouched.
        self.assertEqual(session.moves, [])
        self.assertFalse(session.folders["Archive"])

        # The re-fetch re-adopted the source UID, so the next pass moves it.
        service.sync()
        archived = {v[0] for v in session.folders["Archive"].values()}
        inbox = {v[0] for v in session.folders["INBOX"].values()}
        self.assertIn("<moving@example.com>", archived)
        self.assertNotIn(
            "<moving@example.com>",
            inbox,
            "the message is in both folders on the server",
        )


class AppendWithoutUidplusTestCase(unittest.TestCase):
    """Servers without UIDPLUS return no APPENDUID."""

    def test_a_plain_draft_stays_active_when_no_uid_comes_back(self) -> None:
        """Without APPENDUID the row keeps uid=NULL for the next sync to resolve."""
        from pony.domain import MessageRef
        from pony.message_projection import project_rfc822_message

        service, index, mirror, session = _setup(
            server_folders={"INBOX": _server_message(1, "Existing")}
        )
        service.sync()

        raw = _make_raw_message("Composed", "<composed@example.com>")
        storage_key = mirror.store_message(folder=_PERSONAL_INBOX, raw_message=raw)
        saved = index.insert_message(
            message=project_rfc822_message(
                message_ref=MessageRef(
                    account_name="personal", folder_name="INBOX", id=0
                ),
                raw_message=raw,
                storage_key=storage_key,
            )
        )

        original_append = session.append_message

        def _no_appenduid(*args: object, **kwargs: object) -> None:
            original_append(*args, **kwargs)  # type: ignore[arg-type]
            return None

        session.append_message = _no_appenduid  # type: ignore[method-assign]

        service.execute(service.plan())

        row = index.get_message(message_ref=saved.message_ref)
        assert row is not None
        self.assertEqual(row.local_status, MessageStatus.ACTIVE)
        self.assertIsNone(row.uid)


class PushFailureTestCase(unittest.TestCase):
    """Server-side pushes that come back unsuccessful."""

    def test_a_failed_move_is_reported_and_leaves_the_row_pending(self) -> None:
        """A MOVE whose source UID is gone must not clear PENDING_MOVE."""
        from pony.domain import MessageRef

        raw = _make_raw_message("Mover", "<mover@example.com>")
        service, index, mirror, session = _setup(
            server_folders={
                "INBOX": {1: ("<mover@example.com>", frozenset(), raw)},
                "Archive": {},
            }
        )
        service.sync()

        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]
        archive = FolderRef(account_name="personal", folder_name="Archive")
        new_key = mirror.store_message(folder=archive, raw_message=raw)
        index.update_message(
            message=dataclasses.replace(
                row,
                message_ref=MessageRef(
                    account_name="personal",
                    folder_name="Archive",
                    id=row.message_ref.id,
                ),
                local_status=MessageStatus.PENDING_MOVE,
                storage_key=new_key,
                uid=None,
                source_folder="INBOX",
                source_uid=1,
            )
        )
        # The source UID no longer exists server-side, so MOVE returns None.
        session.folders["INBOX"] = {}

        result = service.execute(service.plan())

        moved = sum(f.moved_to_server for a in result.accounts for f in a.folders)
        self.assertEqual(moved, 0)

    def test_a_failed_reupload_is_reported(self) -> None:
        """C-1 re-upload of a locally-modified message the server lost."""
        raw = _make_raw_message("Modified", "<modified@example.com>")
        service, index, mirror, session = _setup(
            server_folders={"INBOX": {1: ("<modified@example.com>", frozenset(), raw)}}
        )
        service.sync()

        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]
        index.update_message(
            message=dataclasses.replace(
                row, local_flags=frozenset({MessageFlag.FLAGGED})
            )
        )
        # Server loses it — with local drift this plans a ReUploadOp…
        session.folders["INBOX"] = {}

        # …which cannot read its bytes back.
        def _boom(**_kwargs: object) -> bytes:
            raise OSError("mirror unreadable")

        mirror.get_message_bytes = _boom  # type: ignore[method-assign]

        result = service.execute(service.plan())

        reuploaded = sum(
            f.reuploaded_to_server for a in result.accounts for f in a.folders
        )
        self.assertEqual(reuploaded, 0)


class ExpiredTrashTestCase(unittest.TestCase):
    """Retention purges index rows and their mirror files."""

    def test_a_purge_whose_mirror_file_is_already_gone_still_completes(self) -> None:
        """The index row is authoritative; a missing file is not an error."""
        from datetime import UTC, datetime, timedelta

        service, index, mirror, _session = _setup(
            server_folders={"INBOX": _server_message(1, "Old trash")}
        )
        service.sync()
        row = index.list_folder_messages(folder=_PERSONAL_INBOX)[0]

        long_ago = datetime.now(tz=UTC) - timedelta(days=365)
        index.update_message(
            message=dataclasses.replace(
                row,
                local_status=MessageStatus.TRASHED,
                trashed_at=long_ago,
            )
        )
        # Remove the file behind its back.
        mirror.delete_message(folder=_PERSONAL_INBOX, storage_key=row.storage_key)

        service._run_cleanup()

        self.assertIsNone(index.get_message(message_ref=row.message_ref))
