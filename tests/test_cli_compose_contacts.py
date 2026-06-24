"""CLI tests for ``compose`` and the ``contacts`` subcommands.

Covers ``run_compose`` (error branches plus a patched happy path) and the
contacts import/export/show/search code paths in ``pony.cli`` without
opening a TUI or touching the network.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock
from uuid import uuid4

from conftest import TMP_ROOT
from test_cli import (
    isolated_app_env,
    run_cli,
    run_cli_capture,
    sample_config_toml,
    temporary_config,
)

from pony.bbdb import write_bbdb
from pony.domain import Contact


def _temp_dir() -> Path:
    """Return a fresh scratch directory under the test tmp root."""
    path = TMP_ROOT / "cli_compose_contacts" / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_bbdb_file(contacts: list[Contact]) -> Path:
    """Write *contacts* to a fresh BBDB file and return its path."""
    dest = _temp_dir() / "import.bbdb"
    write_bbdb(contacts, dest)
    return dest


def _local_only_config_toml() -> str:
    """Return a config with one local account that cannot send (no SMTP)."""
    return (
        "config_version = 2\n\n"
        "[[accounts]]\n"
        'account_type = "local"\n'
        'name = "localbox"\n'
        'email_address = "me@example.com"\n\n'
        "[accounts.mirror]\n"
        'path = "mirrors/local"\n'
        'format = "maildir"\n'
        "trash_retention_days = 30\n"
    )


def _mixed_config_toml() -> str:
    """Return a config with a sendable IMAP account plus a local one."""
    local_block = (
        "\n[[accounts]]\n"
        'account_type = "local"\n'
        'name = "localbox"\n'
        'email_address = "me@example.com"\n\n'
        "[accounts.mirror]\n"
        'path = "mirrors/local"\n'
        'format = "mbox"\n'
        "trash_retention_days = 30\n"
    )
    return sample_config_toml() + "\n" + local_block


def _write_config(toml: str) -> Path:
    """Write *toml* to a fresh config file and return its path."""
    config_path = _temp_dir() / "config.toml"
    config_path.write_text(toml, encoding="utf-8")
    return config_path


def _sample_contact(
    *,
    first_name: str = "Alice",
    last_name: str = "Example",
    emails: tuple[str, ...] = ("alice@example.com",),
    aliases: tuple[str, ...] = (),
    organization: str = "",
    notes: str = "",
    affix: tuple[str, ...] = (),
) -> Contact:
    now = datetime(2024, 1, 2, 3, 4, tzinfo=UTC)
    return Contact(
        id=None,
        first_name=first_name,
        last_name=last_name,
        emails=emails,
        aliases=aliases,
        organization=organization,
        notes=notes,
        affix=affix,
        message_count=3,
        last_seen=now,
        created_at=now,
        updated_at=now,
    )


class RunComposeTests(unittest.TestCase):
    """Exercise ``run_compose`` argument and account handling."""

    def test_compose_unknown_account_errors(self) -> None:
        """An account name absent from config aborts with SystemExit."""
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            self.assertRaises(SystemExit) as ctx,
        ):
            run_cli(
                "--config",
                str(config_path),
                "compose",
                "--account",
                "ghost",
            )
        self.assertIn("ghost", str(ctx.exception))
        self.assertIn("No account named", str(ctx.exception))

    def test_compose_no_sendable_account_errors(self) -> None:
        """A config with only a non-SMTP local account aborts compose."""
        config_path = _write_config(_local_only_config_toml())
        with isolated_app_env(), self.assertRaises(SystemExit) as ctx:
            run_cli("--config", str(config_path), "compose")
        self.assertIn("SMTP", str(ctx.exception))

    def test_compose_named_account_without_smtp_errors(self) -> None:
        """Naming an existing account that has no SMTP gives a clear error."""
        config_path = _write_config(_mixed_config_toml())
        with isolated_app_env(), self.assertRaises(SystemExit) as ctx:
            run_cli(
                "--config",
                str(config_path),
                "compose",
                "--account",
                "localbox",
            )
        self.assertIn("no SMTP", str(ctx.exception))

    def test_compose_launches_app_with_patched_run(self) -> None:
        """Happy path builds a ComposeApp and runs it (patched no-op)."""
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch("pony.tui.ComposeApp") as compose_app,
        ):
            instance = compose_app.return_value
            instance.run.return_value = None
            rc = run_cli(
                "--config",
                str(config_path),
                "compose",
                "--to",
                "bob@example.com",
                "--subject",
                "Hi",
                "--body",
                "Hello there",
            )
        self.assertEqual(rc, "")  # run_cli returns captured stdout
        compose_app.assert_called_once()
        instance.run.assert_called_once_with()
        kwargs = compose_app.call_args.kwargs
        self.assertEqual(kwargs["to"], "bob@example.com")
        self.assertEqual(kwargs["subject"], "Hi")
        self.assertEqual(kwargs["body"], "Hello there")

    def test_compose_named_sendable_account_with_mbox_mirror(self) -> None:
        """Naming a sendable account selects it; mbox mirrors build cleanly."""
        config_path = _write_config(_mixed_config_toml())
        with (
            isolated_app_env(),
            mock.patch("pony.tui.ComposeApp") as compose_app,
        ):
            compose_app.return_value.run.return_value = None
            run_cli(
                "--config",
                str(config_path),
                "compose",
                "--account",
                "personal",
            )
        kwargs = compose_app.call_args.kwargs
        self.assertEqual(kwargs["account"].name, "personal")
        # Both accounts get a mirror, including the mbox local account.
        self.assertIn("localbox", kwargs["mirrors"])

    def test_compose_markdown_override_and_signature(self) -> None:
        """--markdown forces markdown mode; empty body gets a signature body."""
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch("pony.tui.ComposeApp") as compose_app,
        ):
            compose_app.return_value.run.return_value = None
            run_cli(
                "--config",
                str(config_path),
                "compose",
                "--markdown",
            )
        kwargs = compose_app.call_args.kwargs
        self.assertTrue(kwargs["markdown_mode"])
        # No --body supplied, so the body comes from new_compose_body().
        self.assertIsInstance(kwargs["body"], str)

    def test_compose_bad_theme_returns_error(self) -> None:
        """An unknown theme short-circuits before launching the app."""
        with (
            isolated_app_env(),
            temporary_config() as config_path,
            mock.patch("pony.tui.ComposeApp") as compose_app,
        ):
            _out, rc = run_cli_capture(
                "--config",
                str(config_path),
                "--theme",
                "no-such-theme",
                "compose",
            )
        self.assertNotEqual(rc, 0)
        compose_app.assert_not_called()


class ContactsImportTests(unittest.TestCase):
    """Exercise ``run_contacts_import``."""

    def test_import_creates_contacts(self) -> None:
        """Importing a fresh BBDB file creates new contacts."""
        bbdb = _write_bbdb_file(
            [
                _sample_contact(
                    first_name="Carol",
                    last_name="Imported",
                    emails=("carol@example.com",),
                ),
            ]
        )
        with isolated_app_env(), temporary_config() as config_path:
            output = run_cli(
                "--config", str(config_path), "contacts", "import", str(bbdb)
            )
            self.assertIn("1 new", output)
            shown = run_cli(
                "--config",
                str(config_path),
                "contacts",
                "show",
                "carol@example.com",
            )
        self.assertIn("Carol Imported", shown)

    def test_import_merges_existing_contact(self) -> None:
        """Re-importing a contact with a shared email merges rather than dupes."""
        first = _write_bbdb_file(
            [_sample_contact(emails=("alice@example.com",), notes="first")]
        )
        second = _write_bbdb_file(
            [
                _sample_contact(
                    emails=("alice@example.com", "alice2@example.com"),
                    notes="second",
                ),
            ]
        )
        with isolated_app_env(), temporary_config() as config_path:
            run_cli("--config", str(config_path), "contacts", "import", str(first))
            output = run_cli(
                "--config", str(config_path), "contacts", "import", str(second)
            )
            self.assertIn("0 new", output)
            self.assertIn("1 updated", output)
            shown = run_cli(
                "--config",
                str(config_path),
                "contacts",
                "show",
                "alice2@example.com",
            )
        self.assertIn("alice@example.com", shown)
        self.assertIn("alice2@example.com", shown)

    def test_import_uses_bbdb_path_from_config(self) -> None:
        """With no path argument, import falls back to config bbdb_path."""
        bbdb = _write_bbdb_file(
            [_sample_contact(first_name="Gus", emails=("gus@example.com",))]
        )
        toml = f'bbdb_path = "{bbdb}"\n\n' + sample_config_toml()
        config_path = _write_config(toml)
        with isolated_app_env():
            output = run_cli("--config", str(config_path), "contacts", "import")
        self.assertIn("1 new", output)

    def test_import_merge_keeps_notes_when_unchanged(self) -> None:
        """Re-importing identical notes does not duplicate the notes text."""
        bbdb = _write_bbdb_file(
            [_sample_contact(emails=("alice@example.com",), notes="same note")]
        )
        with isolated_app_env(), temporary_config() as config_path:
            run_cli("--config", str(config_path), "contacts", "import", str(bbdb))
            output = run_cli(
                "--config", str(config_path), "contacts", "import", str(bbdb)
            )
            self.assertIn("1 updated", output)
            shown = run_cli(
                "--config",
                str(config_path),
                "contacts",
                "show",
                "alice@example.com",
            )
        self.assertEqual(shown.count("same note"), 1)

    def test_import_missing_file_returns_error(self) -> None:
        """A nonexistent input path reports an error and returns 1."""
        missing = _temp_dir() / "nope.bbdb"
        with isolated_app_env(), temporary_config() as config_path:
            out, rc = run_cli_capture(
                "--config", str(config_path), "contacts", "import", str(missing)
            )
        self.assertEqual(rc, 1)
        self.assertIn("File not found", out)

    def test_import_no_path_no_config_default(self) -> None:
        """With no path and no bbdb_path configured, import explains usage."""
        with isolated_app_env(), temporary_config() as config_path:
            out, rc = run_cli_capture(
                "--config", str(config_path), "contacts", "import"
            )
        self.assertEqual(rc, 1)
        self.assertIn("No input path", out)


class ContactsExportTests(unittest.TestCase):
    """Exercise ``run_contacts_export``."""

    def test_export_writes_file(self) -> None:
        """Export writes a BBDB file and reports the count."""
        bbdb = _write_bbdb_file(
            [_sample_contact(first_name="Dora", emails=("dora@example.com",))]
        )
        out_path = _temp_dir() / "out.bbdb"
        with isolated_app_env(), temporary_config() as config_path:
            run_cli("--config", str(config_path), "contacts", "import", str(bbdb))
            output = run_cli(
                "--config",
                str(config_path),
                "contacts",
                "export",
                str(out_path),
            )
        self.assertTrue(out_path.exists())
        self.assertIn("Exported", output)
        self.assertIn(str(out_path), output)
        self.assertIn("dora@example.com", out_path.read_text(encoding="utf-8"))

    def test_export_no_path_no_config_default(self) -> None:
        """With no path and no bbdb_path configured, export explains usage."""
        with isolated_app_env(), temporary_config() as config_path:
            out, rc = run_cli_capture(
                "--config", str(config_path), "contacts", "export"
            )
        self.assertEqual(rc, 1)
        self.assertIn("No output path", out)

    def test_export_uses_bbdb_path_from_config(self) -> None:
        """When no path is given, export falls back to config bbdb_path."""
        out_path = _temp_dir() / "configured.bbdb"
        # bbdb_path must be a top-level key, declared before the account
        # tables (otherwise TOML folds it into the last table).
        toml = f'bbdb_path = "{out_path}"\n\n' + sample_config_toml()
        config_path = _write_config(toml)
        with isolated_app_env():
            output = run_cli("--config", str(config_path), "contacts", "export")
        self.assertTrue(out_path.exists())
        self.assertIn("Exported", output)


class ContactsShowTests(unittest.TestCase):
    """Exercise ``run_contacts_show``."""

    def test_show_missing_contact_returns_one(self) -> None:
        """Looking up an unknown email reports nothing found and returns 1."""
        with isolated_app_env(), temporary_config() as config_path:
            out, rc = run_cli_capture(
                "--config",
                str(config_path),
                "contacts",
                "show",
                "ghost@example.com",
            )
        self.assertEqual(rc, 1)
        self.assertIn("No contact found", out)

    def test_show_renders_all_optional_fields(self) -> None:
        """A rich contact prints affix, aliases, org, and notes lines."""
        bbdb = _write_bbdb_file(
            [
                _sample_contact(
                    first_name="Eve",
                    last_name="Rich",
                    emails=("eve@example.com",),
                    aliases=("evie",),
                    organization="Acme",
                    notes="VIP customer",
                    affix=("Dr.",),
                ),
            ]
        )
        with isolated_app_env(), temporary_config() as config_path:
            run_cli("--config", str(config_path), "contacts", "import", str(bbdb))
            out = run_cli(
                "--config",
                str(config_path),
                "contacts",
                "show",
                "eve@example.com",
            )
        self.assertIn("Eve Rich", out)
        self.assertIn("Affix:", out)
        self.assertIn("Aliases:", out)
        self.assertIn("evie", out)
        self.assertIn("Organization: Acme", out)
        self.assertIn("VIP customer", out)


class ContactsSearchTests(unittest.TestCase):
    """Exercise ``run_contacts_search``."""

    def test_search_no_results(self) -> None:
        """Searching an empty store reports no matches and returns 0."""
        with isolated_app_env(), temporary_config() as config_path:
            out, rc = run_cli_capture(
                "--config", str(config_path), "contacts", "search", "zzz"
            )
        self.assertEqual(rc, 0)
        self.assertIn("No contacts matching", out)

    def test_search_lists_matches_with_alias(self) -> None:
        """An imported contact with an alias shows it in search results."""
        bbdb = _write_bbdb_file(
            [
                _sample_contact(
                    first_name="Frank",
                    last_name="Finder",
                    emails=("frank@example.com",),
                    aliases=("franky",),
                ),
            ]
        )
        with isolated_app_env(), temporary_config() as config_path:
            run_cli("--config", str(config_path), "contacts", "import", str(bbdb))
            out = run_cli("--config", str(config_path), "contacts", "search", "frank")
        self.assertIn("frank@example.com", out)
        self.assertIn("aka", out)
        self.assertIn("franky", out)


if __name__ == "__main__":
    unittest.main()
