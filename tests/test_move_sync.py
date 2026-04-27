"""End-to-end move-action tests that drive the sync engine.

These tests simulate what the TUI move action does locally, then run
the sync engine against :class:`FakeImapSession` and assert the server
state matches the expected move semantics:

- Same-account: ``UID MOVE`` on the server; source folder empty,
  target folder holds the message with the original Message-ID.
- Cross-account (IMAP → IMAP): target account ``APPEND``; source
  account ``STORE +\\Deleted`` + ``EXPUNGE``; both ends converge on
  "message only in target", Message-ID preserved throughout.
"""

from __future__ import annotations

import dataclasses
import unittest
from uuid import uuid4

from conftest import TMP_ROOT
from test_sync import FakeImapSession, _make_raw_message

from pony.domain import (
    AccountConfig,
    AppConfig,
    FolderRef,
    MessageRef,
    MessageStatus,
    MirrorConfig,
    SmtpConfig,
)
from pony.index_store import SqliteIndexRepository
from pony.message_copy import copy_message_bytes
from pony.protocols import ImapClientSession, MirrorRepository
from pony.storage import MaildirMirrorRepository
from pony.sync import ImapSyncService


def _simulate_tui_move_same_account(
    *,
    index: SqliteIndexRepository,
    mirror: MirrorRepository,
    source: FolderRef,
    target: FolderRef,
    source_storage_key: str,
) -> None:
    """Run the same local-side operations ``MainScreen._move_same_account`` does."""
    new_key = mirror.move_message_to_folder(
        folder=source,
        storage_key=source_storage_key,
        target_folder=target.folder_name,
    )
    [source_row] = [
        m for m in index.list_folder_messages(folder=source)
        if m.storage_key == source_storage_key
    ]
    # Same-account move: keep the row, mark PENDING_MOVE.
    updated = dataclasses.replace(
        source_row,
        message_ref=MessageRef(
            account_name=target.account_name,
            folder_name=target.folder_name,
            id=source_row.message_ref.id,
        ),
        storage_key=new_key,
        uid=None,
        server_flags=frozenset(),
        extra_imap_flags=frozenset(),
        synced_at=None,
        local_status=MessageStatus.PENDING_MOVE,
        source_folder=source.folder_name,
        source_uid=source_row.uid,
    )
    index.update_message(message=updated)


def _simulate_tui_move_cross_account(
    *,
    index: SqliteIndexRepository,
    source_mirror: MirrorRepository,
    target_mirror: MirrorRepository,
    source: FolderRef,
    target: FolderRef,
    source_storage_key: str,
) -> str:
    """Run the same local-side operations
    ``MainScreen._move_cross_account`` does when the source is IMAP.

    Returns the Message-ID of the new target-side row (which for
    cross-account moves must equal the source MID)."""
    raw = source_mirror.get_message_bytes(
        folder=source, storage_key=source_storage_key,
    )
    new_raw, new_mid = copy_message_bytes(raw, rewrite_message_id=False)
    new_key = target_mirror.store_message(folder=target, raw_message=new_raw)

    [source_row] = [
        m for m in index.list_folder_messages(folder=source)
        if m.storage_key == source_storage_key
    ]
    target_row = dataclasses.replace(
        source_row,
        message_ref=MessageRef(
            account_name=target.account_name,
            folder_name=target.folder_name,
            id=0,
        ),
        message_id=new_mid,
        storage_key=new_key,
        uid=None,
        uid_validity=0,
        base_flags=frozenset(),
        server_flags=frozenset(),
        extra_imap_flags=frozenset(),
        local_status=MessageStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
        source_folder=None,
        source_uid=None,
    )
    index.insert_message(message=target_row)

    # Trash the source (IMAP) so the next sync EXPUNGEs it server-side.
    trashed = dataclasses.replace(source_row, local_status=MessageStatus.TRASHED)
    index.update_message(message=trashed)
    return new_mid


class SameAccountMoveTest(unittest.TestCase):
    """Same-account move: sync emits ``UID MOVE``; Message-ID preserved."""

    def _setup(
        self,
    ) -> tuple[
        ImapSyncService,
        SqliteIndexRepository,
        MaildirMirrorRepository,
        FakeImapSession,
    ]:
        tmp = TMP_ROOT / "move-sync-one" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        mirror = MaildirMirrorRepository(
            account_name="personal", root_dir=tmp / "mirror",
        )
        index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
        index.initialize()
        account = AccountConfig(
            name="personal",
            email_address="alice@example.com",
            imap_host="imap.example.com",
            smtp=SmtpConfig(host="smtp.example.com"),
            username="alice",
            credentials_source="plaintext",
            mirror=MirrorConfig(path=tmp / "mirror", format="maildir"),
        )
        config = AppConfig(accounts=(account,))

        raw = _make_raw_message("Hello", "<orig-move-same@example.com>")
        session = FakeImapSession(
            folders={
                "INBOX": {1: ("<orig-move-same@example.com>", frozenset(), raw)},
                "Filed": {},
            },
        )

        class _Creds:
            def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
                return "x"

        def _mirror_factory(_acc: AccountConfig) -> MirrorRepository:
            return mirror

        def _session_factory(
            _acc: AccountConfig, _pw: str,
        ) -> ImapClientSession:
            return session

        service = ImapSyncService(
            config=config,
            mirror_factory=_mirror_factory,
            index=index,
            credentials=_Creds(),
            session_factory=_session_factory,
        )
        return service, index, mirror, session

    def test_move_to_other_folder_emits_uid_move(self) -> None:
        service, index, mirror, session = self._setup()
        service.sync()

        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        filed = FolderRef(account_name="personal", folder_name="Filed")
        [orig_row] = index.list_folder_messages(folder=inbox)

        _simulate_tui_move_same_account(
            index=index,
            mirror=mirror,
            source=inbox,
            target=filed,
            source_storage_key=orig_row.storage_key,
        )
        # Post-move local state: source folder empty, target has pending row.
        self.assertEqual(list(index.list_folder_messages(folder=inbox)), [])
        self.assertEqual(len(list(index.list_folder_messages(folder=filed))), 1)

        service.sync()

        # Server-side: source empty, target holds the message with
        # its original Message-ID (MOVE preserves identity).
        self.assertEqual(len(session.folders["INBOX"]), 0)
        self.assertEqual(len(session.folders["Filed"]), 1)
        [(filed_mid, _, _)] = list(session.folders["Filed"].values())
        self.assertEqual(filed_mid, "<orig-move-same@example.com>")
        # Sync used UID MOVE, not APPEND.
        self.assertTrue(any(
            call.startswith("move:INBOX->Filed") for call in session.call_log
        ))
        self.assertFalse(any(
            call.startswith("append:") for call in session.call_log
        ))


class CrossAccountMoveTest(unittest.TestCase):
    """Cross-account move: APPEND on target, EXPUNGE on source; MID preserved."""

    def _setup(
        self,
    ) -> tuple[
        ImapSyncService,
        SqliteIndexRepository,
        dict[str, MaildirMirrorRepository],
        dict[str, FakeImapSession],
    ]:
        tmp = TMP_ROOT / "move-sync-two" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        mirror_a = MaildirMirrorRepository(
            account_name="work", root_dir=tmp / "mirror-a",
        )
        mirror_b = MaildirMirrorRepository(
            account_name="personal", root_dir=tmp / "mirror-b",
        )
        mirrors: dict[str, MaildirMirrorRepository] = {
            "work": mirror_a, "personal": mirror_b,
        }
        index = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
        index.initialize()

        work = AccountConfig(
            name="work",
            email_address="alice@work.example.com",
            imap_host="imap.work.example.com",
            smtp=SmtpConfig(host="smtp.work.example.com"),
            username="alice",
            credentials_source="plaintext",
            mirror=MirrorConfig(path=tmp / "mirror-a", format="maildir"),
        )
        personal = AccountConfig(
            name="personal",
            email_address="alice@personal.example.com",
            imap_host="imap.personal.example.com",
            smtp=SmtpConfig(host="smtp.personal.example.com"),
            username="alice",
            credentials_source="plaintext",
            mirror=MirrorConfig(path=tmp / "mirror-b", format="maildir"),
        )
        config = AppConfig(accounts=(work, personal))

        raw = _make_raw_message("Cross move", "<orig-move-cross@example.com>")
        session_work = FakeImapSession(
            folders={"INBOX": {
                1: ("<orig-move-cross@example.com>", frozenset(), raw),
            }},
        )
        session_personal = FakeImapSession(folders={"INBOX": {}})
        sessions = {"work": session_work, "personal": session_personal}

        class _Creds:
            def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
                return "x"

        def _mirror_factory(acc: AccountConfig) -> MirrorRepository:
            return mirrors[acc.name]

        def _session_factory(
            acc: AccountConfig, _pw: str,
        ) -> ImapClientSession:
            return sessions[acc.name]

        service = ImapSyncService(
            config=config,
            mirror_factory=_mirror_factory,
            index=index,
            credentials=_Creds(),
            session_factory=_session_factory,
        )
        return service, index, mirrors, sessions

    def test_cross_account_move_appends_then_expunges(self) -> None:
        service, index, mirrors, sessions = self._setup()
        service.sync()

        work_inbox = FolderRef(account_name="work", folder_name="INBOX")
        personal_inbox = FolderRef(account_name="personal", folder_name="INBOX")
        [work_row] = index.list_folder_messages(folder=work_inbox)

        new_mid = _simulate_tui_move_cross_account(
            index=index,
            source_mirror=mirrors["work"],
            target_mirror=mirrors["personal"],
            source=work_inbox,
            target=personal_inbox,
            source_storage_key=work_row.storage_key,
        )
        # Invariant: cross-account move preserves Message-ID.
        self.assertEqual(new_mid, "<orig-move-cross@example.com>")

        service.sync()

        # Post-sync server state: work's INBOX is empty, personal's
        # INBOX has the message with the original MID.
        self.assertEqual(len(sessions["work"].folders["INBOX"]), 0)
        self.assertEqual(len(sessions["personal"].folders["INBOX"]), 1)
        [(personal_mid, _, _)] = list(
            sessions["personal"].folders["INBOX"].values(),
        )
        self.assertEqual(personal_mid, "<orig-move-cross@example.com>")
        # And the wire-protocol calls happened on the right sessions.
        self.assertTrue(any(
            call == "append:INBOX" for call in sessions["personal"].call_log
        ))
        self.assertIn(1, sessions["work"].deleted_uids)
