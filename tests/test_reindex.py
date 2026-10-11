"""Rebuilding the index from the mirror, with no server involved."""

from __future__ import annotations

import unittest
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import FolderRef, MessageFlag
from pony.index_store import SqliteIndexRepository
from pony.storage import MaildirMirrorRepository
from pony.storage_indexing import reindex_account_from_mirror

INBOX = FolderRef(account_name="personal", folder_name="INBOX")


def _raw(subject: str) -> bytes:
    return (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: " + subject.encode() + b"\r\n"
        b"Message-ID: <" + subject.encode() + b"@example.com>\r\n"
        b"Date: Fri, 10 Apr 2026 10:00:00 +0000\r\n"
        b"\r\n"
        b"Body.\r\n"
    )


class ReindexFromMirrorTest(unittest.TestCase):
    """The mirror is self-describing; the index is derivable from it."""

    def setUp(self) -> None:
        root = TMP_ROOT / "reindex" / uuid4().hex
        self.mirror = MaildirMirrorRepository(
            account_name="personal",
            root_dir=root / "mirror",
        )
        self.index = SqliteIndexRepository(database_path=root / "index.sqlite3")
        self.index.initialize()
        self.addCleanup(self.index.close)

    def _store(self, subject: str, *, uid: int | None, uid_validity: int = 0) -> str:
        return self.mirror.store_message(
            folder=INBOX,
            raw_message=_raw(subject),
            uid=uid,
            uid_validity=uid_validity,
        )

    def _reindex(self):
        return reindex_account_from_mirror(
            mirror_repository=self.mirror,
            index_repository=self.index,
            account_name="personal",
        )

    def test_a_fetched_message_recovers_its_uid_from_its_name(self) -> None:
        self._store("alpha", uid=42, uid_validity=7)

        result = self._reindex()

        (row,) = self.index.list_folder_messages(folder=INBOX)
        self.assertEqual(row.uid, 42)
        self.assertEqual(row.subject, "alpha")
        self.assertEqual(result.with_uid, 1)
        self.assertEqual(result.local_only, 0)

    def test_the_folder_watermark_comes_back_too(self) -> None:
        """Without it the next sync would not know which epoch it is in."""
        for uid in (3, 11, 7):
            self._store(f"m{uid}", uid=uid, uid_validity=7)

        self._reindex()

        state = self.index.get_folder_sync_state(
            account_name="personal",
            folder_name="INBOX",
        )
        assert state is not None
        self.assertEqual(state.uid_validity, 7)
        self.assertEqual(state.highest_uid, 11)
        # UIDNEXT is the server's to report; unknown keeps the next sync
        # on the reconciling path.
        self.assertEqual(state.uidnext, 0)

    def test_a_message_with_no_handle_is_indexed_as_local_only(self) -> None:
        """Which is exactly what makes the next sync upload it."""
        self._store("draft", uid=None)

        result = self._reindex()

        (row,) = self.index.list_folder_messages(folder=INBOX)
        self.assertIsNone(row.uid)
        self.assertEqual(result.local_only, 1)

    def test_a_handle_from_a_superseded_epoch_is_not_trusted(self) -> None:
        """A UID means nothing once UIDVALIDITY has changed."""
        self._store("old", uid=5, uid_validity=7)
        self._store("new", uid=2, uid_validity=9)

        self._reindex()

        by_subject = {
            row.subject: row for row in self.index.list_folder_messages(folder=INBOX)
        }
        self.assertEqual(by_subject["new"].uid, 2)
        self.assertIsNone(by_subject["old"].uid)
        state = self.index.get_folder_sync_state(
            account_name="personal",
            folder_name="INBOX",
        )
        assert state is not None
        self.assertEqual(state.uid_validity, 9)
        self.assertEqual(state.highest_uid, 2)

    def test_running_it_twice_adds_nothing(self) -> None:
        self._store("alpha", uid=1, uid_validity=7)

        first = self._reindex()
        second = self._reindex()

        self.assertEqual(first.added, 1)
        self.assertEqual(second.added, 0)
        self.assertEqual(len(self.index.list_folder_messages(folder=INBOX)), 1)

    def test_it_keeps_the_rows_that_are_already_there(self) -> None:
        """A partial index is completed, not replaced."""
        key = self._store("kept", uid=1, uid_validity=7)
        self._store("missing", uid=2, uid_validity=7)
        import dataclasses

        from pony.domain import MessageRef
        from pony.message_projection import project_rfc822_message

        projected = project_rfc822_message(
            message_ref=MessageRef(
                account_name="personal",
                folder_name="INBOX",
                id=0,
            ),
            raw_message=_raw("kept"),
            storage_key=key,
        )
        saved = self.index.insert_message(
            message=dataclasses.replace(
                projected,
                uid=1,
                local_flags=frozenset({MessageFlag.FLAGGED}),
            ),
        )

        result = self._reindex()

        self.assertEqual(result.added, 1)
        kept = self.index.get_message(message_ref=saved.message_ref)
        assert kept is not None
        # The flag it already carried survived.
        self.assertIn(MessageFlag.FLAGGED, kept.local_flags)

    def test_every_folder_in_the_mirror_is_covered(self) -> None:
        other = FolderRef(account_name="personal", folder_name="Archive")
        self.mirror.create_folder(account_name="personal", folder_name="Archive")
        self.mirror.store_message(
            folder=other,
            raw_message=_raw("filed"),
            uid=4,
            uid_validity=7,
        )
        self._store("inboxed", uid=1, uid_validity=7)

        result = self._reindex()

        self.assertEqual(result.added, 2)
        self.assertGreaterEqual(result.folders, 2)
        self.assertEqual(len(self.index.list_folder_messages(folder=other)), 1)
