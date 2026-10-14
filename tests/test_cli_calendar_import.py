"""``pony calendar import``: ingesting an `.ics` from the command line.

Lifted out of the calendar's own suite with the command itself -- a
command line is a front end, and this one is Pony's.  What the ingest
*does* is still the calendar model's, and
`tests_calendar/test_ingest.py` tests that side -- and this file uses
its fixtures, because driving the model is what a command does.
"""

from __future__ import annotations

import io
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pony.calendar.domain import (
    AccountConfig,
    AppConfig,
    ComponentRef,
    LocalStatus,
    PlaintextCredential,
    VEvent,
)
from pony.calendar.index_store import SqliteIndexRepository
from pony.calendar.paths import CalendarPaths
from pony.calendar.storage import VdirMirrorRepository
from tests_calendar import corpus
from tests_calendar.fake_caldav import FakeCalDAVSession


class CalendarRootMixin:
    """Supplies ``self.calendar_paths`` from the autouse fixture.

    A `unittest.TestCase` method cannot be handed a fixture, so
    `tests/conftest.py` puts the per-test calendar root on the class.
    The annotation is what says so to a reader and to the type checker.
    """

    calendar_paths: CalendarPaths


class IngestCliTest(CalendarRootMixin, unittest.TestCase):
    """`pony calendar import`, over real files and a real index."""

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.tmp = tmp
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def _account(self, name: str = "personal") -> AccountConfig:
        return AccountConfig(
            name=name,
            url="https://caldav.example.com/dav/",
            username="user@example.com",
            credential=PlaintextCredential(password="s3cret"),
            mirror_path=Path("/unused"),
            trash_retention_days=30,
            include=(re.compile(".*"),),
            exclude=(),
            read_only=(),
        )

    def _config(self, *accounts: AccountConfig) -> AppConfig:
        return AppConfig(
            config_version=1,
            use_utf8=False,
            editor=None,
            accounts=tuple(accounts) or (self._account(),),
        )

    def _ctx(
        self,
        config: AppConfig | None = None,
        session_factory: object | None = None,
    ) -> object:
        from pony import cli_calendar as cli
        from pony.calendar.credentials import DefaultCredentialsProvider

        return cli.CliContext(
            config=config or self._config(),
            mirror=self.mirror,
            index=self.index,
            creds=DefaultCredentialsProvider(),
            paths=self.calendar_paths,
            stdout=self.stdout,
            stderr=self.stderr,
            now=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            session_factory=session_factory,  # type: ignore[arg-type]
        )

    def _sync_ctx(self) -> tuple[object, FakeCalDAVSession, list[str]]:
        """Context whose sessions hit a fake server with a `work` calendar."""
        session = FakeCalDAVSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")
        synced: list[str] = []

        def factory(account: AccountConfig, _auth: object) -> FakeCalDAVSession:
            synced.append(account.name)
            return session

        return self._ctx(session_factory=factory), session, synced

    def _write_ics(self, name: str, payload: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(payload)
        return p

    # ------------------------------------------------------------------
    # Both flags supplied
    # ------------------------------------------------------------------

    def test_import_with_both_flags(self) -> None:
        from pony import cli_calendar as cli

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        # Seed the index so list_calendars returns something.
        self.index.upsert_component(
            VEvent(
                ref=ComponentRef("personal", "work", "seed@example.com"),
                href="/seed.ics",
                etag="etag-seed",
                raw_ics=b"",
                summary="seed",
                description=None,
                location=None,
                dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                dtend=None,
                status=None,
                local_flags=frozenset(),
                server_flags=frozenset(),
                local_status=LocalStatus.ACTIVE,
                trashed_at=None,
                synced_at=None,
            )
        )
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name="personal",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
            sync=False,
        )
        self.assertEqual(code, 0)
        self.assertIn("imported 1", self.stdout.getvalue())

    def test_import_non_interactive_missing_flags_exits_2(self) -> None:
        from pony import cli_calendar as cli

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name=None,
            calendar_name=None,
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
            sync=False,
        )
        self.assertEqual(code, 2)
        self.assertIn("non-interactive", self.stderr.getvalue())

    def test_import_interactive_prompts_calendar_selection(self) -> None:
        from pony import cli_calendar as cli

        # Seed two calendars so the menu has two entries.
        for cal in ("work", "home"):
            self.index.upsert_component(
                VEvent(
                    ref=ComponentRef("personal", cal, f"seed-{cal}@example.com"),
                    href=f"/{cal}/seed.ics",
                    etag="etag",
                    raw_ics=b"",
                    summary="seed",
                    description=None,
                    location=None,
                    dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                    dtend=None,
                    status=None,
                    local_flags=frozenset(),
                    server_flags=frozenset(),
                    local_status=LocalStatus.ACTIVE,
                    trashed_at=None,
                    synced_at=None,
                )
            )

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        # User picks option 1 (personal/home, sorted alphabetically).
        prompts: list[str] = []

        def capture_prompt(msg: str) -> str:
            prompts.append(msg)
            return "1"

        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name=None,
            calendar_name=None,
            on_conflict="skip",
            prompt=capture_prompt,
            is_interactive=lambda: True,
            sync=False,
        )
        self.assertEqual(code, 0)
        self.assertTrue(prompts, "expected at least one prompt")
        self.assertIn("imported 1", self.stdout.getvalue())

    def test_import_directory_walks_ics_files(self) -> None:
        from pony import cli_calendar as cli

        ics_dir = self.tmp / "ics_dir"
        ics_dir.mkdir()
        (ics_dir / "a.ics").write_bytes(corpus.simple_event())
        (ics_dir / "b.ics").write_bytes(corpus.simple_todo())
        (ics_dir / "not_ics.txt").write_text("ignore me")

        ctx = self._ctx()
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_dir],
            account_name="personal",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
            sync=False,
        )
        self.assertEqual(code, 0)
        self.assertIn("imported 2", self.stdout.getvalue())

    def test_import_unknown_account_exits_2(self) -> None:
        from pony import cli_calendar as cli

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name="no-such-account",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
            sync=False,
        )
        self.assertEqual(code, 2)
        self.assertIn("no-such-account", self.stderr.getvalue())

    def test_import_all_skipped_returns_nonzero(self) -> None:
        from pony import cli_calendar as cli

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        # First import succeeds.
        cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name="personal",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
            sync=False,
        )
        # Second import with skip should return non-zero.
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name="personal",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
            sync=False,
        )
        self.assertEqual(code, 1)

    # ------------------------------------------------------------------
    # Sync after import
    # ------------------------------------------------------------------

    def _import(self, ctx: object, **kwargs: object) -> int:
        from pony import cli_calendar as cli

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        args: dict[str, object] = {
            "paths": [ics_file],
            "account_name": "personal",
            "calendar_name": "work",
            "on_conflict": "skip",
            "prompt": lambda _: "",
            "is_interactive": lambda: False,
        }
        args.update(kwargs)
        return cli.cmd_import(ctx, **args)  # type: ignore[arg-type]

    def test_import_non_interactive_syncs_target_account(self) -> None:
        ctx, session, synced = self._sync_ctx()
        code = self._import(ctx)
        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(synced, ["personal"])
        self.assertEqual(len(session.hrefs_in("https://cal.example.com/work/")), 1)

    def test_import_interactive_asks_before_syncing(self) -> None:
        ctx, _session, synced = self._sync_ctx()
        prompts: list[str] = []

        def answer(msg: str) -> str:
            prompts.append(msg)
            return "y"

        code = self._import(ctx, prompt=answer, is_interactive=lambda: True)
        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(len(prompts), 1)
        self.assertIn("Sync account 'personal'", prompts[0])
        self.assertEqual(synced, ["personal"])

    def test_import_interactive_decline_skips_sync(self) -> None:
        ctx, _session, synced = self._sync_ctx()
        code = self._import(ctx, prompt=lambda _: "n", is_interactive=lambda: True)
        self.assertEqual(code, 0)
        self.assertEqual(synced, [])
        self.assertIn("Not synced", self.stdout.getvalue())

    def test_import_yes_syncs_without_prompting(self) -> None:
        ctx, _session, synced = self._sync_ctx()

        def fail(_msg: str) -> str:
            raise AssertionError("unexpected prompt")

        code = self._import(ctx, prompt=fail, is_interactive=lambda: True, yes=True)
        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(synced, ["personal"])

    def test_import_nothing_imported_does_not_sync(self) -> None:
        ctx, _session, synced = self._sync_ctx()
        self._import(ctx, sync=False)
        code = self._import(ctx)
        self.assertEqual(code, 1)
        self.assertEqual(synced, [])
