"""Tests for pony.mailbox_ops.

These two row rewrites *are* the local-mutation half of the sync
contract: the planner has no queue to consult, only the row (see
``docs/synchronization.md``). Each was open-coded at every call site --
"arrived in a folder" in three places, "moved to a folder" in two -- so
the contract held only as well as the least careful copy.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from pony.domain import (
    FolderRef,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
)
from pony.mailbox_ops import landed_in_folder, moved_to_folder

SOURCE = FolderRef(account_name="work", folder_name="INBOX")
TARGET = FolderRef(account_name="work", folder_name="Archive")
OTHER_ACCOUNT = FolderRef(account_name="personal", folder_name="INBOX")


def _synced_message() -> IndexedMessage:
    """A row as it looks after a successful sync: known to the server."""
    return IndexedMessage(
        message_ref=MessageRef(account_name="work", folder_name="INBOX", id=42),
        sender="alice@example.com",
        recipients="me@example.com",
        cc="",
        subject="Tuesday meeting",
        body_preview="preview",
        received_at=datetime(2026, 4, 17, 12, 0, tzinfo=UTC),
        message_id="<original@example.com>",
        storage_key="key-1",
        uid=1234,
        uid_validity=99,
        base_flags=frozenset({MessageFlag.SEEN}),
        server_flags=frozenset({MessageFlag.SEEN}),
        extra_imap_flags=frozenset({"$Important"}),
        local_flags=frozenset({MessageFlag.SEEN}),
        local_status=MessageStatus.ACTIVE,
        synced_at=datetime(2026, 4, 17, 12, 5, tzinfo=UTC),
    )


class LandedInFolderTest(unittest.TestCase):
    """A message written into a folder as genuinely new local mail."""

    def test_it_is_addressed_to_the_target_folder(self) -> None:
        row = landed_in_folder(_synced_message(), target=TARGET, storage_key="new-key")
        self.assertEqual(row.message_ref.account_name, "work")
        self.assertEqual(row.message_ref.folder_name, "Archive")
        self.assertEqual(row.storage_key, "new-key")

    def test_it_is_a_fresh_row_for_insertion(self) -> None:
        # id=0 marks it as not-yet-inserted; the caller inserts rather
        # than updating in place.
        row = landed_in_folder(_synced_message(), target=TARGET, storage_key="k")
        self.assertEqual(row.message_ref.id, 0)

    def test_no_server_identity_is_carried_over(self) -> None:
        # The message does not exist upstream yet. uid=None is what makes
        # the sync planner emit a PushAppendOp; keeping the source UID
        # would point the planner at an unrelated server message.
        row = landed_in_folder(_synced_message(), target=TARGET, storage_key="k")
        self.assertIsNone(row.uid)
        self.assertEqual(row.uid_validity, 0)
        self.assertEqual(row.base_flags, frozenset())
        self.assertEqual(row.server_flags, frozenset())
        self.assertEqual(row.extra_imap_flags, frozenset())
        self.assertIsNone(row.synced_at)

    def test_it_is_active_and_not_mid_move(self) -> None:
        mid_move = moved_to_folder(_synced_message(), target=TARGET, storage_key="k1")
        row = landed_in_folder(mid_move, target=OTHER_ACCOUNT, storage_key="k2")
        self.assertEqual(row.local_status, MessageStatus.ACTIVE)
        self.assertIsNone(row.source_folder)
        self.assertIsNone(row.source_uid)
        self.assertIsNone(row.trashed_at)

    def test_local_flags_survive(self) -> None:
        # Re-projecting from the raw bytes instead would reset these,
        # which is how `pony folder mirror` used to mark everything unread.
        row = landed_in_folder(_synced_message(), target=TARGET, storage_key="k")
        self.assertEqual(row.local_flags, frozenset({MessageFlag.SEEN}))

    def test_the_message_id_is_kept_by_default(self) -> None:
        row = landed_in_folder(_synced_message(), target=TARGET, storage_key="k")
        self.assertEqual(row.message_id, "<original@example.com>")

    def test_a_rewritten_message_id_is_applied(self) -> None:
        # A same-account copy rewrites the Message-ID so the two rows are
        # distinct upstream identities.
        row = landed_in_folder(
            _synced_message(),
            target=TARGET,
            storage_key="k",
            message_id="<rewritten@example.com>",
        )
        self.assertEqual(row.message_id, "<rewritten@example.com>")

    def test_a_trashed_source_does_not_arrive_trashed(self) -> None:
        import dataclasses

        trashed = dataclasses.replace(
            _synced_message(),
            local_status=MessageStatus.TRASHED,
            trashed_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        row = landed_in_folder(trashed, target=TARGET, storage_key="k")
        self.assertEqual(row.local_status, MessageStatus.ACTIVE)
        self.assertIsNone(row.trashed_at)


class MovedToFolderTest(unittest.TestCase):
    """A message moved between folders of one account."""

    def test_it_keeps_the_same_row(self) -> None:
        # Same id, same Message-ID: this is an update in place, not a
        # delete-then-insert.
        row = moved_to_folder(_synced_message(), target=TARGET, storage_key="k")
        self.assertEqual(row.message_ref.id, 42)
        self.assertEqual(row.message_id, "<original@example.com>")

    def test_it_is_addressed_to_the_target_folder(self) -> None:
        row = moved_to_folder(_synced_message(), target=TARGET, storage_key="new")
        self.assertEqual(row.message_ref.folder_name, "Archive")
        self.assertEqual(row.storage_key, "new")

    def test_it_records_where_the_server_copy_still_lives(self) -> None:
        # Without these the planner cannot tell the server which message
        # to remove, and the move degrades into a duplicate.
        row = moved_to_folder(_synced_message(), target=TARGET, storage_key="k")
        self.assertEqual(row.local_status, MessageStatus.PENDING_MOVE)
        self.assertEqual(row.source_folder, "INBOX")
        self.assertEqual(row.source_uid, 1234)

    def test_the_uid_is_cleared_so_the_planner_notices(self) -> None:
        row = moved_to_folder(_synced_message(), target=TARGET, storage_key="k")
        self.assertIsNone(row.uid)
        self.assertIsNone(row.synced_at)
        self.assertEqual(row.server_flags, frozenset())
        self.assertEqual(row.extra_imap_flags, frozenset())

    def test_uid_validity_is_preserved(self) -> None:
        # Unlike an arrival, a move stays within the same mailbox
        # generation -- the row still refers to the same server folder
        # lineage until sync resolves it.
        row = moved_to_folder(_synced_message(), target=TARGET, storage_key="k")
        self.assertEqual(row.uid_validity, 99)

    def test_local_flags_survive_a_move(self) -> None:
        row = moved_to_folder(_synced_message(), target=TARGET, storage_key="k")
        self.assertEqual(row.local_flags, frozenset({MessageFlag.SEEN}))

    def test_a_local_only_message_records_no_source_uid(self) -> None:
        # A message that never reached the server has no UID to point at;
        # the planner appends it in its new folder instead.
        import dataclasses

        local_only = dataclasses.replace(_synced_message(), uid=None)
        row = moved_to_folder(local_only, target=TARGET, storage_key="k")
        self.assertIsNone(row.source_uid)
        self.assertEqual(row.source_folder, "INBOX")


class MirrorFlagsTest(unittest.TestCase):
    """Flag changes must reach the mirror, not just the index.

    A Maildir or mbox tree is routinely shared with another MUA. Nothing
    ever wrote flags to disk, so anything Pony marked read still looked
    unread to everything else reading the same mirror.
    """

    def _mirror(self, fmt: str) -> tuple[object, FolderRef, IndexedMessage]:
        import dataclasses
        import tempfile
        from email.message import EmailMessage
        from pathlib import Path

        from pony.storage import MaildirMirrorRepository, MboxMirrorRepository

        cls = MaildirMirrorRepository if fmt == "maildir" else MboxMirrorRepository
        mirror = cls(account_name="work", root_dir=Path(tempfile.mkdtemp()))
        folder = FolderRef(account_name="work", folder_name="INBOX")
        raw = EmailMessage()
        raw["Subject"] = "flagged"
        raw["Message-ID"] = "<flag@example.com>"
        raw.set_content("body")
        key = mirror.store_message(folder=folder, raw_message=raw.as_bytes())
        message = dataclasses.replace(
            _synced_message(),
            message_ref=MessageRef(account_name="work", folder_name="INBOX", id=1),
            storage_key=key,
            local_flags=frozenset({MessageFlag.SEEN}),
        )
        return mirror, folder, message

    def test_maildir_records_the_flag_in_the_filename(self) -> None:
        from pony.mailbox_ops import mirror_flags

        mirror, _folder, message = self._mirror("maildir")
        self.assertTrue(mirror_flags(mirror, message))  # type: ignore[arg-type]
        names = [p.name for p in (mirror._root_dir / "cur").iterdir()]  # type: ignore[attr-defined]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith("!2,S"), names[0])

    def test_the_storage_key_survives_the_write(self) -> None:
        # The file is renamed to carry the flags; if that changed the key
        # the index row would point at nothing and a rescan would treat
        # the message as deleted-and-re-added.
        from pony.mailbox_ops import mirror_flags

        mirror, folder, message = self._mirror("maildir")
        mirror_flags(mirror, message)  # type: ignore[arg-type]
        self.assertIn(message.storage_key, mirror.list_messages(folder=folder))  # type: ignore[attr-defined]
        self.assertTrue(
            mirror.get_message_bytes(  # type: ignore[attr-defined]
                folder=folder, storage_key=message.storage_key
            )
        )

    def test_mbox_records_the_flag_in_the_status_header(self) -> None:
        from pony.mailbox_ops import mirror_flags

        mirror, folder, message = self._mirror("mbox")
        self.assertTrue(mirror_flags(mirror, message))  # type: ignore[arg-type]
        raw = mirror.get_message_bytes(  # type: ignore[attr-defined]
            folder=folder, storage_key=message.storage_key
        )
        self.assertIn(b"Status:", raw)

    def test_clearing_a_flag_is_written_too(self) -> None:
        import dataclasses

        from pony.mailbox_ops import mirror_flags

        mirror, _folder, message = self._mirror("maildir")
        mirror_flags(mirror, message)  # type: ignore[arg-type]
        cleared = dataclasses.replace(message, local_flags=frozenset())
        self.assertTrue(mirror_flags(mirror, cleared))  # type: ignore[arg-type]
        names = [p.name for p in (mirror._root_dir / "cur").iterdir()]  # type: ignore[attr-defined]
        self.assertTrue(names[0].endswith("!2,"), names[0])

    def test_a_missing_file_is_reported_not_raised(self) -> None:
        # The index row is authoritative and already written; a sync
        # racing the same message must not turn a successful flag change
        # into an error the user sees.
        import dataclasses

        from pony.mailbox_ops import mirror_flags

        mirror, _folder, message = self._mirror("maildir")
        vanished = dataclasses.replace(message, storage_key="not-a-real-key")
        self.assertFalse(mirror_flags(mirror, vanished))  # type: ignore[arg-type]
