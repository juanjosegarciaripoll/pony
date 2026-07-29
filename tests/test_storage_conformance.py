"""Conformance tests for mirror storage backends."""

from __future__ import annotations

import mailbox
import unittest
from email.message import EmailMessage
from uuid import uuid4

from conftest import TMP_ROOT

from pony.domain import FolderRef, MessageFlag
from pony.protocols import MirrorRepository
from pony.storage import (
    MaildirMirrorRepository,
    MboxMirrorRepository,
    _build_mbox_toc,
)


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

    def test_interleaved_mutations_all_survive_one_flush(self) -> None:
        """Stores, flag writes and deletes may be freely interleaved.

        All three defer their commit so that a sync does not pay a whole
        rewrite per message, which means they queue up together — this
        pins that the deferred state is coherent when it is finally
        written, and that everything is readable before it is.
        """
        repository = self.make_repository()
        folder = FolderRef(account_name=self.account_name, folder_name="INBOX")

        alpha = repository.store_message(
            folder=folder, raw_message=_rfc5322_message_bytes("alpha", "<a@x>")
        )
        bravo = repository.store_message(
            folder=folder, raw_message=_rfc5322_message_bytes("bravo", "<b@x>")
        )
        repository.set_flags(
            folder=folder, storage_key=alpha, flags=frozenset({MessageFlag.SEEN})
        )
        charlie = repository.store_message(
            folder=folder, raw_message=_rfc5322_message_bytes("charlie", "<c@x>")
        )
        repository.delete_message(folder=folder, storage_key=bravo)
        delta = repository.store_message(
            folder=folder, raw_message=_rfc5322_message_bytes("delta", "<d@x>")
        )

        # Everything still readable before anything is committed.
        for key, subject in ((alpha, "alpha"), (charlie, "charlie"), (delta, "delta")):
            body = repository.get_message_bytes(folder=folder, storage_key=key)
            self.assertIn(subject.encode(), body)

        repository.flush_writes()

        keys = set(repository.list_messages(folder=folder))
        self.assertEqual({alpha, charlie, delta}, keys & {alpha, charlie, delta})
        self.assertNotIn(bravo, keys)
        self.assertIn(
            b"alpha",
            repository.get_message_bytes(folder=folder, storage_key=alpha),
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


class BuildMboxTocTestCase(unittest.TestCase):
    """``_build_mbox_toc`` must match ``mailbox.mbox._generate_toc`` exactly.

    The TUI's first-message preview path assigns the result directly to
    ``mbox._toc`` (and friends), so any divergence in start/stop offsets
    would corrupt every read.
    """

    def _build_fixture(
        self, subjects: list[str]
    ) -> tuple[dict[int, tuple[int, int]], int, int]:
        root = TMP_ROOT / "storage" / "mbox-toc" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        path = root / "fixture.mbox"
        mb = mailbox.mbox(str(path), create=True)
        for subject in subjects:
            mb.add(mailbox.mboxMessage(sample_message_bytes(subject)))
        mb.flush()
        mb.close()

        # Stdlib reference: open fresh, force toc.
        ref = mailbox.mbox(str(path), create=False)
        ref._generate_toc()  # type: ignore[attr-defined]
        ref_toc = dict(ref._toc)  # type: ignore[attr-defined]
        ref_next: int = ref._next_key  # type: ignore[attr-defined]
        ref_filelen: int = ref._file_length  # type: ignore[attr-defined]
        ref.close()

        toc, next_key, file_length = _build_mbox_toc(path)
        self.assertEqual(toc, ref_toc)
        self.assertEqual(next_key, ref_next)
        self.assertEqual(file_length, ref_filelen)
        return toc, next_key, file_length

    def test_empty_mbox(self) -> None:
        toc, next_key, file_length = self._build_fixture([])
        self.assertEqual(toc, {})
        self.assertEqual(next_key, 0)
        self.assertEqual(file_length, 0)

    def test_single_message(self) -> None:
        toc, next_key, _ = self._build_fixture(["only"])
        self.assertEqual(set(toc), {0})
        self.assertEqual(next_key, 1)

    def test_multiple_messages(self) -> None:
        toc, next_key, _ = self._build_fixture(
            ["first", "second", "third", "fourth", "fifth"]
        )
        self.assertEqual(set(toc), {0, 1, 2, 3, 4})
        self.assertEqual(next_key, 5)
        # Offsets must be strictly monotonic and disjoint.
        prev_stop = -1
        for k in sorted(toc):
            start, stop = toc[k]
            self.assertGreaterEqual(start, prev_stop)
            self.assertLess(start, stop)
            prev_stop = stop


class MboxMessageByIdTest(unittest.TestCase):
    """Tests for the mbox message_id fast-path and _close_all."""

    def test_get_message_bytes_with_message_id_fast_path(self) -> None:
        """Providing message_id triggers the mmap fast path when mbox is not open."""
        from uuid import uuid4

        from pony.domain import FolderRef
        from pony.storage import MboxMirrorRepository

        root = TMP_ROOT / "mbox-mid" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        mirror = MboxMirrorRepository(account_name="acct", root_dir=root)
        folder = FolderRef(account_name="acct", folder_name="INBOX")

        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "Fast path test"
        msg["Date"] = "Mon, 1 Jan 2024 12:00:00 +0000"
        msg["Message-ID"] = "<fast-path-test@example.com>"
        msg.set_content("body content")
        raw = msg.as_bytes()

        key = mirror.store_message(folder=folder, raw_message=raw)
        mirror.flush_writes()

        # Force a fresh mirror instance to ensure the mbox is not cached
        mirror2 = MboxMirrorRepository(account_name="acct", root_dir=root)
        result = mirror2.get_message_bytes(
            folder=folder,
            storage_key=key,
            message_id="<fast-path-test@example.com>",
        )
        self.assertIn(b"Fast path test", result)

    def test_get_message_bytes_with_unknown_message_id_falls_back(self) -> None:
        """When message_id is not found via mmap, fallback returns correct bytes."""
        from uuid import uuid4

        from pony.domain import FolderRef
        from pony.storage import MboxMirrorRepository

        root = TMP_ROOT / "mbox-mid-fb" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        mirror = MboxMirrorRepository(account_name="acct", root_dir=root)
        folder = FolderRef(account_name="acct", folder_name="INBOX")

        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "Fallback test"
        msg["Date"] = "Mon, 1 Jan 2024 12:00:00 +0000"
        msg["Message-ID"] = "<fallback-test@example.com>"
        msg.set_content("fallback body")
        raw = msg.as_bytes()

        key = mirror.store_message(folder=folder, raw_message=raw)
        mirror.flush_writes()

        mirror2 = MboxMirrorRepository(account_name="acct", root_dir=root)
        # Pass a non-matching message_id - should fall back to key lookup
        result = mirror2.get_message_bytes(
            folder=folder,
            storage_key=key,
            message_id="<no-such-id@example.com>",
        )
        self.assertIn(b"Fallback test", result)

    def test_mbox_close_all_on_del(self) -> None:
        """_close_all runs without error during garbage collection."""
        from uuid import uuid4

        from pony.domain import FolderRef
        from pony.storage import MboxMirrorRepository

        root = TMP_ROOT / "mbox-gc" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        mirror = MboxMirrorRepository(account_name="acct", root_dir=root)
        folder = FolderRef(account_name="acct", folder_name="INBOX")

        msg = EmailMessage()
        msg["From"] = "x@x.com"
        msg["To"] = "y@y.com"
        msg["Date"] = "Mon, 1 Jan 2024 12:00:00 +0000"
        msg["Message-ID"] = "<gc-test@example.com>"
        msg.set_content("gc body")
        mirror.store_message(folder=folder, raw_message=msg.as_bytes())

        # Open the mbox by reading
        mirror.get_message_bytes(
            folder=folder,
            storage_key="0",
        )
        # Calling _close_all explicitly should not raise
        mirror._close_all()
        # And calling it again (empty handles) should also be fine
        mirror._close_all()

    def test_repository_is_not_retained_until_process_exit(self) -> None:
        """The cleanup registration must not keep the repository alive."""
        import gc
        import weakref
        from uuid import uuid4

        from pony.storage import MboxMirrorRepository

        root = TMP_ROOT / "mbox-finalizer" / uuid4().hex
        mirror = MboxMirrorRepository(account_name="acct", root_dir=root)
        repository_ref = weakref.ref(mirror)

        del mirror
        gc.collect()

        self.assertIsNone(repository_ref())


class MboxDirectLookupTestCase(unittest.TestCase):
    """Edge cases of the mmap-based single-message lookup."""

    def _mbox_path(self, body: bytes) -> object:
        from pathlib import Path

        root = TMP_ROOT / "mbox-lookup" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        path: Path = root / "folder.mbox"
        path.write_bytes(body)
        return path

    def test_missing_file_returns_none(self) -> None:
        from pathlib import Path

        from pony.storage import _mbox_find_message_by_id

        missing = Path(TMP_ROOT / "mbox-lookup" / uuid4().hex / "absent.mbox")
        self.assertIsNone(_mbox_find_message_by_id(missing, "<a@example.com>"))

    def test_empty_file_returns_none(self) -> None:
        from pony.storage import _mbox_find_message_by_id

        path = self._mbox_path(b"")
        self.assertIsNone(_mbox_find_message_by_id(path, "<a@example.com>"))  # type: ignore[arg-type]

    def test_absent_message_id_returns_none(self) -> None:
        from pony.storage import _mbox_find_message_by_id

        path = self._mbox_path(
            b"From sender@example.com\nMessage-ID: <other@example.com>\n\nbody\n\n"
        )
        self.assertIsNone(_mbox_find_message_by_id(path, "<a@example.com>"))  # type: ignore[arg-type]

    def test_last_message_without_trailing_blank_line(self) -> None:
        from pony.storage import _mbox_find_message_by_id

        path = self._mbox_path(
            b"From sender@example.com\nMessage-ID: <last@example.com>\n\nbody\n"
        )
        found = _mbox_find_message_by_id(path, "<last@example.com>")  # type: ignore[arg-type]
        assert found is not None
        self.assertIn(b"<last@example.com>", found)
        self.assertNotIn(b"From sender@example.com", found)

    def test_message_followed_by_another_is_bounded(self) -> None:
        from pony.storage import _mbox_find_message_by_id

        path = self._mbox_path(
            b"From a@example.com\nMessage-ID: <first@example.com>\n\nfirst body\n\n"
            b"From b@example.com\nMessage-ID: <second@example.com>\n\nsecond body\n\n"
        )
        found = _mbox_find_message_by_id(path, "<first@example.com>")  # type: ignore[arg-type]
        assert found is not None
        self.assertIn(b"first body", found)
        self.assertNotIn(b"second body", found)

    def test_envelope_line_without_newline_returns_none(self) -> None:
        from pony.storage import _mbox_find_message_by_id

        # A hit with no newline terminating the "From " envelope line.
        path = self._mbox_path(b"From x\rMessage-ID: <trunc@example.com>")
        self.assertIsNone(_mbox_find_message_by_id(path, "<trunc@example.com>"))  # type: ignore[arg-type]


class MboxMiscTestCase(unittest.TestCase):
    """Small mbox repository paths not exercised by the conformance suite."""

    def _repo(self) -> MboxMirrorRepository:
        root = TMP_ROOT / "mbox-misc" / uuid4().hex
        return MboxMirrorRepository(account_name="acct", root_dir=root)

    def test_unknown_account_is_rejected(self) -> None:
        repo = self._repo()
        with self.assertRaises(ValueError):
            repo.list_messages(
                folder=FolderRef(account_name="other", folder_name="INBOX")
            )

    def test_folder_mtime_of_absent_folder_is_zero(self) -> None:
        repo = self._repo()
        mtime = repo.folder_mtime_ns(
            folder=FolderRef(account_name="acct", folder_name="NeverCreated")
        )
        self.assertEqual(mtime, 0)

    def test_folder_mtime_tracks_writes(self) -> None:
        repo = self._repo()
        folder = FolderRef(account_name="acct", folder_name="INBOX")
        repo.create_folder(account_name="acct", folder_name="INBOX")
        repo.store_message(
            folder=folder,
            raw_message=_rfc5322_message_bytes("Subject", "<m@example.com>"),
        )
        self.assertGreater(repo.folder_mtime_ns(folder=folder), 0)


class MaildirShutdownTestCase(unittest.TestCase):
    """The async write pool must release cleanly and be idempotent."""

    def test_shutdown_is_idempotent(self) -> None:
        root = TMP_ROOT / "maildir-shutdown" / uuid4().hex
        repo = MaildirMirrorRepository(account_name="acct", root_dir=root)
        folder = FolderRef(account_name="acct", folder_name="INBOX")
        repo.create_folder(account_name="acct", folder_name="INBOX")
        # The async path is what allocates the write pool that _shutdown frees.
        repo.store_message_async(
            folder=folder,
            raw_message=_rfc5322_message_bytes("Subject", "<s@example.com>"),
        )

        repo._shutdown()
        repo._shutdown()

    def test_shutdown_survives_a_failing_flush(self) -> None:
        root = TMP_ROOT / "maildir-shutdown-fail" / uuid4().hex
        repo = MaildirMirrorRepository(account_name="acct", root_dir=root)

        def _boom() -> None:
            raise OSError("flush failed")

        repo.flush_writes = _boom  # type: ignore[method-assign]
        repo._shutdown()


class MaildirMissingKeyTestCase(unittest.TestCase):
    """Operations against a storage_key with no file behind it.

    The index is authoritative, so a row can outlive its mirror file —
    after an external tool prunes the maildir, or a partially-restored
    backup.  Every lookup must raise ``KeyError`` naming the key rather
    than fail obscurely deeper down.
    """

    def _repo(self) -> MaildirMirrorRepository:
        root = TMP_ROOT / "maildir-missing" / uuid4().hex
        repo = MaildirMirrorRepository(account_name="acct", root_dir=root)
        repo.create_folder(account_name="acct", folder_name="INBOX")
        return repo

    def test_reading_an_absent_key_raises(self) -> None:
        repo = self._repo()
        folder = FolderRef(account_name="acct", folder_name="INBOX")

        with self.assertRaises(KeyError):
            repo.get_message_bytes(folder=folder, storage_key="not-there")

    def test_setting_flags_on_an_absent_key_raises(self) -> None:
        repo = self._repo()
        folder = FolderRef(account_name="acct", folder_name="INBOX")

        with self.assertRaises(KeyError):
            repo.set_flags(
                folder=folder,
                storage_key="not-there",
                flags=frozenset({MessageFlag.SEEN}),
            )

    def test_moving_an_absent_key_raises(self) -> None:
        repo = self._repo()
        folder = FolderRef(account_name="acct", folder_name="INBOX")
        repo.create_folder(account_name="acct", folder_name="Archive")

        with self.assertRaises(KeyError):
            repo.move_message_to_folder(
                folder=folder,
                storage_key="not-there",
                target_folder="Archive",
            )

    def test_an_unknown_account_is_rejected(self) -> None:
        repo = self._repo()

        with self.assertRaises(ValueError) as ctx:
            repo.list_folders(account_name="someone-else")

        self.assertIn("unknown account", str(ctx.exception))


class MaildirFolderListingTestCase(unittest.TestCase):
    """``list_folders`` reads the maildir layout off disk."""

    def test_stray_files_and_dotfiles_are_not_folders(self) -> None:
        """Only ``.name`` *directories* count; a bare ``.`` name does not."""
        root = TMP_ROOT / "maildir-listing" / uuid4().hex
        repo = MaildirMirrorRepository(account_name="acct", root_dir=root)
        repo.create_folder(account_name="acct", folder_name="Archive")

        # A dotfile (not a directory) and a directory named just "." —
        # both must be ignored rather than become folders.
        (root / ".uidvalidity").write_text("1", encoding="utf-8")

        names = {ref.folder_name for ref in repo.list_folders(account_name="acct")}

        self.assertIn("INBOX", names)
        self.assertIn("Archive", names)
        self.assertNotIn("uidvalidity", names)
        self.assertNotIn("", names)


class MboxEmptyMailboxTestCase(unittest.TestCase):
    """An mbox file with no messages must list as empty, not crash."""

    def test_listing_an_empty_mbox_returns_nothing(self) -> None:
        root = TMP_ROOT / "mbox-empty" / uuid4().hex
        repo = MboxMirrorRepository(account_name="acct", root_dir=root)
        repo.create_folder(account_name="acct", folder_name="INBOX")
        folder = FolderRef(account_name="acct", folder_name="INBOX")

        self.assertEqual(repo.list_messages(folder=folder), ())

    def test_inbox_is_listed_even_when_only_other_mboxes_exist(self) -> None:
        root = TMP_ROOT / "mbox-folders" / uuid4().hex
        repo = MboxMirrorRepository(account_name="acct", root_dir=root)
        repo.create_folder(account_name="acct", folder_name="Archive")

        names = {ref.folder_name for ref in repo.list_folders(account_name="acct")}

        self.assertIn("INBOX", names)
        self.assertIn("Archive", names)

    def test_reading_an_absent_key_raises(self) -> None:
        root = TMP_ROOT / "mbox-missing" / uuid4().hex
        repo = MboxMirrorRepository(account_name="acct", root_dir=root)
        repo.create_folder(account_name="acct", folder_name="INBOX")
        folder = FolderRef(account_name="acct", folder_name="INBOX")
        repo.store_message(
            folder=folder,
            raw_message=_rfc5322_message_bytes("Present", "<present@example.com>"),
        )

        with self.assertRaises(KeyError):
            repo.get_message_bytes(folder=folder, storage_key="9999")


class MirrorProtocolCompletenessTest(unittest.TestCase):
    """The deferred-write pair belongs to the interface.

    The sync engine used to look for ``store_message_async`` and
    ``flush_writes`` with ``getattr`` because the protocol did not
    declare them.  Any backend that did not happen to have them was
    silently downgraded to one synchronous write per message, with no
    error and no way to notice.  Declaring them means the engine calls
    them outright and a backend missing them fails type-checking.
    """

    def test_both_methods_are_declared_on_the_protocol(self) -> None:
        for name in ("store_message_async", "flush_writes"):
            with self.subTest(method=name):
                self.assertTrue(hasattr(MirrorRepository, name))

    def test_every_shipped_backend_implements_them(self) -> None:
        for backend in (MaildirMirrorRepository, MboxMirrorRepository):
            for name in ("store_message_async", "flush_writes"):
                with self.subTest(backend=backend.__name__, method=name):
                    self.assertIsNot(
                        getattr(backend, name),
                        getattr(MirrorRepository, name),
                        f"{backend.__name__} must define {name}, not inherit the stub",
                    )
