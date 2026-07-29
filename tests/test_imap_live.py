"""Sync-engine scenarios against a real IMAP server.

Skipped entirely unless ``PONY_LIVE_IMAP=1``; see ``tests/imap_live.py``.

These cover the decisions a fake session makes for itself and a server
makes for real: what a UID is, when UIDVALIDITY changes, and what APPEND
answers.  The UIDVALIDITY recovery path depends on all three, and it is
the path that used to delete the user's only copy of a message.
"""

from __future__ import annotations

import unittest
from email.message import EmailMessage
from uuid import uuid4

from conftest import TMP_ROOT
from imap_live import Dovecot, FixedCredentials, skip_reason

from pony.accounts import build_mirror
from pony.domain import FolderRef, MessageFlag, MessageStatus
from pony.index_store import SqliteIndexRepository
from pony.sync import ImapSyncService

_SKIP = skip_reason()


def _message(subject: str, message_id: str | None = None) -> bytes:
    message = EmailMessage()
    message["From"] = "alice@example.test"
    message["To"] = "bob@example.test"
    message["Subject"] = subject
    message["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content(f"Body of {subject}.")
    return message.as_bytes()


@unittest.skipIf(_SKIP is not None, _SKIP or "")
class LiveImapTestCase(unittest.TestCase):
    """One clean server and one clean index per test."""

    server: Dovecot

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Dovecot()
        cls.server.ensure_installed()
        cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.server.reset()
        root = TMP_ROOT / "imap-live" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.account = self.server.account(name="live", mirror_root=root / "mirror")
        self.index = SqliteIndexRepository(database_path=root / "index.sqlite3")
        self.index.initialize()
        self.mirror = build_mirror(self.account)
        self.service = ImapSyncService(
            config=self.server.config(self.account),
            mirror_factory=lambda _account: self.mirror,
            index=self.index,
            credentials=FixedCredentials(),
            session_factory=self.server.session_factory(),
        )
        self.inbox = FolderRef(account_name="live", folder_name="INBOX")

    # -- helpers ---------------------------------------------------------

    def _deliver(self, *messages: bytes, folder: str = "INBOX") -> None:
        session = self.server.raw_session()
        try:
            if folder != "INBOX":
                session.create_folder(folder)
            for raw in messages:
                session.append_message(folder, raw, frozenset())
        finally:
            session.logout()

    def _server_subjects(self, folder: str = "INBOX") -> set[str]:
        session = self.server.raw_session()
        try:
            uids = session.fetch_uid_to_message_id(folder)
            subjects = set()
            for uid in uids:
                raw = session.fetch_message_bytes(folder, uid)
                for line in raw.split(b"\n"):
                    if line.startswith(b"Subject:"):
                        subjects.add(line.split(b": ", 1)[1].decode().strip())
                        break
            return subjects
        finally:
            session.logout()

    def _rows(self, folder: str = "INBOX") -> list:  # type: ignore[type-arg]
        return list(
            self.index.list_folder_messages(
                folder=FolderRef(account_name="live", folder_name=folder)
            )
        )

    # -- scenarios -------------------------------------------------------

    def test_a_first_sync_downloads_and_indexes(self) -> None:
        self._deliver(
            _message("one", "<one@example.test>"),
            _message("two", "<two@example.test>"),
        )
        self.service.sync()

        rows = self._rows()
        self.assertEqual({r.subject for r in rows}, {"one", "two"})
        self.assertTrue(all(r.uid is not None for r in rows))
        self.assertEqual(len(self.mirror.list_messages(folder=self.inbox)), 2)

    def test_a_uidvalidity_change_keeps_the_mail_and_readopts_the_uids(self) -> None:
        """The event that used to delete every row and its mirror file."""
        self._deliver(*(_message(f"m{i}", f"<m{i}@example.test>") for i in range(5)))
        self.service.sync()
        before = {r.message_ref.id for r in self._rows()}

        self.server.bump_uidvalidity("INBOX")
        self.service.sync()

        rows = self._rows()
        self.assertEqual(len(rows), 5, "messages were duplicated or lost")
        self.assertEqual(
            {r.message_ref.id for r in rows}, before, "rows were replaced, not adopted"
        )
        self.assertTrue(all(r.uid is not None for r in rows), "UIDs not re-adopted")
        self.assertEqual(len(self.mirror.list_messages(folder=self.inbox)), 5)

    def test_a_uidvalidity_change_on_an_emptied_folder_keeps_local_mail(self) -> None:
        """A restore-from-backup that lost mail must not lose the local copy."""
        self._deliver(*(_message(f"k{i}", f"<k{i}@example.test>") for i in range(4)))
        self.service.sync()

        session = self.server.raw_session()
        try:
            for uid in session.fetch_uid_to_message_id("INBOX"):
                session.mark_deleted("INBOX", uid)
            session.expunge("INBOX")
        finally:
            session.logout()
        self.server.bump_uidvalidity("INBOX")
        self.service.sync()

        self.assertEqual(len(self._rows()), 4, "local mail was deleted")
        self.assertEqual(len(self.mirror.list_messages(folder=self.inbox)), 4)

    def test_a_local_flag_survives_a_uidvalidity_change(self) -> None:
        import dataclasses

        self._deliver(_message("flagme", "<flagme@example.test>"))
        self.service.sync()
        row = self._rows()[0]
        self.index.update_message(
            message=dataclasses.replace(
                row, local_flags=frozenset({MessageFlag.FLAGGED})
            )
        )

        self.server.bump_uidvalidity("INBOX")
        self.service.sync()

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertIn(MessageFlag.FLAGGED, rows[0].local_flags)

    def test_a_pending_deletion_survives_and_then_reaches_the_server(self) -> None:
        import dataclasses

        self._deliver(
            _message("keep", "<keep@example.test>"),
            _message("drop", "<drop@example.test>"),
        )
        self.service.sync()
        doomed = next(r for r in self._rows() if r.subject == "drop")
        self.index.update_message(
            message=dataclasses.replace(doomed, local_status=MessageStatus.TRASHED)
        )

        self.server.bump_uidvalidity("INBOX")
        self.service.sync()
        # It kept its intent and regained a server handle...
        same = [r for r in self._rows() if r.message_id == doomed.message_id]
        self.assertEqual(len(same), 1, "the message came back as a second row")
        self.assertEqual(same[0].local_status, MessageStatus.TRASHED)

        # ...so the next pass can finally push the deletion.
        self.service.sync()
        self.assertEqual(self._server_subjects(), {"keep"})

    def test_messages_without_a_message_id_are_not_duplicated(self) -> None:
        """Adoption cannot match on Message-ID when there is none."""
        self._deliver(_message("nomid one"), _message("nomid two"))
        self.service.sync()

        self.server.bump_uidvalidity("INBOX")
        self.service.sync()

        rows = self._rows()
        self.assertEqual(len(rows), 2, "duplicated without a Message-ID to match on")
        self.assertTrue(all(r.uid is not None for r in rows))

    def test_an_append_round_trips_through_the_server(self) -> None:
        """A locally composed message is uploaded and keeps one row."""
        from pony.domain import MessageRef
        from pony.message_projection import project_rfc822_message

        self.service.sync()
        raw = _message("composed", "<composed@example.test>")
        storage_key = self.mirror.store_message(folder=self.inbox, raw_message=raw)
        self.index.insert_message(
            message=project_rfc822_message(
                message_ref=MessageRef(account_name="live", folder_name="INBOX", id=0),
                raw_message=raw,
                storage_key=storage_key,
            )
        )

        self.service.sync()
        self.assertIn("composed", self._server_subjects())
        self.service.sync()
        rows = [r for r in self._rows() if r.subject == "composed"]
        self.assertEqual(len(rows), 1, "the appended message came back as a duplicate")
        self.assertIsNotNone(rows[0].uid, "no UID captured from APPEND")

    def test_a_folder_of_two_hundred_messages_adopts_cleanly(self) -> None:
        """Adoption is per-message; make sure it holds up past a handful."""
        self._deliver(
            *(_message(f"bulk {i}", f"<bulk{i}@example.test>") for i in range(200))
        )
        self.service.sync()
        self.assertEqual(len(self._rows()), 200)

        self.server.bump_uidvalidity("INBOX")
        self.service.sync()

        rows = self._rows()
        self.assertEqual(len(rows), 200, "bulk adoption duplicated or lost rows")
        self.assertTrue(all(r.uid is not None for r in rows))
