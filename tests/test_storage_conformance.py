"""Conformance tests for mirror storage backends."""

from __future__ import annotations

import unittest
from email.message import EmailMessage
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import FolderRef, MessageFlag
from pony.protocols import MirrorRepository
from pony.storage import MaildirMirrorRepository, MboxMirrorRepository


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

        stored = repository.store_message(
            folder=folder, raw_message=sample_message_bytes("hello")
        )
        listed = repository.list_messages(folder=folder)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].message_id, stored.message_id)

        payload = repository.get_message_bytes(message_ref=stored)
        self.assertIn(b"Subject: hello", payload)

        repository.delete_message(message_ref=stored)
        self.assertEqual(repository.list_messages(folder=folder), ())

    def test_set_flags_roundtrip(self) -> None:
        repository = self.make_repository()
        folder = FolderRef(account_name=self.account_name, folder_name="INBOX")
        stored = repository.store_message(
            folder=folder, raw_message=sample_message_bytes("flag-test")
        )

        repository.set_flags(
            message_ref=stored,
            flags=frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}),
        )
        payload = repository.get_message_bytes(message_ref=stored)
        self.assertIn(b"Subject: flag-test", payload)

    def test_move_message_to_folder_relocates_bytes(self) -> None:
        repository = self.make_repository()
        inbox = FolderRef(account_name=self.account_name, folder_name="INBOX")
        stored = repository.store_message(
            folder=inbox, raw_message=sample_message_bytes("to-archive"),
        )

        moved = repository.move_message_to_folder(
            message_ref=stored, target_folder="Archive",
        )

        self.assertEqual(moved.folder_name, "Archive")
        self.assertEqual(repository.list_messages(folder=inbox), ())
        archive = FolderRef(
            account_name=self.account_name, folder_name="Archive",
        )
        self.assertEqual(len(repository.list_messages(folder=archive)), 1)

        payload = repository.get_message_bytes(message_ref=moved)
        self.assertIn(b"Subject: to-archive", payload)

    def test_move_message_to_same_folder_is_noop(self) -> None:
        repository = self.make_repository()
        inbox = FolderRef(account_name=self.account_name, folder_name="INBOX")
        stored = repository.store_message(
            folder=inbox, raw_message=sample_message_bytes("stay"),
        )
        result = repository.move_message_to_folder(
            message_ref=stored, target_folder="INBOX",
        )
        self.assertEqual(result, stored)
        self.assertEqual(len(repository.list_messages(folder=inbox)), 1)


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
