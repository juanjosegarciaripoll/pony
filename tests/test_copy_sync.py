"""End-to-end copy-action tests that drive the sync engine.

These tests simulate what the TUI copy action does locally (write bytes
into the target mirror, insert a ``uid=NULL`` index row), then run the
sync engine against a :class:`FakeImapSession` and assert the server
state matches the expected copy semantics:

- Same-account: Message-ID rewritten to a synthetic id; source survives
  on the server untouched; target folder gains a new message.
- Cross-account: Message-ID preserved; both servers hold a message with
  the same MID in their respective folders.
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


def _simulate_tui_copy(
    *,
    index: SqliteIndexRepository,
    source_mirror: MirrorRepository,
    target_mirror: MirrorRepository,
    source: FolderRef,
    target: FolderRef,
    source_storage_key: str,
) -> tuple[bytes, str]:
    """Run the same local-side operations the MainScreen copy action does.

    Returns ``(new_raw, new_message_id)`` so tests can assert against the
    bytes that ultimately hit the target server via APPEND.
    """
    raw = source_mirror.get_message_bytes(
        folder=source, storage_key=source_storage_key,
    )
    rewrite = target.account_name == source.account_name
    new_raw, new_mid = copy_message_bytes(raw, rewrite_message_id=rewrite)
    new_key = target_mirror.store_message(folder=target, raw_message=new_raw)

    [source_row] = [
        m for m in index.list_folder_messages(folder=source)
        if m.storage_key == source_storage_key
    ]
    new_row = dataclasses.replace(
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
    index.insert_message(message=new_row)
    return new_raw, new_mid


class SameAccountCopyTest(unittest.TestCase):
    """Copying within one account rewrites the MID so sync APPENDs
    the copy rather than interpreting it as a move."""

    def _setup_one_account(
        self,
    ) -> tuple[
        ImapSyncService,
        SqliteIndexRepository,
        MaildirMirrorRepository,
        FakeImapSession,
    ]:
        tmp = TMP_ROOT / "copy-sync-one" / uuid4().hex
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

        raw = _make_raw_message("Hello", "<orig-same@example.com>")
        session = FakeImapSession(
            folders={
                "INBOX": {1: ("<orig-same@example.com>", frozenset(), raw)},
                "Archive": {},
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

    def test_copy_to_other_folder_appends_with_new_mid(self) -> None:
        service, index, mirror, session = self._setup_one_account()

        # 1. Initial sync to ingest the one server-side message.
        service.sync()
        inbox = FolderRef(account_name="personal", folder_name="INBOX")
        archive = FolderRef(account_name="personal", folder_name="Archive")
        [orig_row] = index.list_folder_messages(folder=inbox)
        self.assertEqual(orig_row.message_id, "<orig-same@example.com>")

        # 2. TUI-side copy to Archive (same account → MID rewritten).
        _, new_mid = _simulate_tui_copy(
            index=index,
            source_mirror=mirror,
            target_mirror=mirror,
            source=inbox,
            target=archive,
            source_storage_key=orig_row.storage_key,
        )
        self.assertTrue(new_mid.startswith("<pony-copy-"))
        self.assertNotEqual(new_mid, "<orig-same@example.com>")

        # 3. Second sync should APPEND the copy to the server's Archive
        #    folder — NOT move it from INBOX.
        service.sync()

        inbox_server = session.folders["INBOX"]
        archive_server = session.folders["Archive"]
        # Source untouched: original UID and MID still in INBOX.
        self.assertEqual(len(inbox_server), 1)
        [(_orig_mid, _, _)] = list(inbox_server.values())
        self.assertEqual(_orig_mid, "<orig-same@example.com>")
        # Archive has exactly one new message with the synthetic MID.
        self.assertEqual(len(archive_server), 1)
        [(copy_mid, _, _)] = list(archive_server.values())
        self.assertEqual(copy_mid, new_mid)
        # And no server-side MOVE was issued — the copy arrived via APPEND.
        self.assertFalse(any(call.startswith("move:") for call in session.call_log))
        self.assertTrue(any(call == "append:Archive" for call in session.call_log))


class CrossAccountCopyTest(unittest.TestCase):
    """Copying across accounts preserves the Message-ID — the two
    accounts are independent identity namespaces."""

    def _setup_two_accounts(
        self,
    ) -> tuple[
        ImapSyncService,
        SqliteIndexRepository,
        dict[str, MaildirMirrorRepository],
        dict[str, FakeImapSession],
    ]:
        tmp = TMP_ROOT / "copy-sync-two" / uuid4().hex
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

        raw = _make_raw_message("Cross", "<orig-cross@example.com>")
        session_work = FakeImapSession(
            folders={
                "INBOX": {1: ("<orig-cross@example.com>", frozenset(), raw)},
            },
        )
        session_personal = FakeImapSession(
            folders={"INBOX": {}},
        )
        sessions: dict[str, FakeImapSession] = {
            "work": session_work, "personal": session_personal,
        }

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

    def test_copy_across_accounts_preserves_mid(self) -> None:
        service, index, mirrors, sessions = self._setup_two_accounts()

        # 1. Initial sync: pulls the work-side message into the index.
        service.sync()
        work_inbox = FolderRef(account_name="work", folder_name="INBOX")
        personal_inbox = FolderRef(account_name="personal", folder_name="INBOX")
        [work_row] = index.list_folder_messages(folder=work_inbox)

        # 2. Simulate TUI copy work/INBOX → personal/INBOX.
        _, new_mid = _simulate_tui_copy(
            index=index,
            source_mirror=mirrors["work"],
            target_mirror=mirrors["personal"],
            source=work_inbox,
            target=personal_inbox,
            source_storage_key=work_row.storage_key,
        )
        # Invariant the test exists to protect: cross-account keeps MID.
        self.assertEqual(new_mid, "<orig-cross@example.com>")

        # 3. Second sync: personal's Step 5 should APPEND the bytes to
        #    the personal server's INBOX.
        service.sync()

        work_server = sessions["work"].folders["INBOX"]
        personal_server = sessions["personal"].folders["INBOX"]
        # Work side is untouched: same MID still there.
        self.assertEqual(len(work_server), 1)
        [(work_mid, _, _)] = list(work_server.values())
        self.assertEqual(work_mid, "<orig-cross@example.com>")
        # Personal side got the APPEND with the SAME MID.
        self.assertEqual(len(personal_server), 1)
        [(personal_mid, _, _)] = list(personal_server.values())
        self.assertEqual(personal_mid, "<orig-cross@example.com>")
        # Personal account's session saw an APPEND.
        self.assertTrue(any(
            call == "append:INBOX" for call in sessions["personal"].call_log
        ))
