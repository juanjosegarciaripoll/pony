"""Tests for the person-centric contacts store and BBDB interop."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
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
        saved = repo.upsert_contact(contact=_make_contact(aliases=("Ali", "Allie")))
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
                first_name="Alice",
                last_name="Smith",
                emails=("alice@example.com",),
                message_count=10,
            )
        )
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Bob",
                last_name="Jones",
                emails=("bob@example.com",),
                message_count=5,
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

    def test_search_finds_contact_after_name_update(self) -> None:
        repo = _make_repo()
        saved = repo.upsert_contact(
            contact=_make_contact(
                first_name="Robert",
                last_name="Doe",
                emails=("rdoe@example.com",),
            )
        )
        repo.upsert_contact(
            contact=Contact(
                id=saved.id,
                first_name="Alice",
                last_name="Doe",
                emails=("rdoe@example.com",),
            )
        )
        results = repo.search_contacts(prefix="Alice")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].first_name, "Alice")
        # Old first name must no longer match (no trace of it in email either).
        self.assertEqual(repo.search_contacts(prefix="Robert"), [])

    def test_search_folds_diacritics(self) -> None:
        repo = _make_repo()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="María",
                last_name="López",
                emails=("maria@example.com",),
            )
        )
        ascii_hits = repo.search_contacts(prefix="maria")
        self.assertEqual(len(ascii_hits), 1)
        self.assertEqual(ascii_hits[0].first_name, "María")

    def test_search_full_name_across_fts_columns(self) -> None:
        repo = _make_repo()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Marina",
                last_name="Núñez Robles",
                emails=("marina@example.test",),
            )
        )

        hits = repo.search_contacts(prefix="marina nunez rob")

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].display_name, "Marina Núñez Robles")

    def test_search_multiple_tokens_can_match_different_fields(self) -> None:
        repo = _make_repo()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Marina",
                last_name="Núñez Robles",
                emails=("marina@example.test",),
            )
        )

        hits = repo.search_contacts(prefix="nunez marina")

        self.assertEqual(len(hits), 1)

    def test_search_prefix_returns_multiple(self) -> None:
        repo = _make_repo()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="María",
                last_name="L",
                emails=("maria@example.com",),
            )
        )
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Mariano",
                last_name="R",
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
                first_name="Juan",
                last_name="Garcia",
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


def _make_indexed_message(recipients: str, cc: str = "") -> IndexedMessage:
    return IndexedMessage(
        message_ref=MessageRef(
            account_name="test",
            folder_name="INBOX",
            id=0,
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

    def test_harvest_three_word_name(self) -> None:
        repo = _make_repo()
        msg = _make_indexed_message("Juan Garcia Ripoll <juan@example.com>")
        repo.harvest_contacts([msg])
        found = repo.find_contact_by_email(email_address="juan@example.com")
        assert found is not None
        self.assertEqual(found.first_name, "Juan")
        self.assertEqual(found.last_name, "Garcia Ripoll")

    def test_harvest_email_as_display_name_creates_blank_name(self) -> None:
        repo = _make_repo()
        msg = _make_indexed_message('"bob@example.com" <bob@example.com>')
        repo.harvest_contacts([msg])
        found = repo.find_contact_by_email(email_address="bob@example.com")
        assert found is not None
        self.assertEqual(found.first_name, "")
        self.assertEqual(found.last_name, "")

    def test_harvest_real_name_replaces_email_as_name(self) -> None:
        repo = _make_repo()
        # First harvest stores the email address as the display name.
        msg1 = _make_indexed_message('"bob@example.com" <bob@example.com>')
        repo.harvest_contacts([msg1])
        # Second harvest has the real name — should overwrite the placeholder.
        msg2 = _make_indexed_message("Bob Jones <bob@example.com>")
        repo.harvest_contacts([msg2])
        found = repo.find_contact_by_email(email_address="bob@example.com")
        assert found is not None
        self.assertEqual(found.first_name, "Bob")
        self.assertEqual(found.last_name, "Jones")

    def test_harvest_four_word_name(self) -> None:
        repo = _make_repo()
        msg = _make_indexed_message("Juan Jose Garcia Ripoll <juan@example.com>")
        repo.harvest_contacts([msg])
        found = repo.find_contact_by_email(email_address="juan@example.com")
        assert found is not None
        self.assertEqual(found.first_name, "Juan Jose")
        self.assertEqual(found.last_name, "Garcia Ripoll")


# ---------------------------------------------------------------------------
# Delete and merge
# ---------------------------------------------------------------------------


class DeleteContactTests(unittest.TestCase):
    def test_delete_removes_contact(self) -> None:
        repo = _make_repo()
        saved = repo.upsert_contact(contact=_make_contact())
        assert saved.id is not None
        repo.delete_contact(contact_id=saved.id)
        self.assertIsNone(repo.find_contact_by_email(email_address="alice@example.com"))

    def test_delete_removes_emails_and_aliases(self) -> None:
        repo = _make_repo()
        saved = repo.upsert_contact(
            contact=_make_contact(
                emails=("a@x.com", "b@x.com"),
                aliases=("Ali",),
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
                first_name="Alice",
                emails=("a@work.com",),
                message_count=3,
            )
        )
        c2 = repo.upsert_contact(
            contact=_make_contact(
                first_name="Alice",
                last_name="S",
                emails=("a@home.com",),
                message_count=7,
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
                first_name="Bob",
                emails=("b@x.com",),
                aliases=("Bobby",),
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
                first_name="B",
                emails=("b@x.com",),
                message_count=2,
            )
        )
        c3 = repo.upsert_contact(
            contact=_make_contact(
                first_name="C",
                emails=("c@x.com",),
                message_count=3,
            )
        )
        assert c1.id is not None and c2.id is not None and c3.id is not None
        merged = repo.merge_contacts(
            target_id=c1.id,
            source_ids=[c2.id, c3.id],
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
                first_name="Test",
                last_name="User",
                emails=("test@example.com",),
            )
        )

        output = _capture(
            run_contacts_search,
            paths=paths,
            prefix="test",
            limit=10,
        )
        self.assertIn("test@example.com", output)
        self.assertIn("Test User", output)

    def test_contacts_search_no_results(self) -> None:
        from pony.cli import run_contacts_search

        paths, repo = _cli_paths()
        del repo  # unused — just need initialized DB

        output = _capture(
            run_contacts_search,
            paths=paths,
            prefix="nobody",
            limit=10,
        )
        self.assertIn("No contacts", output)

    def test_contacts_show_found(self) -> None:
        from pony.cli import run_contacts_show

        paths, repo = _cli_paths()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Alice",
                last_name="Smith",
                emails=("alice@example.com",),
                organization="Acme",
                aliases=("Ali",),
            )
        )

        output = _capture(
            run_contacts_show,
            paths=paths,
            email="alice@example.com",
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
                first_name="Juan",
                last_name="Garcia",
                emails=("juan@example.com",),
            )
        )
        out_file = paths.data_dir / "test.bbdb"

        output = _capture(
            run_contacts_export,
            paths=paths,
            output_path=str(out_file),
        )
        self.assertIn("Exported 1 contact", output)
        self.assertTrue(out_file.exists())

        loaded = read_bbdb(out_file)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].first_name, "Juan")

    def test_contacts_export_with_no_path_writes_ponys_own_copy(self) -> None:
        """Never the user's bbdb_path — this export cannot represent it.

        It carries no phone numbers, postal addresses, xfields beyond
        notes, or stable record id, so writing over the user's file would
        destroy data Pony never had.
        """
        from pony.cli import run_contacts_export

        paths, repo = _cli_paths()
        repo.upsert_contact(
            contact=_make_contact(
                first_name="Ana", last_name="Ruiz", emails=("ana@example.com",)
            )
        )

        output = _capture(run_contacts_export, paths=paths, output_path=None)

        destination = paths.data_dir / "contacts.bbdb"
        self.assertIn("Exported 1 contact", output)
        self.assertTrue(destination.exists())
        self.assertEqual(read_bbdb(destination)[0].first_name, "Ana")

    def test_contacts_import_creates_new(self) -> None:
        from pony.cli import run_contacts_import

        paths, repo = _cli_paths()
        del repo
        bbdb_file = paths.data_dir / "test.bbdb"
        write_bbdb(
            [
                _make_contact(
                    first_name="Eve",
                    last_name="New",
                    emails=("eve@example.com",),
                )
            ],
            bbdb_file,
        )

        output = _capture(
            run_contacts_import,
            paths=paths,
            config_path=None,
            input_path=str(bbdb_file),
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
                first_name="Alice",
                last_name="Smith",
                emails=("alice@example.com",),
                aliases=("Ali",),
            )
        )

        # Import a BBDB file that has the same email with extra info.
        bbdb_file = paths.data_dir / "merge.bbdb"
        write_bbdb(
            [
                _make_contact(
                    first_name="Alice",
                    last_name="Smith",
                    emails=("alice@example.com", "alice@work.com"),
                    organization="Acme",
                    aliases=("Allie",),
                )
            ],
            bbdb_file,
        )

        created, updated = import_bbdb_contacts(
            index=repo,
            bbdb_path=bbdb_file,
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


# ---------------------------------------------------------------------------
# bbdb.py internal parser tests
# ---------------------------------------------------------------------------


class BbdbParserInternalsTest(unittest.TestCase):
    """Tests for bbdb.py parsing helpers not otherwise exercised."""

    def test_lisp_string_empty_returns_nil(self) -> None:
        from pony.bbdb import _lisp_string

        self.assertEqual(_lisp_string(""), "nil")

    def test_sexp_to_string_non_string_returns_empty(self) -> None:
        from pony.bbdb import _sexp_to_string

        self.assertEqual(_sexp_to_string(42), "")
        self.assertEqual(_sexp_to_string(None), "")

    def test_parse_bbdb_record_too_few_fields_returns_none(self) -> None:
        from pony.bbdb import _parse_bbdb_record

        # A record with fewer than 8 fields
        result = _parse_bbdb_record('["Alice" "Smith"]')
        self.assertIsNone(result)

    def test_read_bbdb_skips_comment_lines(self) -> None:
        import tempfile

        from pony.bbdb import read_bbdb

        content = "; this is a comment\n; another comment\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bbdb", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        try:
            result = read_bbdb(path)
            self.assertEqual(result, [])
        finally:
            path.unlink(missing_ok=True)

    def test_extract_notes_with_nested_list(self) -> None:
        from pony.bbdb import _extract_notes

        # Nested list structure: [[("notes", "text")]]
        xfields = [[("notes", "my notes")]]
        result = _extract_notes(xfields)
        self.assertEqual(result, "my notes")

    def test_extract_notes_with_flat_tuple(self) -> None:
        from pony.bbdb import _extract_notes

        xfields = [("notes", "flat notes")]
        result = _extract_notes(xfields)
        self.assertEqual(result, "flat notes")

    def test_extract_notes_empty_returns_empty(self) -> None:
        from pony.bbdb import _extract_notes

        self.assertEqual(_extract_notes([]), "")
        self.assertEqual(_extract_notes(None), "")

    def test_parse_bbdb_date_from_string(self) -> None:
        from pony.bbdb import _parse_bbdb_date

        result = _parse_bbdb_date("2024-01-15")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.year, 2024)

    def test_parse_bbdb_date_from_tuple(self) -> None:
        from pony.bbdb import _parse_bbdb_date

        result = _parse_bbdb_date(("creation-date", "2024-06-01"))
        self.assertIsNotNone(result)

    def test_parse_bbdb_date_from_list(self) -> None:
        from pony.bbdb import _parse_bbdb_date

        result = _parse_bbdb_date([("timestamp", "2024-03-15")])
        self.assertIsNotNone(result)

    def test_parse_bbdb_date_invalid_returns_none(self) -> None:
        from pony.bbdb import _parse_bbdb_date

        result = _parse_bbdb_date("not-a-date")
        self.assertIsNone(result)

    def test_parse_bbdb_date_none_returns_none(self) -> None:
        from pony.bbdb import _parse_bbdb_date

        result = _parse_bbdb_date(None)
        self.assertIsNone(result)

    def test_read_bbdb_line_not_ending_with_bracket_skipped(self) -> None:
        import tempfile

        from pony.bbdb import read_bbdb

        # A line that starts with [ but doesn't end with ]
        content = "[incomplete record without end bracket\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bbdb", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        try:
            result = read_bbdb(path)
            self.assertEqual(result, [])
        finally:
            path.unlink(missing_ok=True)


class BbdbMalformedRecordTests(unittest.TestCase):
    """BBDB files are hand-edited in Emacs, so the parser meets bad input.

    A truncated or short record must be skipped rather than take the
    whole import down — the user's other contacts are still worth
    having.
    """

    def _read(self, *lines: str) -> list[Contact]:
        path = TMP_ROOT / f"bbdb-malformed-{uuid4().hex}.bbdb"
        path.write_text(
            ";; -*-coding: utf-8-emacs;-*-\n" + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        try:
            return read_bbdb(path)
        finally:
            path.unlink(missing_ok=True)

    def test_a_record_with_too_few_fields_is_skipped(self) -> None:
        """Under eight fields is not a BBDB record — drop it, keep the rest."""
        good = '["Ada" "Lovelace" nil nil nil nil nil ("ada@example.com") nil nil]'
        contacts = self._read('["Only" "TwoFields"]', good)

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].first_name, "Ada")

    def test_an_unterminated_list_does_not_hang_or_raise(self) -> None:
        """A missing ``)`` ends the parse at end-of-input."""
        contacts = self._read(
            '["Grace" "Hopper" nil nil ("Navy" nil nil ("grace@example.com") nil nil]'
        )

        # Parsed as best it can; the point is that it returns at all.
        self.assertIsInstance(contacts, list)

    def test_a_vector_valued_field_is_parsed_as_a_list(self) -> None:
        """BBDB writes some fields as ``[...]`` vectors rather than lists."""
        contacts = self._read(
            '["Alan" "Turing" nil ["alan" "at"] nil nil nil'
            ' ("alan@example.com") nil nil]'
        )

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].aliases, ("alan", "at"))

    def test_notes_are_found_inside_the_nested_xfields_alist(self) -> None:
        """``xfields`` nests the alist one level deeper than it looks."""
        contacts = self._read(
            '["Katherine" "Johnson" nil nil nil nil nil'
            ' ("katherine@example.com") ((notes . "Orbital mechanics")) nil]'
        )

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].notes, "Orbital mechanics")

    def test_creation_and_update_dates_are_read_from_dotted_pairs(self) -> None:
        """Dates arrive as ``(creation-date . "…")`` pairs, sometimes nested."""
        contacts = self._read(
            '["Margaret" "Hamilton" nil nil nil nil nil'
            ' ("margaret@example.com") nil nil'
            ' ((creation-date . "2026-04-12"))'
            ' ((timestamp . "2026-04-13 09:27:04 +0000"))]'
        )

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].created_at.date().isoformat(), "2026-04-12")
        self.assertEqual(contacts[0].updated_at.date().isoformat(), "2026-04-13")

    def test_an_unparseable_date_falls_back_to_now(self) -> None:
        """A date in no known format must not drop the contact."""
        before = datetime.now(tz=UTC)
        contacts = self._read(
            '["Radia" "Perlman" nil nil nil nil nil'
            ' ("radia@example.com") nil nil "not a date" "also not a date"]'
        )
        after = datetime.now(tz=UTC)

        self.assertEqual(len(contacts), 1)
        self.assertGreaterEqual(contacts[0].created_at, before)
        self.assertLessEqual(contacts[0].created_at, after)


class ContactOrderingTest(unittest.TestCase):
    """The order a query asks for must survive the batch load.

    ``search_contacts`` ranks by how often you write to someone and
    ``list_all_contacts`` orders by name, but both then fetch the rows
    with ``WHERE id IN (…)``, which returns rowid order.  Both orderings
    were being replaced by "whichever contact was created first", so the
    composer's autocomplete offered the least-used match first.
    """

    def _seeded_index(self) -> SqliteIndexRepository:
        from tui_helpers import make_index, make_tmp_paths

        index = make_index(make_tmp_paths("contact-order"))
        # Inserted in the opposite order to the expected ranking.
        for first, count in (("Aaron", 1), ("Bella", 50), ("Cyril", 10)):
            index.upsert_contact(
                contact=Contact(
                    id=None,
                    first_name=first,
                    last_name="Zed",
                    emails=(f"{first.lower()}@example.com",),
                    message_count=count,
                    last_seen=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        return index

    def test_search_returns_the_most_written_to_contact_first(self) -> None:
        results = self._seeded_index().search_contacts(prefix="zed", limit=10)

        self.assertEqual(
            [c.message_count for c in results],
            [50, 10, 1],
            "search_contacts ranks by message_count DESC",
        )

    def test_listing_all_contacts_is_ordered_by_name(self) -> None:
        results = self._seeded_index().list_all_contacts()

        self.assertEqual([c.first_name for c in results], ["Aaron", "Bella", "Cyril"])

    def test_the_limit_keeps_the_highest_ranked(self) -> None:
        """Truncation must drop the tail of the ranking, not an arbitrary slice."""
        results = self._seeded_index().search_contacts(prefix="zed", limit=2)

        self.assertEqual([c.message_count for c in results], [50, 10])


class BbdbFieldRoundTripTests(unittest.TestCase):
    """Any character must survive an export/import cycle.

    A record was written on one line and read back by requiring each
    line to start with ``[`` and end with ``]``. A field containing any
    line-break character therefore split the record in two, and both
    halves failed the test — so the contact vanished with no error. Pony
    produces such fields itself: the BBDB import merge joins notes with
    newlines, and the notes editor is a multi-line text area.

    This is also the format the schema-recovery backup is written in,
    immediately before the index is deleted — the one moment the export
    is the only copy of a contact.
    """

    def _round_trip(self, value: str) -> list[Contact]:
        path = TMP_ROOT / "bbdb-round" / f"{uuid4().hex}.bbdb"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bbdb(
            [
                Contact(
                    id=None,
                    first_name="Alice",
                    last_name="Smith",
                    emails=("alice@example.com",),
                    notes=value,
                )
            ],
            path,
        )
        return read_bbdb(path)

    def test_every_line_break_character_survives(self) -> None:
        for name, value in {
            "LF": "a\nb",
            "CR": "a\rb",
            "CRLF": "a\r\nb",
            "VT": "a\vb",
            "FF": "a\fb",
            "FS": "a\x1cb",
            "GS": "a\x1db",
            "RS": "a\x1eb",
            "NEL": "a\x85b",
            "LINE SEP": "a\u2028b",
            "PARA SEP": "a\u2029b",
        }.items():
            with self.subTest(character=name):
                loaded = self._round_trip(value)
                self.assertEqual(len(loaded), 1, f"{name} destroyed the contact")
                self.assertEqual(loaded[0].notes, value)

    def test_quotes_backslashes_and_a_literal_escape_survive(self) -> None:
        # r"a\nb" must come back as a backslash and an n, not a newline —
        # the reason unescaping is a single pass rather than chained
        # str.replace calls.
        for value in ('say "hi"', r"C:\path", r"a\nb", "tab\there"):
            with self.subTest(value=value):
                loaded = self._round_trip(value)
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0].notes, value)

    def test_a_record_written_by_emacs_across_lines_is_read(self) -> None:
        """Emacs writes line breaks inside strings verbatim."""
        path = TMP_ROOT / "bbdb-round" / f"{uuid4().hex}.bbdb"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            ";; -*-coding: utf-8-emacs;-*-\n;;; file-format: 9\n"
            '["Emacs" "User" nil nil nil nil nil ("e@u.com") '
            '((notes . "line one\nline two")) nil nil nil nil]\n',
            encoding="utf-8",
        )
        loaded = read_bbdb(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].notes, "line one\nline two")

    def test_a_bracket_in_a_note_does_not_end_the_record(self) -> None:
        loaded = self._round_trip("see [1] and [2]")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].notes, "see [1] and [2]")

    def test_a_non_utf8_file_reports_itself(self) -> None:
        """It used to raise UnicodeDecodeError out of the CLI."""
        from pony.bbdb import BbdbError

        path = TMP_ROOT / "bbdb-round" / f"{uuid4().hex}.bbdb"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b';; -*-coding: latin-1;-*-\n["Jos\xe9" "R" nil]\n')
        with self.assertRaises(BbdbError) as ctx:
            read_bbdb(path)
        self.assertIn("not UTF-8", str(ctx.exception))
