"""Conformance tests for mirror storage backends."""

from __future__ import annotations

import unittest
from email.message import EmailMessage
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import FolderRef, MessageFlag
from pony.protocols import MirrorRepository
from pony.storage import MaildirMirrorRepository, MboxMirrorRepository


def _rfc5322_message_bytes(subject: str, message_id: str) -> bytes:
    """Fixture bytes with an explicit RFC 5322 Message-ID header."""
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "user@example.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
    message.set_content("sample body")
    return message.as_bytes()


class MirrorRepositoryConformanceMixin(unittest.TestCase):
    """Shared test cases that every mirror backend must pass.

    Inheriting from :class:`unittest.TestCase` makes all assertion methods
    available to the type checker without a ``TYPE_CHECKING`` guard.

    ``make_repository`` calls ``self.skipTest`` so that if the test runner
    discovers this class directly (which it will, because it is a ``TestCase``
    subclass), each test is reported as *skipped* rather than failing with
    ``NotImplementedError``.  ``skipTest`` is declared ``NoReturn`` in
    typeshed, which satisfies the ``-> MirrorRepository`` return type.

    Concrete test classes override ``make_repository`` and inherit nothing
    else from ``unittest.TestCase`` directly — they rely on this mixin's
    inheritance, avoiding diamond-MRO issues.
    """

    account_name = "personal"

    def make_repository(self) -> MirrorRepository:
        self.skipTest(
            "MirrorRepositoryConformanceMixin is abstract — override make_repository"
        )

    def test_store_list_read_delete_cycle(self) -> None:
        repository = self.make_repository()
        folder = FolderRef(account_name=self.account_name, folder_name="INBOX")

        storage_key = repository.store_message(
            folder=folder, raw_message=sample_message_bytes("hello")
        )
        listed = repository.list_messages(folder=folder)
        self.assertEqual(listed, (storage_key,))

        payload = repository.get_message_bytes(
            folder=folder,
            storage_key=storage_key,
        )
        self.assertIn(b"Subject: hello", payload)

        repository.delete_message(folder=folder, storage_key=storage_key)
        self.assertEqual(repository.list_messages(folder=folder), ())

    def test_set_flags_roundtrip(self) -> None:
        repository = self.make_repository()
        folder = FolderRef(account_name=self.account_name, folder_name="INBOX")
        storage_key = repository.store_message(
            folder=folder, raw_message=sample_message_bytes("flag-test")
        )

        repository.set_flags(
            folder=folder,
            storage_key=storage_key,
            flags=frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
        )
        # Maildir may rename the file (adding a flag suffix), changing the
        # "storage_key" the backend exposes via list_messages.  Re-read
        # via list_messages to get the currently-valid key.
        updated_keys = repository.list_messages(folder=folder)
        self.assertEqual(len(updated_keys), 1)
        payload = repository.get_message_bytes(
            folder=folder,
            storage_key=updated_keys[0],
        )
        self.assertIn(b"Subject: flag-test", payload)

    def test_move_message_to_folder_relocates_bytes(self) -> None:
        repository = self.make_repository()
        inbox = FolderRef(account_name=self.account_name, folder_name="INBOX")
        storage_key = repository.store_message(
            folder=inbox,
            raw_message=sample_message_bytes("to-archive"),
        )

        new_key = repository.move_message_to_folder(
            folder=inbox,
            storage_key=storage_key,
            target_folder="Archive",
        )

        self.assertEqual(repository.list_messages(folder=inbox), ())
        archive = FolderRef(
            account_name=self.account_name,
            folder_name="Archive",
        )
        self.assertEqual(len(repository.list_messages(folder=archive)), 1)

        payload = repository.get_message_bytes(
            folder=archive,
            storage_key=new_key,
        )
        self.assertIn(b"Subject: to-archive", payload)

    def test_move_message_preserves_retrievability(self) -> None:
        """Retrieval with the returned key works; with the old key fails.

        Regression test: any caller that stashes the pre-move storage_key
        and tries to reuse it after a move must get an explicit error,
        not a silent no-op or a stale read.  Maildir and mbox both must
        honour this — even though Maildir happens to preserve the key
        across moves, the contract is "use the returned key".
        """
        repository = self.make_repository()
        inbox = FolderRef(account_name=self.account_name, folder_name="INBOX")
        storage_key = repository.store_message(
            folder=inbox,
            raw_message=sample_message_bytes("moving"),
        )
        archive = FolderRef(
            account_name=self.account_name,
            folder_name="Archive",
        )
        new_key = repository.move_message_to_folder(
            folder=inbox,
            storage_key=storage_key,
            target_folder="Archive",
        )

        payload = repository.get_message_bytes(
            folder=archive,
            storage_key=new_key,
        )
        self.assertIn(b"Subject: moving", payload)

        # Looking in the old folder for the old key must fail.
        with self.assertRaises(KeyError):
            repository.get_message_bytes(
                folder=inbox,
                storage_key=storage_key,
            )

    def test_retrieval_uses_storage_key_not_rfc5322_id(self) -> None:
        """The returned storage_key must be distinct from the RFC 5322 id.

        Regression test for the mirror identity bug: any caller that
        confuses ``MessageRef.message_id`` (= RFC 5322 id for IMAP-synced
        mail) with the backend's own storage_key will silently fail.
        This test pins that the backend does NOT use the RFC 5322 header
        as its internal key, so callers cannot accidentally get it right.
        """
        repository = self.make_repository()
        folder = FolderRef(account_name=self.account_name, folder_name="INBOX")
        rfc5322_id = "<distinct-rfc5322-id@example.com>"
        raw = _rfc5322_message_bytes("probe", rfc5322_id)
        storage_key = repository.store_message(folder=folder, raw_message=raw)
        self.assertNotEqual(
            storage_key,
            rfc5322_id,
            "storage_key must not be the RFC 5322 Message-ID header",
        )
        payload = repository.get_message_bytes(
            folder=folder,
            storage_key=storage_key,
        )
        self.assertIn(rfc5322_id.encode(), payload)

    def test_move_message_to_same_folder_is_noop(self) -> None:
        repository = self.make_repository()
        inbox = FolderRef(account_name=self.account_name, folder_name="INBOX")
        storage_key = repository.store_message(
            folder=inbox,
            raw_message=sample_message_bytes("stay"),
        )
        result = repository.move_message_to_folder(
            folder=inbox,
            storage_key=storage_key,
            target_folder="INBOX",
        )
        self.assertEqual(result, storage_key)
        self.assertEqual(len(repository.list_messages(folder=inbox)), 1)

    def test_create_folder_makes_empty_folder_visible(self) -> None:
        repository = self.make_repository()
        repository.create_folder(
            account_name=self.account_name,
            folder_name="Projects",
        )
        names = [
            f.folder_name
            for f in repository.list_folders(
                account_name=self.account_name,
            )
        ]
        self.assertIn("Projects", names)

    def test_create_folder_is_idempotent(self) -> None:
        repository = self.make_repository()
        repository.create_folder(
            account_name=self.account_name,
            folder_name="Archive",
        )
        repository.create_folder(
            account_name=self.account_name,
            folder_name="Archive",
        )
        names = [
            f.folder_name
            for f in repository.list_folders(
                account_name=self.account_name,
            )
        ]
        self.assertEqual(names.count("Archive"), 1)


class MaildirMirrorRepositoryTestCase(MirrorRepositoryConformanceMixin):
    """Run conformance tests against Maildir backend."""

    def make_repository(self) -> MirrorRepository:
        root = TMP_ROOT / "storage" / "maildir" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        return MaildirMirrorRepository(account_name=self.account_name, root_dir=root)


class MboxMirrorRepositoryTestCase(MirrorRepositoryConformanceMixin):
    """Run conformance tests against mbox backend."""

    def make_repository(self) -> MirrorRepository:
        root = TMP_ROOT / "storage" / "mbox" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        return MboxMirrorRepository(account_name=self.account_name, root_dir=root)


def sample_message_bytes(subject: str) -> bytes:
    """Create deterministic RFC 5322 fixture bytes."""
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "user@example.com"
    message["Subject"] = subject
    message["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
    message.set_content("sample body")
    return message.as_bytes()
