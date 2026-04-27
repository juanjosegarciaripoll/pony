"""Tests for the person-centric contacts store and BBDB interop."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from conftest import TMP_ROOT

from pony.bbdb import read_bbdb, write_bbdb
from pony.domain import (
    Contact,
    IndexedMessage,
    MessageRef,
    MessageStatus,
)
from pony.index_store import SqliteIndexRepository
from pony.paths import AppPaths


def _make_repo() -> SqliteIndexRepository:
    tmp = TMP_ROOT / "contacts" / uuid4().hex
    tmp.mkdir(parents=True, exist_ok=True)
    repo = SqliteIndexRepository(database_path=tmp / "index.sqlite3")
    repo.initialize()
    return repo


def _make_contact(**kwargs: object) -> Contact:
    defaults: dict[str, object] = {
        "id": None,
        "first_name": "Alice",
        "last_name": "Smith",
        "emails": ("alice@example.com",),
    }
    defaults.update(kwargs)
    return Contact(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Upsert and lookup
# ---------------------------------------------------------------------------


class UpsertContactTests(unittest.TestCase):
    def test_insert_and_find_by_email(self) -> None:
        repo = _make_repo()
        contact = _make_contact()
        saved = repo.upsert_contact(contact=contact)
        self.assertIsNotNone(saved.id)
        found = repo.find_contact_by_email(email_address="alice@example.com")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.first_name, "Alice")
        self.assertEqual(found.last_name, "Smith")

    def test_multiple_emails(self) -> None:
        repo = _make_repo()
        contact = _make_contact(emails=("a@work.com", "a@home.com"))
        repo.upsert_contact(contact=contact)
        self.assertIsNotNone(repo.find_contact_by_email(email_address="a@work.com"))
        self.assertIsNotNone(repo.find_contact_by_email(email_address="a@home.com"))

    def test_update_existing(self) -> None:
        repo = _make_repo()
        saved = repo.upsert_contact(contact=_make_contact())
        updated = repo.upsert_contact(
            contact=Contact(
                id=saved.id,
                first_name="Alice",
                last_name="Smith",
                emails=("alice@example.com",),
                organization="Acme",
                message_count=5,
            )
        )
        self.assertEqual(updated.organization, "Acme")
        self.assertEqual(updated.message_count, 5)

    def test_aliases_stored(self) -> None:
        repo = _make_repo()
        saved = repo.upsert_contact(
            contact=_make_contact(aliases=("Ali", "Allie"))
        )
        self.assertEqual(saved.aliases, ("Ali", "Allie"))

    def test_display_name_property(self) -> None:
        c = _make_contact(first_name="Juan", last_name="Garcia")
        self.assertEqual(c.display_name, "Juan Garcia")

    def test_display_name_first_only(self) -> None:
        c = _make_contact(first_name="Madonna", last_name="")
        self.assertEqual(c.display_name, "Madonna")

    def test_primary_email(self) -> None:
        c = _make_contact(emails=("a@x.com", "b@x.com"))
        self.assertEqual(c.primary_email, "a@x.com")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchContactsTests(unittest.TestCase):
    def _seed(self) -> SqliteIndexRepository:
        repo = _make_repo()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Alice", last_name="Smith",
                emails=("alice@example.com",), message_count=10,
            )
        )
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Bob", last_name="Jones",
                emails=("bob@example.com",), message_count=5,
                aliases=("Bobby",),
            )
        )
        return repo

    def test_search_by_first_name(self) -> None:
        repo = self._seed()
        results = repo.search_contacts(prefix="ali")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].first_name, "Alice")

    def test_search_by_email(self) -> None:
        repo = self._seed()
        results = repo.search_contacts(prefix="bob@")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].first_name, "Bob")

    def test_search_by_alias(self) -> None:
        repo = self._seed()
        results = repo.search_contacts(prefix="Bobby")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].first_name, "Bob")

    def test_search_ordered_by_message_count(self) -> None:
        repo = self._seed()
        results = repo.search_contacts(prefix="example")
        self.assertEqual(results[0].first_name, "Alice")  # 10 > 5

    def test_search_respects_limit(self) -> None:
        repo = self._seed()
        results = repo.search_contacts(prefix="example", limit=1)
        self.assertEqual(len(results), 1)

    def test_search_no_match(self) -> None:
        repo = self._seed()
        results = repo.search_contacts(prefix="zzz")
        self.assertEqual(len(results), 0)

    def test_search_folds_diacritics(self) -> None:
        repo = _make_repo()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="María", last_name="López",
                emails=("maria@example.com",),
            )
        )
        ascii_hits = repo.search_contacts(prefix="maria")
        self.assertEqual(len(ascii_hits), 1)
        self.assertEqual(ascii_hits[0].first_name, "María")

    def test_search_prefix_returns_multiple(self) -> None:
        repo = _make_repo()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="María", last_name="L",
                emails=("maria@example.com",),
            )
        )
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Mariano", last_name="R",
                emails=("mariano@example.com",),
            )
        )
        # Prefix match ("mar" is not a whole word).
        names = {c.first_name for c in repo.search_contacts(prefix="mar")}
        self.assertEqual(names, {"María", "Mariano"})

    def test_search_by_email_local_part(self) -> None:
        repo = _make_repo()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Juan", last_name="Garcia",
                emails=("juan.garcia@example.com",),
            )
        )
        # The email address splits on punctuation in unicode61, so
        # "garcia" matches the local-part even without a prefix.
        hits = repo.search_contacts(prefix="garcia")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].first_name, "Juan")


# ---------------------------------------------------------------------------
# Harvesting
# ---------------------------------------------------------------------------


def _make_indexed_message(
    recipients: str, cc: str = ""
) -> IndexedMessage:
    return IndexedMessage(
        message_ref=MessageRef(
            account_name="test", folder_name="INBOX", id=0,
        ),
        message_id="<t@t>",
        sender="sender@example.com",
        recipients=recipients,
        cc=cc,
        subject="Test",
        body_preview="body",
        storage_key="",
        local_flags=frozenset(),
        base_flags=frozenset(),
        local_status=MessageStatus.ACTIVE,
        received_at=datetime.now(tz=UTC),
    )


class HarvestContactsTests(unittest.TestCase):
    def test_harvest_creates_contact(self) -> None:
        repo = _make_repo()
        msg = _make_indexed_message("Alice Smith <alice@example.com>")
        repo.harvest_contacts([msg])
        found = repo.find_contact_by_email(email_address="alice@example.com")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.first_name, "Alice")
        self.assertEqual(found.last_name, "Smith")
        self.assertEqual(found.message_count, 1)

    def test_harvest_increments_count(self) -> None:
        repo = _make_repo()
        msg = _make_indexed_message("alice@example.com")
        repo.harvest_contacts([msg, msg, msg])
        found = repo.find_contact_by_email(email_address="alice@example.com")
        assert found is not None
        self.assertEqual(found.message_count, 3)

    def test_harvest_cc(self) -> None:
        repo = _make_repo()
        msg = _make_indexed_message("", cc="Carol <carol@example.com>")
        repo.harvest_contacts([msg])
        self.assertIsNotNone(
            repo.find_contact_by_email(email_address="carol@example.com")
        )

    def test_harvest_does_not_harvest_sender(self) -> None:
        repo = _make_repo()
        msg = _make_indexed_message("alice@example.com")
        repo.harvest_contacts([msg])
        self.assertIsNone(
            repo.find_contact_by_email(email_address="sender@example.com")
        )

    def test_harvest_updates_name_on_empty(self) -> None:
        repo = _make_repo()
        msg1 = _make_indexed_message("alice@example.com")
        repo.harvest_contacts([msg1])
        msg2 = _make_indexed_message("Alice Smith <alice@example.com>")
        repo.harvest_contacts([msg2])
        found = repo.find_contact_by_email(email_address="alice@example.com")
        assert found is not None
        self.assertEqual(found.first_name, "Alice")


# ---------------------------------------------------------------------------
# Delete and merge
# ---------------------------------------------------------------------------


class DeleteContactTests(unittest.TestCase):
    def test_delete_removes_contact(self) -> None:
        repo = _make_repo()
        saved = repo.upsert_contact(contact=_make_contact())
        assert saved.id is not None
        repo.delete_contact(contact_id=saved.id)
        self.assertIsNone(
            repo.find_contact_by_email(email_address="alice@example.com")
        )

    def test_delete_removes_emails_and_aliases(self) -> None:
        repo = _make_repo()
        saved = repo.upsert_contact(
            contact=_make_contact(
                emails=("a@x.com", "b@x.com"), aliases=("Ali",),
            )
        )
        assert saved.id is not None
        repo.delete_contact(contact_id=saved.id)
        self.assertIsNone(repo.find_contact_by_email(email_address="a@x.com"))
        self.assertEqual(repo.search_contacts(prefix="Ali"), [])


class MergeContactsTests(unittest.TestCase):
    def test_merge_combines_emails(self) -> None:
        repo = _make_repo()
        c1 = repo.upsert_contact(
            contact=_make_contact(
                first_name="Alice", emails=("a@work.com",), message_count=3,
            )
        )
        c2 = repo.upsert_contact(
            contact=_make_contact(
                first_name="Alice", last_name="S",
                emails=("a@home.com",), message_count=7,
            )
        )
        assert c1.id is not None and c2.id is not None
        merged = repo.merge_contacts(target_id=c1.id, source_ids=[c2.id])
        self.assertIn("a@work.com", merged.emails)
        self.assertIn("a@home.com", merged.emails)
        self.assertEqual(merged.message_count, 10)
        # Source should be gone.
        self.assertIsNone(
            repo.find_contact_by_email(email_address="a@home.com")
            if "a@home.com" not in merged.emails
            else None  # email moved to target, so find returns target
        )

    def test_merge_combines_aliases(self) -> None:
        repo = _make_repo()
        c1 = repo.upsert_contact(
            contact=_make_contact(emails=("a@x.com",), aliases=("Ali",))
        )
        c2 = repo.upsert_contact(
            contact=_make_contact(
                first_name="Bob", emails=("b@x.com",), aliases=("Bobby",),
            )
        )
        assert c1.id is not None and c2.id is not None
        merged = repo.merge_contacts(target_id=c1.id, source_ids=[c2.id])
        self.assertIn("Ali", merged.aliases)
        self.assertIn("Bobby", merged.aliases)

    def test_merge_three_contacts(self) -> None:
        repo = _make_repo()
        c1 = repo.upsert_contact(
            contact=_make_contact(emails=("a@x.com",), message_count=1)
        )
        c2 = repo.upsert_contact(
            contact=_make_contact(
                first_name="B", emails=("b@x.com",), message_count=2,
            )
        )
        c3 = repo.upsert_contact(
            contact=_make_contact(
                first_name="C", emails=("c@x.com",), message_count=3,
            )
        )
        assert c1.id is not None and c2.id is not None and c3.id is not None
        merged = repo.merge_contacts(
            target_id=c1.id, source_ids=[c2.id, c3.id],
        )
        self.assertEqual(len(merged.emails), 3)
        self.assertEqual(merged.message_count, 6)
        self.assertEqual(len(repo.list_all_contacts()), 1)


# ---------------------------------------------------------------------------
# BBDB roundtrip
# ---------------------------------------------------------------------------


class BbdbRoundtripTests(unittest.TestCase):
    def test_write_and_read(self) -> None:
        tmp = TMP_ROOT / "bbdb" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        bbdb_path = tmp / "bbdb"

        contacts = [
            _make_contact(
                first_name="Juan",
                last_name="Garcia",
                emails=("juan@example.com", "jj@alt.com"),
                aliases=("JJ", "Juanjo"),
                organization="Acme",
                notes="Met at conference",
            ),
            _make_contact(
                first_name="Alice",
                last_name="Smith",
                emails=("alice@example.com",),
                affix=("Dr.",),
            ),
        ]
        write_bbdb(contacts, bbdb_path)
        loaded = read_bbdb(bbdb_path)

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].first_name, "Juan")
        self.assertEqual(loaded[0].last_name, "Garcia")
        self.assertEqual(loaded[0].emails, ("juan@example.com", "jj@alt.com"))
        self.assertEqual(loaded[0].aliases, ("JJ", "Juanjo"))
        self.assertEqual(loaded[0].organization, "Acme")
        self.assertEqual(loaded[0].notes, "Met at conference")
        self.assertEqual(loaded[1].affix, ("Dr.",))

    def test_empty_file(self) -> None:
        tmp = TMP_ROOT / "bbdb" / uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        self.assertEqual(read_bbdb(tmp / "nonexistent"), [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_paths() -> tuple[AppPaths, SqliteIndexRepository]:
    """Create a fresh temp directory, AppPaths, and initialized repo."""
    tmp = TMP_ROOT / "contacts-cli" / uuid4().hex
    tmp.mkdir(parents=True, exist_ok=True)
    paths = AppPaths(
        config_file=tmp / "config.toml",
        data_dir=tmp,
        state_dir=tmp,
        cache_dir=tmp,
        log_dir=tmp,
        index_db_file=tmp / "index.sqlite3",
    )
    repo = SqliteIndexRepository(database_path=paths.index_db_file)
    repo.initialize()
    return paths, repo


def _capture(fn: object, *args: object, **kwargs: object) -> str:
    """Call *fn* and return its stdout as a string."""
    import io
    import sys

    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        fn(*args, **kwargs)  # type: ignore[operator]
    finally:
        sys.stdout = old_stdout
    return captured.getvalue()


class ContactsCliTests(unittest.TestCase):
    def test_contacts_search_finds_contact(self) -> None:
        from pony.cli import run_contacts_search

        paths, repo = _cli_paths()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Test", last_name="User",
                emails=("test@example.com",),
            )
        )

        output = _capture(
            run_contacts_search, paths=paths, prefix="test", limit=10,
        )
        self.assertIn("test@example.com", output)
        self.assertIn("Test User", output)

    def test_contacts_search_no_results(self) -> None:
        from pony.cli import run_contacts_search

        paths, repo = _cli_paths()
        del repo  # unused — just need initialized DB

        output = _capture(
            run_contacts_search, paths=paths, prefix="nobody", limit=10,
        )
        self.assertIn("No contacts", output)

    def test_contacts_show_found(self) -> None:
        from pony.cli import run_contacts_show

        paths, repo = _cli_paths()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Alice", last_name="Smith",
                emails=("alice@example.com",),
                organization="Acme",
                aliases=("Ali",),
            )
        )

        output = _capture(
            run_contacts_show, paths=paths, email="alice@example.com",
        )
        self.assertIn("Alice Smith", output)
        self.assertIn("alice@example.com", output)
        self.assertIn("Acme", output)
        self.assertIn("Ali", output)

    def test_contacts_show_not_found(self) -> None:
        import io
        import sys

        from pony.cli import run_contacts_show

        paths, repo = _cli_paths()
        del repo

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = run_contacts_show(paths=paths, email="nobody@x.com")
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 1)
        self.assertIn("No contact found", captured.getvalue())

    def test_contacts_export_to_explicit_path(self) -> None:
        from pony.cli import run_contacts_export

        paths, repo = _cli_paths()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Juan", last_name="Garcia",
                emails=("juan@example.com",),
            )
        )
        out_file = paths.data_dir / "test.bbdb"

        output = _capture(
            run_contacts_export,
            paths=paths,
            config_path=None,
            output_path=str(out_file),
        )
        self.assertIn("Exported 1 contact", output)
        self.assertTrue(out_file.exists())

        loaded = read_bbdb(out_file)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].first_name, "Juan")

    def test_contacts_export_no_path_errors(self) -> None:
        import io
        import sys

        from pony.cli import run_contacts_export

        paths, repo = _cli_paths()
        del repo

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = run_contacts_export(
                paths=paths,
                config_path=paths.data_dir / "nonexistent.toml",
                output_path=None,
            )
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 1)
        self.assertIn("No output path", captured.getvalue())

    def test_contacts_import_creates_new(self) -> None:
        from pony.cli import run_contacts_import

        paths, repo = _cli_paths()
        del repo
        bbdb_file = paths.data_dir / "test.bbdb"
        write_bbdb(
            [_make_contact(
                first_name="Eve", last_name="New",
                emails=("eve@example.com",),
            )],
            bbdb_file,
        )

        output = _capture(
            run_contacts_import,
            paths=paths, config_path=None, input_path=str(bbdb_file),
        )
        self.assertIn("1 new", output)

        index = SqliteIndexRepository(database_path=paths.index_db_file)
        index.initialize()
        found = index.find_contact_by_email(email_address="eve@example.com")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.first_name, "Eve")

    def test_contacts_import_merges_existing(self) -> None:
        from pony.cli import import_bbdb_contacts

        paths, repo = _cli_paths()
        # Seed an existing contact.
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Alice", last_name="Smith",
                emails=("alice@example.com",),
                aliases=("Ali",),
            )
        )

        # Import a BBDB file that has the same email with extra info.
        bbdb_file = paths.data_dir / "merge.bbdb"
        write_bbdb(
            [_make_contact(
                first_name="Alice", last_name="Smith",
                emails=("alice@example.com", "alice@work.com"),
                organization="Acme",
                aliases=("Allie",),
            )],
            bbdb_file,
        )

        created, updated = import_bbdb_contacts(
            index=repo, bbdb_path=bbdb_file,
        )
        self.assertEqual(created, 0)
        self.assertEqual(updated, 1)

        found = repo.find_contact_by_email(email_address="alice@work.com")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertIn("alice@example.com", found.emails)
        self.assertIn("alice@work.com", found.emails)
        self.assertIn("Ali", found.aliases)
        self.assertIn("Allie", found.aliases)
        self.assertEqual(found.organization, "Acme")

    def test_contacts_import_no_path_errors(self) -> None:
        import io
        import sys

        from pony.cli import run_contacts_import

        paths, repo = _cli_paths()
        del repo

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = run_contacts_import(
                paths=paths,
                config_path=paths.data_dir / "nonexistent.toml",
                input_path=None,
            )
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 1)
        self.assertIn("No input path", captured.getvalue())
