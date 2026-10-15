from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from pony.calendar.config import ConfigDocument, mirror_roots
from pony.calendar.credentials import DefaultCredentialsProvider
from pony.calendar.domain import (
    AccountConfig,
    AppConfig,
    ComponentRef,
    EnvCredential,
    LocalStatus,
    PlaintextCredential,
    ResourceRef,
    VEvent,
)
from pony.calendar.index_store import SqliteIndexRepository
from pony.calendar.oauth import (
    has_local_graphical_browser,
)
from pony.calendar.paths import CalendarPaths
from pony.calendar.services import default_session_factory
from pony.calendar.storage import VdirMirrorRepository
from pony.cli import calendar as cli
from pony.scope import SyncScope
from tests_calendar import corpus
from tests_calendar.fake_caldav import FakeCalDAVSession

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)


def _account(
    *,
    name: str = "personal",
    credential: PlaintextCredential | EnvCredential | None = None,
) -> AccountConfig:
    return AccountConfig(
        name=name,
        url="https://caldav.example.com/dav/",
        username="user@example.com",
        credential=credential or PlaintextCredential(password="s3cret"),
        mirror_path=Path("/unused"),
        trash_retention_days=30,
        scope=SyncScope(),
    )


def _config(*accounts: AccountConfig) -> AppConfig:
    return AppConfig(
        config_version=1,
        use_utf8=False,
        editor=None,
        accounts=tuple(accounts) or (_account(),),
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    """Parse *argv* with the program's own parser.

    One command line serves both halves, so a calendar command is
    parsed by the parser `pony --help` describes -- not by one these
    tests build. The calendar used to have a parser of its own and this
    is what called it.
    """
    from pony.cli.program import build_parser

    return build_parser().parse_args(["calendar", *argv])


def _nowhere_document(path: Path) -> ConfigDocument:
    """A document for the commands that are given their stores instead."""
    return ConfigDocument(
        path=path,
        label=str(path),
        load=lambda: _config(),
        save=lambda _config: None,
        validate=lambda _path: None,
    )


def _no_context() -> cli.CliContext:
    """For the commands that edit a configuration and open no store."""
    raise AssertionError("this command should not have opened the stores")


def _unconfigured() -> cli.CliContext:
    """No `[calendar]` table yet, which is what the bootstrap offers to fix."""
    from pony.calendar_runtime import CalendarNotConfiguredError

    raise CalendarNotConfiguredError("no calendar configured")


class CalendarRootMixin:
    """Supplies ``self.calendar_paths`` from the autouse fixture.

    A `unittest.TestCase` method cannot be handed a fixture, so
    `tests/conftest.py` puts the per-test calendar root on the class.
    The annotation is what says so to a reader and to the type checker.
    """

    calendar_paths: CalendarPaths


class CliTestCase(CalendarRootMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.session = FakeCalDAVSession()

    def _ctx(
        self,
        *,
        config: AppConfig | None = None,
        session_factory: cli.SessionFactory | None = None,
        creds_env: dict[str, str] | None = None,
        paths: CalendarPaths | None = None,
    ) -> cli.CliContext:
        return cli.CliContext(
            config=config or _config(),
            mirror=self.mirror,
            index=self.index,
            creds=DefaultCredentialsProvider(
                env=creds_env or {}, paths=paths or self.calendar_paths
            ),
            paths=paths or self.calendar_paths,
            stdout=self.stdout,
            stderr=self.stderr,
            now=NOW,
            session_factory=session_factory,
        )

    def _run(
        self,
        argv: list[str],
        *,
        context: cli.CliContext | None = None,
    ) -> int:
        ctx = context or self._ctx()
        return cli.run(
            _parse(argv),
            paths=self.calendar_paths,
            document=_nowhere_document(self.calendar_paths.config_file),
            context_factory=lambda: ctx,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class ParserTest(CliTestCase):
    """The calendar's commands are in the program's one parser."""

    def test_a_bare_calendar_names_no_command(self) -> None:
        """`pony calendar` by itself: nothing to run, so the help.

        It used to default to `tui` and open the calendar's own
        application, which no longer exists -- the agenda is a screen
        of the one app.
        """
        self.assertIsNone(_parse([]).calendar_command)

    def test_one_help_covers_both_halves(self) -> None:
        """What the user asked the command line for: one help.

        The mail commands and `calendar` in the same listing, and the
        calendar's own commands under it. They used to be two parsers,
        so `pony calendar --help` was another program's help and
        nothing listed both.
        """
        from pony.cli.program import build_parser

        top = build_parser().format_help()
        self.assertIn("calendar", top)
        self.assertIn("compose", top)

        calendar = io.StringIO()
        with contextlib.redirect_stdout(calendar), self.assertRaises(SystemExit):
            _parse(["--help"])
        listed = calendar.getvalue()
        for command in ("sync", "list", "show", "add", "edit", "rm", "doctor"):
            with self.subTest(command=command):
                self.assertIn(command, listed)
        # And no application of its own to launch.
        self.assertNotIn("tui", listed)

    def test_unknown_subcommand_exits_nonzero(self) -> None:
        with self.assertRaises(SystemExit):
            _parse(["nope"])


class SyncCommandTest(CliTestCase):
    def _seed_server(self) -> None:
        self.session.add_calendar(url="https://cal.example.com/work/", name="work")
        self.session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

    def test_sync_happy_path(self) -> None:
        self._seed_server()
        from pony.calendar.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("personal:", self.stdout.getvalue())
        self.assertIn("+1", self.stdout.getvalue())

    def test_reset_deletes_index_and_mirror_with_yes_flag(self) -> None:
        # Seed the mirror + index so there's something to delete.
        self._seed_server()
        from pony.calendar.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        # First sync populates everything.
        self.assertEqual(self._run(["sync"], context=ctx), 0)
        # Sanity: data is on disk.
        self.assertTrue(self.index._path.exists())  # type: ignore[attr-defined]

        index_path = self.index._path  # type: ignore[attr-defined]
        # `VdirMirrorRepository` stores its root on `_root` (the
        # constructor's `tmp / "mirror"`).
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]

        code = cli.cmd_reset(
            ctx, yes=True, index_path=index_path, mirror_dir=mirror_dir
        )
        self.assertEqual(code, 0)
        self.assertFalse(index_path.exists())
        self.assertFalse(mirror_dir.exists())
        self.assertIn("Reset complete", self.stdout.getvalue())

    def test_reset_is_a_noop_when_nothing_exists(self) -> None:
        # No prior sync — index file doesn't exist on disk, mirror
        # dir doesn't exist either. Reset should print a friendly
        # "nothing to do" line and return 0.
        ctx = self._ctx()
        # Close + unlink so the file genuinely isn't there.
        ctx.index.close()
        index_path = self.index._path  # type: ignore[attr-defined]
        if index_path.exists():
            index_path.unlink()
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]
        if mirror_dir.exists():
            import shutil as _sh

            _sh.rmtree(mirror_dir)

        code = cli.cmd_reset(
            ctx, yes=True, index_path=index_path, mirror_dir=mirror_dir
        )
        self.assertEqual(code, 0)
        self.assertIn("Nothing to reset", self.stdout.getvalue())

    def test_the_host_says_where_a_running_application_publishes_itself(self) -> None:
        """`pony calendar reset` asks the program's endpoint, not its own.

        The guard used to read the calendar's own endpoint file, which
        nothing has written since the calendar stopped being a program
        of its own: a running application holding this index went
        undetected and the index was deleted under it.
        """
        import socket

        from pony.mcp.state import Endpoint, publish

        ctx = self._ctx()
        index_file = self.calendar_paths.index_file
        index_file.parent.mkdir(parents=True, exist_ok=True)
        index_file.touch()

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            endpoint_file = (
                Path(self.enterContext(tempfile.TemporaryDirectory())) / "mcp.json"
            )
            publish(endpoint_file, Endpoint(port=port, token="tok"))

            code = cli.run(
                _parse(["reset", "--yes"]),
                paths=self.calendar_paths,
                document=_nowhere_document(self.calendar_paths.config_file),
                context_factory=lambda: ctx,
                mcp_endpoint_file=endpoint_file,
                stdout=self.stdout,
                stderr=self.stderr,
            )
        finally:
            srv.close()

        self.assertEqual(code, 2)
        self.assertIn("Pony Express is running", self.stderr.getvalue())
        self.assertIn(str(port), self.stderr.getvalue())
        self.assertTrue(index_file.exists())

    def test_reset_refuses_when_mcp_server_reachable(self) -> None:
        import socket

        from pony.mcp.state import Endpoint, publish

        self._seed_server()

        def factory(_account: AccountConfig, _auth: Any) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self.assertEqual(self._run(["sync"], context=ctx), 0)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            state_file = (
                Path(self.enterContext(tempfile.TemporaryDirectory())) / "mcp.json"
            )
            publish(state_file, Endpoint(port=port, token="tok"))
            index_path = self.index._path  # type: ignore[attr-defined]
            mirror_dir = self.mirror._root  # type: ignore[attr-defined]
            code = cli.cmd_reset(
                ctx,
                yes=True,
                index_path=index_path,
                mirror_dir=mirror_dir,
                endpoint_file=state_file,
            )
        finally:
            srv.close()

        self.assertEqual(code, 2)
        self.assertIn("Pony Express is running", self.stderr.getvalue())
        self.assertIn(str(port), self.stderr.getvalue())
        self.assertTrue(index_path.exists())

    def test_reset_refuses_when_sync_lock_held(self) -> None:
        from pony.calendar.locking import SyncLockError

        self._seed_server()

        def factory(_account: AccountConfig, _auth: Any) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self.assertEqual(self._run(["sync"], context=ctx), 0)

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        index_path = self.index._path  # type: ignore[attr-defined]
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]

        @contextlib.contextmanager
        def _contended(_path: Path):  # type: ignore[return]
            raise SyncLockError("another calendar sync is already running (pid=1234)")
            yield  # the bare yield is what makes this a generator

        with mock.patch("pony.cli.calendar.acquire_sync_lock", new=_contended):
            code = cli.cmd_reset(
                ctx,
                yes=True,
                index_path=index_path,
                mirror_dir=mirror_dir,
                endpoint_file=tmp / "mcp.json",  # nothing published → nothing running
            )

        self.assertEqual(code, 2)
        self.assertIn("refusing to reset", self.stderr.getvalue())
        self.assertIn("1234", self.stderr.getvalue())
        self.assertTrue(index_path.exists())

    def test_reset_force_bypasses_presence_checks(self) -> None:
        import socket

        from pony.mcp.state import Endpoint, publish

        self._seed_server()

        def factory(_account: AccountConfig, _auth: Any) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self.assertEqual(self._run(["sync"], context=ctx), 0)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            state_file = (
                Path(self.enterContext(tempfile.TemporaryDirectory())) / "mcp.json"
            )
            publish(state_file, Endpoint(port=port, token="tok"))
            index_path = self.index._path  # type: ignore[attr-defined]
            mirror_dir = self.mirror._root  # type: ignore[attr-defined]
            code = cli.cmd_reset(
                ctx,
                yes=True,
                force=True,
                index_path=index_path,
                mirror_dir=mirror_dir,
                endpoint_file=state_file,
            )
        finally:
            srv.close()

        self.assertEqual(code, 0)
        self.assertFalse(index_path.exists())
        self.assertIn("Reset complete", self.stdout.getvalue())

    def test_reset_proceeds_when_state_file_stale(self) -> None:
        import socket

        from pony.mcp.state import Endpoint, publish

        self._seed_server()

        def factory(_account: AccountConfig, _auth: Any) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self.assertEqual(self._run(["sync"], context=ctx), 0)

        # Bind, capture port, then immediately close so nothing is listening.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state_file = tmp / "mcp.json"
        publish(state_file, Endpoint(port=port, token="tok"))

        index_path = self.index._path  # type: ignore[attr-defined]
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]
        code = cli.cmd_reset(
            ctx,
            yes=True,
            index_path=index_path,
            mirror_dir=mirror_dir,
            endpoint_file=state_file,
            lock_path=tmp / "sync.lock",
        )

        self.assertEqual(code, 0)
        self.assertFalse(index_path.exists())
        self.assertFalse(state_file.exists())

    def test_reset_refuses_without_yes_when_non_interactive(self) -> None:
        self._seed_server()
        from pony.calendar.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self._run(["sync"], context=ctx)
        index_path = self.index._path  # type: ignore[attr-defined]
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]

        # Tests run with stdin/stdout not attached to a TTY, so the
        # confirmation path takes the "refuse non-interactively"
        # branch.
        code = cli.cmd_reset(
            ctx, yes=False, index_path=index_path, mirror_dir=mirror_dir
        )
        self.assertEqual(code, 1)
        self.assertIn("Refusing to reset non-interactively", self.stderr.getvalue())
        # Files are still there.
        self.assertTrue(index_path.exists())
        self.assertTrue(mirror_dir.exists())

    def test_sync_force_clears_sync_state_and_reruns_slow_path(self) -> None:
        # `pony calendar sync --force` is the user-facing escape hatch when
        # the local cache has drifted out of step with the components
        # table. It clears every per-calendar CTag so the next run
        # re-enters the slow path for every calendar (which, on its
        # tail, calls `populate_occurrences` to rebuild the cache).
        self._seed_server()
        from pony.calendar.authorization import Authorization
        from pony.calendar.domain import CalendarRef, SyncState

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        # Pre-seed a sync-state row so we can prove --force wipes it.
        ctx.index.set_sync_state(
            SyncState(
                calendar=CalendarRef(account_name="personal", calendar_name="work"),
                ctag="ctag-stale",
                sync_token=None,
                synced_at=ctx.now,
            )
        )

        code = self._run(["sync", "--force"], context=ctx)
        self.assertEqual(code, 0)
        # The pre-seeded row was wiped, then the actual sync wrote a
        # fresh state with the server's current CTag.
        new_state = ctx.index.get_sync_state(
            CalendarRef(account_name="personal", calendar_name="work")
        )
        assert new_state is not None
        self.assertNotEqual(new_state.ctag, "ctag-stale")
        # Confirmation line on stdout so the user sees the wipe.
        self.assertIn("--force", self.stdout.getvalue())
        self.assertIn("cleared sync state", self.stdout.getvalue())

    def test_sync_account_filters_to_named_account(self) -> None:
        # `pony calendar sync --account NAME` should sync only that account,
        # leaving the others untouched — the isolation knob for debugging
        # one flaky account without churning the rest.
        self._seed_server()
        from pony.calendar.authorization import Authorization

        synced: list[str] = []

        def factory(account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            synced.append(account.name)
            return self.session

        ctx = self._ctx(
            config=_config(_account(name="personal"), _account(name="work")),
            session_factory=factory,
        )
        code = self._run(["sync", "--account", "work"], context=ctx)
        self.assertEqual(code, 0)
        self.assertEqual(synced, ["work"])
        self.assertIn("work:", self.stdout.getvalue())
        self.assertNotIn("personal:", self.stdout.getvalue())

    def test_sync_account_unknown_name_errors(self) -> None:
        ctx = self._ctx(
            config=_config(_account(name="personal"), _account(name="work"))
        )
        code = self._run(["sync", "--account", "nope"], context=ctx)
        self.assertEqual(code, 2)
        err = self.stderr.getvalue()
        self.assertIn("no account named 'nope'", err)
        # Lists the configured names so the user can correct the typo.
        self.assertIn("personal", err)
        self.assertIn("work", err)

    def test_sync_account_force_clears_only_that_account(self) -> None:
        # `--account NAME --force` must scope the slow-path reset to that
        # account; sibling accounts keep their CTags so they don't all
        # re-download just because one was being debugged.
        self._seed_server()
        from pony.calendar.authorization import Authorization
        from pony.calendar.domain import CalendarRef, SyncState

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(
            config=_config(_account(name="personal"), _account(name="work")),
            session_factory=factory,
        )
        for acct in ("personal", "work"):
            ctx.index.set_sync_state(
                SyncState(
                    calendar=CalendarRef(account_name=acct, calendar_name="work"),
                    ctag="ctag-stale",
                    sync_token=None,
                    synced_at=ctx.now,
                )
            )

        code = self._run(["sync", "--account", "personal", "--force"], context=ctx)
        self.assertEqual(code, 0)
        # The other account's stale CTag survives untouched.
        other = ctx.index.get_sync_state(
            CalendarRef(account_name="work", calendar_name="work")
        )
        assert other is not None
        self.assertEqual(other.ctag, "ctag-stale")
        self.assertIn("account 'personal'", self.stdout.getvalue())

    def test_sync_reports_credential_failure(self) -> None:
        bad_config = _config(
            _account(credential=EnvCredential(variable="UNSET_FOR_TEST"))
        )
        ctx = self._ctx(config=bad_config, creds_env={})
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("UNSET_FOR_TEST", self.stderr.getvalue())

    def test_sync_without_factory_builds_caldav_http_session(self) -> None:
        from pony.calendar.authorization import Authorization
        from pony.calendar.caldav import CalDAVHttpSession

        session = default_session_factory(
            _account(), Authorization(basic=("user@example.com", "pw"))
        )
        self.assertIsInstance(session, CalDAVHttpSession)

    def test_sync_reports_caldav_error_from_session(self) -> None:
        from pony.calendar.authorization import Authorization
        from pony.calendar.caldav import CalDAVError

        def broken_factory(
            _account: AccountConfig, _auth: Authorization
        ) -> FakeCalDAVSession:
            session = FakeCalDAVSession()

            def _raise_principal() -> str:
                raise CalDAVError("simulated network failure")

            session.discover_principal = _raise_principal  # type: ignore[method-assign]
            return session

        ctx = self._ctx(session_factory=broken_factory)
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("CalDAV error", self.stderr.getvalue())
        self.assertIn("simulated network failure", self.stderr.getvalue())

    def test_sync_catches_caldav_error_from_session_factory(self) -> None:
        """A CalDAVError raised by the session factory itself (e.g. Google
        email-discovery failing in the default factory) must be reported
        per-account, not crash the whole sync."""
        from pony.calendar.authorization import Authorization
        from pony.calendar.caldav import CalDAVError

        def broken_factory(
            _account: AccountConfig, _auth: Authorization
        ) -> FakeCalDAVSession:
            raise CalDAVError("session construction blew up")

        ctx = self._ctx(session_factory=broken_factory)
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("CalDAV error", self.stderr.getvalue())
        self.assertIn("session construction blew up", self.stderr.getvalue())


class SyncLockReleaseTest(CliTestCase):
    """The TUI runs sync synchronously on the UI thread. Any exception
    raised during the sync (including a Ctrl-C that bubbles up as
    KeyboardInterrupt) must release the sync lockfile so the next
    `pony calendar sync` invocation isn't blocked. The persistence layers
    (mirror temp-files, SQLite transactions, OAuth-token writes) are
    each tested for atomicity in their own modules; this test pins
    the additional lockfile-release guarantee at the cli boundary.
    """

    def _seed(self) -> None:
        self.session.add_calendar(url="https://cal.example.com/work/", name="work")
        self.session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

    def test_concurrent_cmd_sync_invocations_rejected(self) -> None:
        # Hold the lockfile via a separate `acquire_sync_lock` and
        # verify `cmd_sync` exits non-zero with a "another calendar
        # sync is already running" message.
        from pony.calendar.locking import acquire_sync_lock

        self._seed()
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        under_tmp = CalendarPaths(root=tmp)
        ctx = self._ctx(session_factory=lambda *_: self.session, paths=under_tmp)

        with acquire_sync_lock(under_tmp.lock_file):
            exit_code = cli.cmd_sync(ctx)

        self.assertEqual(exit_code, 2)
        self.assertIn(
            "another calendar sync is already running", self.stderr.getvalue()
        )


class ListCommandTest(CliTestCase):
    def _seed_index(self, uid: str, *, summary: str = "Meeting") -> None:
        ref = ComponentRef("personal", "work", uid)
        event = VEvent(
            ref=ref,
            href="/dav/x.ics",
            etag="e",
            raw_ics=corpus.simple_event(),
            summary=summary,
            description=None,
            location=None,
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        self.mirror.write(ref.resource, corpus.simple_event())
        self.index.upsert_component(event)

    def test_list_shows_seeded_events(self) -> None:
        self._seed_index("a@example.com", summary="Alpha")
        self._seed_index("b@example.com", summary="Beta")
        code = self._run(["list"])
        self.assertEqual(code, 0)
        output = self.stdout.getvalue()
        self.assertIn("Alpha", output)
        self.assertIn("Beta", output)
        self.assertIn("EVENT", output)

    def test_list_respects_calendar_filter(self) -> None:
        self._seed_index("a@example.com")
        code = self._run(["list", "--calendar", "nonexistent"])
        self.assertEqual(code, 0)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_list_hides_trashed(self) -> None:
        self._seed_index("a@example.com", summary="Alpha")
        current = self.index.get_component(
            ComponentRef("personal", "work", "a@example.com")
        )
        assert current is not None
        assert isinstance(current, VEvent)
        trashed = VEvent(
            ref=current.ref,
            href=current.href,
            etag=current.etag,
            raw_ics=current.raw_ics,
            summary=current.summary,
            description=current.description,
            location=current.location,
            dtstart=current.dtstart,
            dtend=current.dtend,
            status=current.status,
            local_flags=current.local_flags,
            server_flags=current.server_flags,
            local_status=LocalStatus.TRASHED,
            trashed_at=NOW,
            synced_at=current.synced_at,
        )
        self.index.upsert_component(trashed)
        code = self._run(["list"])
        self.assertEqual(code, 0)
        self.assertNotIn("Alpha", self.stdout.getvalue())


class ShowCommandTest(CliTestCase):
    def test_not_found_returns_one(self) -> None:
        code = self._run(["show", "nope@example.com"])
        self.assertEqual(code, 1)
        self.assertIn("not found", self.stderr.getvalue())

    def test_renders_details(self) -> None:
        ref = ResourceRef("personal", "work", "ev@example.com")
        self.mirror.write(ref, corpus.simple_event())
        self.index.upsert_component(
            VEvent(
                ref=ComponentRef("personal", "work", "ev@example.com"),
                href="/dav/ev.ics",
                etag="etag-1",
                raw_ics=corpus.simple_event(),
                summary="Staff review",
                description="With pastries",
                location="Conference room",
                dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
                status=None,
                local_flags=frozenset(),
                server_flags=frozenset(),
                local_status=LocalStatus.ACTIVE,
                trashed_at=None,
                synced_at=None,
            )
        )
        code = self._run(["show", "ev@example.com"])
        self.assertEqual(code, 0)
        output = self.stdout.getvalue()
        self.assertIn("Staff review", output)
        self.assertIn("With pastries", output)
        self.assertIn("Conference room", output)
        self.assertIn("ev@example.com", output)


class AddCommandTest(CliTestCase):
    def test_add_writes_mirror_and_index_with_href_null(self) -> None:
        code = self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Lunch",
                "--start",
                "2026-05-01T12:00:00+00:00",
                "--end",
                "2026-05-01T13:00:00+00:00",
                "--uid",
                "lunch@example.com",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("lunch@example.com", self.stdout.getvalue())
        # Mirror populated.
        self.assertTrue(
            self.mirror.exists(ResourceRef("personal", "work", "lunch@example.com"))
        )
        # Index row pending (href IS NULL).
        component = self.index.get_component(
            ComponentRef("personal", "work", "lunch@example.com")
        )
        assert component is not None
        self.assertIsNone(component.href)
        self.assertEqual(component.summary, "Lunch")

    def test_an_added_event_is_visible_in_the_views(self) -> None:
        """It was not: the occurrence cache was never rebuilt.

        Every view -- agenda, day, grid, month -- joins `components`
        against `occurrences`, and so does the alarm pass. `add` wrote
        the row directly, and `upsert_component` *clears* the cached
        occurrences for a master, so the event the command reported
        creating could not be seen anywhere until the next sync put the
        cache back.
        """
        from pony.calendar.domain import CalendarRef
        from pony.calendar.views import CalendarSelection, gather_occurrences

        code = self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Visible",
                "--start",
                "2026-05-01T12:00:00+00:00",
                "--end",
                "2026-05-01T13:00:00+00:00",
            ]
        )

        self.assertEqual(code, 0)
        rows = gather_occurrences(
            index=self.index,
            calendars=(CalendarRef("personal", "work"),),
            selection=CalendarSelection(refs=frozenset()),
            window=(
                datetime(2026, 5, 1, tzinfo=UTC),
                datetime(2026, 5, 2, tzinfo=UTC),
            ),
        )
        self.assertEqual(["Visible"], [row.component.summary for row in rows])

    def test_an_edit_keeps_the_reminders_it_was_not_asked_about(self) -> None:
        """Rebuilding the text from fields dropped them silently."""
        from pony.calendar.ical_parser import extract_alarm_triggers

        self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Standup",
                "--start",
                "2026-05-01T09:00:00+00:00",
                "--uid",
                "standup@example.com",
            ]
        )
        ref = ComponentRef("personal", "work", "standup@example.com")
        stored = self.index.get_component(ref)
        assert stored is not None
        with_alarm = stored.raw_ics.replace(
            b"END:VEVENT",
            b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\n"
            b"END:VALARM\r\nEND:VEVENT",
        )
        self.index.upsert_component(replace(stored, raw_ics=with_alarm))
        self.mirror.write(ref.resource, with_alarm)

        code = self._run(["edit", "standup@example.com", "--summary", "Sync"])

        self.assertEqual(code, 0)
        edited = self.index.get_component(ref)
        assert edited is not None
        self.assertEqual("Sync", edited.summary)
        self.assertEqual(
            1,
            len(extract_alarm_triggers(edited.raw_ics, ref.uid)),
            edited.raw_ics,
        )

    def test_add_with_attendees_writes_invites(self) -> None:
        from pony.calendar.ical_parser import extract_attendees

        code = self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Planning",
                "--start",
                "2026-05-01T12:00:00+00:00",
                "--uid",
                "planning@example.com",
                "--attendee",
                "alice@example.com",
                "--attendee",
                "Bob <bob@example.com>",
            ]
        )
        self.assertEqual(code, 0)
        component = self.index.get_component(
            ComponentRef("personal", "work", "planning@example.com")
        )
        assert isinstance(component, VEvent)
        self.assertEqual(
            extract_attendees(component.raw_ics, component.ref.uid),
            ("alice@example.com", "bob@example.com"),
        )

    def test_add_rejects_unknown_account(self) -> None:
        code = self._run(
            [
                "add",
                "--account",
                "nope",
                "--calendar",
                "work",
                "--summary",
                "X",
                "--start",
                "2026-05-01T12:00:00+00:00",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown account", self.stderr.getvalue())

    def test_add_without_uid_generates_stable_uid(self) -> None:
        code = self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Auto",
                "--start",
                "2026-05-01T12:00:00+00:00",
            ]
        )
        self.assertEqual(code, 0)
        uid = self.stdout.getvalue().strip()
        self.assertTrue(uid.endswith("@chronos"))
        self.assertTrue(self.mirror.exists(ResourceRef("personal", "work", uid)))


class EditCommandTest(CliTestCase):
    def test_edit_updates_summary(self) -> None:
        self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Old",
                "--start",
                "2026-05-01T12:00:00+00:00",
                "--uid",
                "ed@example.com",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["edit", "ed@example.com", "--summary", "New"])
        self.assertEqual(code, 0)
        component = self.index.get_component(
            ComponentRef("personal", "work", "ed@example.com")
        )
        assert component is not None
        self.assertEqual(component.summary, "New")

    def test_edit_of_synced_event_is_pushed_on_next_sync(self) -> None:
        from pony.calendar.authorization import Authorization

        cal_url = "https://cal.example.com/work/"
        href = "https://cal.example.com/work/a.ics"
        self.session.add_calendar(url=cal_url, name="work")
        self.session.put_resource(
            calendar_url=cal_url, href=href, ics=corpus.simple_event(), etag="e1"
        )

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self.assertEqual(self._run(["sync"], context=ctx), 0)
        code = self._run(
            [
                "edit",
                "simple-event-1@example.com",
                "--start",
                "2026-06-01T09:00:00+00:00",
            ],
            context=ctx,
        )
        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(self._run(["sync"], context=ctx), 0)

        self.assertNotEqual(self.session.etag_for(href), "e1")
        remote = self.session.calendar_multiget(cal_url, [href])[0][2]
        self.assertIn(b"DTSTART:20260601T090000Z", remote)
        component = self.index.get_component(
            ComponentRef("personal", "work", "simple-event-1@example.com")
        )
        assert component is not None
        self.assertEqual(component.local_flags, frozenset())

    def test_edit_keeps_all_day_events_as_dates(self) -> None:
        from pony.calendar.mutations import all_day_bounds, build_event_ics

        start, end = all_day_bounds(date(2026, 5, 1), date(2026, 5, 1))
        ics = build_event_ics(
            "day@example.com", "Holiday", start, end, NOW, all_day=True
        )
        self.mirror.write(ResourceRef("personal", "work", "day@example.com"), ics)
        self.index.upsert_component(
            VEvent(
                ref=ComponentRef("personal", "work", "day@example.com"),
                href=None,
                etag=None,
                raw_ics=ics,
                summary="Holiday",
                description=None,
                location=None,
                dtstart=start,
                dtend=end,
                status=None,
                local_flags=frozenset(),
                server_flags=frozenset(),
                local_status=LocalStatus.ACTIVE,
                trashed_at=None,
                synced_at=None,
            )
        )
        code = self._run(["edit", "day@example.com", "--summary", "Day off"])
        self.assertEqual(code, 0, self.stderr.getvalue())
        component = self.index.get_component(
            ComponentRef("personal", "work", "day@example.com")
        )
        assert component is not None
        self.assertIn(b"DTSTART;VALUE=DATE:20260501", component.raw_ics)

    def test_edit_keeps_an_outlook_all_day_event_whole(self) -> None:
        """An all-day event written at local midnight stays all-day.

        Outlook, Apple Calendar and Thunderbird store an all-day event
        as ``DTSTART;TZID=...:...T000000``, which lands off UTC
        midnight.  The grid draws it as a banner; `edit` used to decide
        it was not an all-day span and rewrite it as a timed event, so
        changing its summary turned a holiday into a 24-hour
        appointment.  The reader's zone is pinned because in UTC the two
        readings are the same.
        """
        zone = timezone(timedelta(hours=2))
        start = datetime(2026, 5, 1, 0, 0, tzinfo=zone)
        ics = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:outlook-day@example.com\r\n"
            b"DTSTAMP:20260422T120000Z\r\n"
            b"DTSTART;TZID=Europe/Madrid:20260501T000000\r\n"
            b"DTEND;TZID=Europe/Madrid:20260502T000000\r\n"
            b"SUMMARY:Holiday\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        self.mirror.write(
            ResourceRef("personal", "work", "outlook-day@example.com"), ics
        )
        self.index.upsert_component(
            VEvent(
                ref=ComponentRef("personal", "work", "outlook-day@example.com"),
                href=None,
                etag=None,
                raw_ics=ics,
                summary="Holiday",
                description=None,
                location=None,
                dtstart=start,
                dtend=start + timedelta(days=1),
                status=None,
                local_flags=frozenset(),
                server_flags=frozenset(),
                local_status=LocalStatus.ACTIVE,
                trashed_at=None,
                synced_at=None,
            )
        )
        with mock.patch("pony.calendar.ical_parser.local_tzinfo", return_value=zone):
            code = self._run(
                ["edit", "outlook-day@example.com", "--summary", "Day off"]
            )
        self.assertEqual(code, 0, self.stderr.getvalue())
        component = self.index.get_component(
            ComponentRef("personal", "work", "outlook-day@example.com")
        )
        assert component is not None
        self.assertIn(b"DTSTART;VALUE=DATE:20260501", component.raw_ics)
        self.assertIn(b"DTEND;VALUE=DATE:20260502", component.raw_ics)

    def test_edit_not_found(self) -> None:
        code = self._run(["edit", "missing@example.com", "--summary", "X"])
        self.assertEqual(code, 1)
        self.assertIn("not found", self.stderr.getvalue())


class RmCommandTest(CliTestCase):
    def test_rm_marks_trashed(self) -> None:
        self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Gone",
                "--start",
                "2026-05-01T12:00:00+00:00",
                "--uid",
                "gone@example.com",
            ]
        )
        code = self._run(["rm", "gone@example.com"])
        self.assertEqual(code, 0)
        self.assertIn("trashed 1", self.stdout.getvalue())
        component = self.index.get_component(
            ComponentRef("personal", "work", "gone@example.com")
        )
        assert component is not None
        self.assertEqual(component.local_status, LocalStatus.TRASHED)
        self.assertEqual(component.trashed_at, NOW)

    def test_rm_not_found(self) -> None:
        code = self._run(["rm", "missing@example.com"])
        self.assertEqual(code, 1)

    def test_rm_refuses_a_uid_two_calendars_hold(self) -> None:
        """It trashed both, where `trash_event` takes one component.

        The same UID is in two calendars whenever an invitation is filed
        twice or a copy is made by hand, and `rm <uid>` does not say
        which. `show` and `edit` refuse that and name the calendars;
        this used to delete the lot.
        """
        for calendar in ("work", "home"):
            self._run(
                [
                    "add",
                    "--account",
                    "personal",
                    "--calendar",
                    calendar,
                    "--summary",
                    "Twice",
                    "--start",
                    "2026-05-01T12:00:00+00:00",
                    "--uid",
                    "twice@example.com",
                ]
            )

        code = self._run(["rm", "twice@example.com"])

        self.assertEqual(code, 2)
        self.assertIn("ambiguous uid", self.stderr.getvalue())
        self.assertIn("personal/work", self.stderr.getvalue())
        self.assertIn("personal/home", self.stderr.getvalue())
        for calendar in ("work", "home"):
            component = self.index.get_component(
                ComponentRef("personal", calendar, "twice@example.com"),
            )
            assert component is not None
            self.assertEqual(component.local_status, LocalStatus.ACTIVE)


class DoctorCommandTest(CliTestCase):
    def test_doctor_reports_ok_on_clean_state(self) -> None:
        code = self._run(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn("credentials", self.stdout.getvalue())
        self.assertIn("OK", self.stdout.getvalue())

    def test_doctor_surfaces_unparseable_mirror(self) -> None:
        self.mirror.write(
            ResourceRef("personal", "work", "junk@example.com"),
            b"totally not ical",
        )
        code = self._run(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", self.stdout.getvalue())
        self.assertIn("unparseable", self.stdout.getvalue())

    def test_doctor_remote_runs_redacted_caldav_probe(self) -> None:
        self.session.add_calendar(url="https://cal.example.com/work/", name="work")
        self.session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

        from pony.calendar.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        code = self._run(["doctor", "--remote"], context=ctx)

        self.assertEqual(code, 0)
        output = self.stdout.getvalue()
        self.assertIn("remote-caldav", output)
        self.assertIn("remote-calendar-query", output)
        self.assertIn("1 resource", output)

    def test_doctor_remote_debug_streams_step_progress_to_stderr(self) -> None:
        self.session.add_calendar(url="https://cal.example.com/work/", name="work")
        self.session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

        from pony.calendar.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        code = self._run(["doctor", "--remote", "--debug"], context=ctx)

        self.assertEqual(code, 0)
        progress = self.stderr.getvalue()
        # Each blocking step is announced on stderr before it runs, so a
        # stalled probe is attributable to the last line printed.
        self.assertIn("[doctor", progress)
        self.assertIn("discovering principal", progress)
        self.assertIn("listing calendars", progress)
        self.assertIn("calendar-query", progress)
        self.assertIn("multiget sample", progress)
        self.assertIn("remote probe complete", progress)
        # The report itself still goes to stdout.
        self.assertIn("remote-calendar-query", self.stdout.getvalue())

    def test_doctor_without_debug_emits_no_step_progress(self) -> None:
        from pony.calendar.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self._run(["doctor", "--remote"], context=ctx)

        self.assertNotIn("[doctor", self.stderr.getvalue())


class ConfigLoadErrorTest(CliTestCase):
    def test_an_unconfigured_calendar_in_non_interactive_exits_two(self) -> None:
        """What `pony calendar doctor` says with no `[calendar]` table."""
        from pony.calendar_runtime import CalendarNotConfiguredError

        def unconfigured() -> cli.CliContext:
            raise CalendarNotConfiguredError("no calendar configured")

        code = cli.run(
            _parse(["doctor"]),
            paths=self.calendar_paths,
            document=_nowhere_document(self.calendar_paths.config_file),
            context_factory=unconfigured,
            is_interactive=lambda: False,
            stdout=self.stdout,
            stderr=self.stderr,
        )

        self.assertEqual(code, 2)
        self.assertIn("no calendar configured", self.stderr.getvalue())
        self.assertIn("pony calendar init", self.stderr.getvalue())


_HOST_CONFIG = """\
config_version = 3
data_dir = "{data_dir}"
profile = "calendar-cli-tests"
accounts = []
"""


class ConfigEditingCliTestCase(CalendarRootMixin, unittest.TestCase):
    """Harness for the config-editing subcommands.

    These commands don't need a mirror/index/session -- just a config
    path and captured streams.  The path holds Pony's own configuration,
    because the document they edit is the `[calendar]` table of it:
    there is no standalone calendar file, and the tests used to be given
    one, which is how a branch nothing could reach kept its coverage.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config_path = self.tmp / "config.toml"
        self.config_path.write_text(self._host_config(), encoding="utf-8")
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def _host_config(self) -> str:
        """Pony's own configuration, which the `[calendar]` table lives in."""
        return _HOST_CONFIG.format(data_dir=(self.tmp / "data").as_posix())

    def _document(self) -> ConfigDocument:
        from pony.calendar_runtime import hosted_calendar_document

        return hosted_calendar_document(self.config_path)

    def _run(
        self,
        argv: list[str],
        *,
        open_editor: cli.EditorFn | None = None,
    ) -> int:
        return cli.run(
            _parse(argv),
            paths=self.calendar_paths,
            document=self._document(),
            context_factory=_no_context,
            open_editor=open_editor,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class InitCommandTest(ConfigEditingCliTestCase):
    def test_init_adds_the_table_to_the_hosts_file(self) -> None:
        code = self._run(["init"])

        self.assertEqual(code, 0)
        self.assertIn("Initialised", self.stdout.getvalue())
        config = self._document().load()
        self.assertEqual(config.config_version, 1)
        self.assertEqual(config.accounts, ())
        # The host's own keys survive: it is one file, and `init` writes
        # one table of it. `init` used to be able to write a commented
        # template *over* a file instead, which over Pony's own
        # `config.toml` could only ever refuse.
        body = self.config_path.read_text(encoding="utf-8")
        self.assertIn("profile = ", body)
        self.assertIn("[calendar]", body)

    def test_init_refuses_when_the_table_already_has_accounts(self) -> None:
        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://cal.example.com/",
                "--username",
                "u@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "s3cret",
            ]
        )
        self.stderr.truncate(0)
        self.stderr.seek(0)

        code = self._run(["init"])

        self.assertEqual(code, 1)
        self.assertIn("already has calendar accounts", self.stderr.getvalue())


class FirstLaunchBootstrapTest(ConfigEditingCliTestCase):
    """A data command with no `[calendar]` table offers to add one.

    In a non-TTY environment we exit with a helpful message instead.
    The file itself exists -- it is Pony's -- so what is missing is the
    table, which is what the offer writes.
    """

    def _scripted_prompt(self, answers: list[str]) -> Any:
        iterator = iter(answers)

        def prompt(_message: str) -> str:
            return next(iterator)

        return prompt

    def test_non_interactive_prints_help_and_exits_2(self) -> None:
        code = cli.run(
            _parse(["sync"]),
            paths=self.calendar_paths,
            document=self._document(),
            context_factory=_unconfigured,
            is_interactive=lambda: False,
            stdout=self.stdout,
            stderr=self.stderr,
        )
        self.assertEqual(code, 2)
        self.assertIn("no calendar configured", self.stderr.getvalue())
        self.assertIn("pony calendar init", self.stderr.getvalue())
        self.assertNotIn("[calendar]", self.config_path.read_text(encoding="utf-8"))

    def test_interactive_user_adds_the_table_and_skips_the_editor(self) -> None:
        edits: list[Path] = []

        code = cli.run(
            _parse(["sync"]),
            paths=self.calendar_paths,
            document=self._document(),
            context_factory=_unconfigured,
            is_interactive=lambda: True,
            prompt=self._scripted_prompt(["y", "n"]),  # create yes, edit no
            open_editor=lambda p: edits.append(p),
            stdout=self.stdout,
            stderr=self.stderr,
        )
        self.assertEqual(code, 0)
        self.assertIn("[calendar]", self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(edits, [])
        self.assertIn("Initialised", self.stdout.getvalue())

    def test_interactive_user_declines(self) -> None:
        code = cli.run(
            _parse(["list"]),
            paths=self.calendar_paths,
            document=self._document(),
            context_factory=_unconfigured,
            is_interactive=lambda: True,
            prompt=self._scripted_prompt(["n"]),
            stdout=self.stdout,
            stderr=self.stderr,
        )
        self.assertEqual(code, 1)
        self.assertNotIn("[calendar]", self.config_path.read_text(encoding="utf-8"))
        self.assertIn("Skipped", self.stdout.getvalue())

    def test_init_command_works_without_prompt_in_missing_config_path(self) -> None:
        # `pony calendar init` itself is a config-editing command, so the
        # missing-config bootstrap flow MUST NOT trigger for it — that
        # would prompt to create a template before letting init create
        # one, which is silly. Verify by passing no prompt at all.
        code = cli.run(
            _parse(["init"]),
            paths=self.calendar_paths,
            document=self._document(),
            context_factory=_unconfigured,
            stdout=self.stdout,
            stderr=self.stderr,
        )
        self.assertEqual(code, 0)
        self.assertIn("[calendar]", self.config_path.read_text(encoding="utf-8"))


class AccountAddCommandTest(ConfigEditingCliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)

    def test_add_plaintext_account(self) -> None:
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://caldav.example.com/dav/",
                "--username",
                "user@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "s3cret",
                "--mirror-path",
                "/tmp/pony/calendar/personal",
            ]
        )
        self.assertEqual(code, 0)
        config = self._document().load()
        self.assertEqual(len(config.accounts), 1)
        from pony.calendar.domain import PlaintextCredential

        cred = config.accounts[0].credential
        assert isinstance(cred, PlaintextCredential)
        self.assertEqual(cred.password, "s3cret")

    def test_add_account_without_mirror_path_uses_default(self) -> None:
        self._run(["init"])
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://caldav.example.com/dav/",
                "--username",
                "user@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "s3cret",
            ]
        )
        self.assertEqual(code, 0)
        config = self._document().load()
        # An account that declared nothing keeps nothing: the default
        # belongs to whichever root the calendar runs under, and
        # `mirror_roots` is where it is applied. Writing the resolved
        # default into the file -- which it used to do, reading a
        # global for it -- pinned the account to that run's root.
        self.assertIsNone(config.accounts[0].mirror_path)
        self.assertEqual(
            mirror_roots(config, self.calendar_paths)["personal"],
            self.calendar_paths.mirror_path("personal"),
        )
        # And the TOML on disk carries no explicit mirror_path line.
        body = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("mirror_path", body)

    def test_add_env_account(self) -> None:
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://caldav.example.com/dav/",
                "--username",
                "user@example.com",
                "--credential-backend",
                "env",
                "--credential-value",
                "CHRONOS_PW",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.assertEqual(code, 0)
        from pony.calendar.domain import EnvCredential

        config = self._document().load()
        cred = config.accounts[0].credential
        assert isinstance(cred, EnvCredential)
        self.assertEqual(cred.variable, "CHRONOS_PW")

    def test_add_command_account_splits_value(self) -> None:
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://x/",
                "--username",
                "u@example.com",
                "--credential-backend",
                "command",
                "--credential-value",
                "pass show chronos",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.assertEqual(code, 0)
        from pony.calendar.domain import CommandCredential

        config = self._document().load()
        cred = config.accounts[0].credential
        assert isinstance(cred, CommandCredential)
        self.assertEqual(cred.command, ("pass", "show", "chronos"))

    def test_add_rejects_duplicate_name(self) -> None:
        self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://x/",
                "--username",
                "u@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "p",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        self.stderr.truncate(0)
        self.stderr.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://x/",
                "--username",
                "u@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "p",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", self.stderr.getvalue())


class AccountListCommandTest(ConfigEditingCliTestCase):
    def test_list_no_accounts(self) -> None:
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["account", "list"])
        self.assertEqual(code, 0)
        self.assertIn("no accounts", self.stdout.getvalue())

    def test_list_shows_accounts_without_passwords(self) -> None:
        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://caldav.example.com/dav/",
                "--username",
                "user@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "SUPER_SECRET_DO_NOT_LEAK",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["account", "list"])
        self.assertEqual(code, 0)
        output = self.stdout.getvalue()
        self.assertIn("personal", output)
        self.assertIn("user@example.com", output)
        self.assertIn("backend=plaintext", output)
        self.assertNotIn("SUPER_SECRET_DO_NOT_LEAK", output)


class AccountRmCommandTest(ConfigEditingCliTestCase):
    def test_rm_existing_account(self) -> None:
        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                "gone",
                "--url",
                "https://x/",
                "--username",
                "u@example.com",
                "--credential-backend",
                "env",
                "--credential-value",
                "X",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["account", "rm", "gone"])
        self.assertEqual(code, 0)
        config = self._document().load()
        self.assertEqual(config.accounts, ())

    def test_rm_missing_account(self) -> None:
        self._run(["init"])
        code = self._run(["account", "rm", "never-existed"])
        self.assertEqual(code, 1)
        self.assertIn("not found", self.stderr.getvalue())


class AccountAddOAuthTest(ConfigEditingCliTestCase):
    def test_add_oauth_account(self) -> None:
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--url",
                "https://apidata.googleusercontent.com/caldav/v2/me@gmail.com/events/",
                "--username",
                "me@gmail.com",
                "--credential-backend",
                "oauth",
                "--client-id",
                "1234.apps.googleusercontent.com",
                "--client-secret",
                "GOCSPX-secret",
                "--mirror-path",
                "/tmp/pony/calendar/google",
            ]
        )
        self.assertEqual(code, 0)
        from pony.calendar.domain import OAuthCredential

        config = self._document().load()
        cred = config.accounts[0].credential
        assert isinstance(cred, OAuthCredential)
        self.assertEqual(cred.client_id, "1234.apps.googleusercontent.com")
        self.assertEqual(cred.client_secret, "GOCSPX-secret")

    def test_add_google_account_minimal(self) -> None:
        """`--credential-backend google` accepts only client_id+secret;
        url and username are filled in from Google's defaults."""
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--credential-backend",
                "google",
                "--client-id",
                "1234.apps.googleusercontent.com",
                "--client-secret",
                "GOCSPX-secret",
            ]
        )
        self.assertEqual(code, 0)
        from pony.calendar.domain import GOOGLE_CALDAV_URL, GoogleCredential

        config = self._document().load()
        account = config.accounts[0]
        cred = account.credential
        assert isinstance(cred, GoogleCredential)
        self.assertEqual(cred.client_id, "1234.apps.googleusercontent.com")
        self.assertEqual(cred.client_secret, "GOCSPX-secret")
        self.assertEqual(account.url, GOOGLE_CALDAV_URL)
        self.assertEqual(account.username, "")

    def test_google_missing_client_id_rejected(self) -> None:
        self._run(["init"])
        self.stderr.truncate(0)
        self.stderr.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--credential-backend",
                "google",
                "--client-secret",
                "secret-only",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--client-id", self.stderr.getvalue())

    def test_non_google_backend_still_requires_url(self) -> None:
        self._run(["init"])
        self.stderr.truncate(0)
        self.stderr.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--username",
                "u@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "pw",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--url", self.stderr.getvalue())

    def test_oauth_missing_client_id_rejected(self) -> None:
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        self.stderr.truncate(0)
        self.stderr.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--url",
                "https://x/",
                "--username",
                "me@example.com",
                "--credential-backend",
                "oauth",
                "--client-secret",
                "s",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--client-id", self.stderr.getvalue())


class OAuthAuthorizeCommandTest(ConfigEditingCliTestCase):
    def _add_oauth_account(self, name: str = "google") -> None:
        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                name,
                "--url",
                "https://x/",
                "--username",
                "me@example.com",
                "--credential-backend",
                "oauth",
                "--client-id",
                "cid",
                "--client-secret",
                "cs",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)

    def test_authorize_with_injected_flow_saves_tokens(self) -> None:
        from pony.calendar.domain import OAuthCredential
        from pony.calendar.oauth import StoredTokens

        self._add_oauth_account()
        token_path = self.tmp / "tokens.json"

        captured: dict[str, Any] = {}

        def fake_flow(credential: OAuthCredential, stdout: Any) -> StoredTokens:
            captured["credential"] = credential
            stdout.write("would have printed URL + code\n")
            return StoredTokens(
                access_token="at",
                refresh_token="rt",
                expiry_unix=12345.0,
                scope=credential.scope,
            )

        # Override the token store location by editing the saved config
        # to point at our tmp path.
        config = self._document().load()
        account = config.accounts[0]
        credential = account.credential
        from pony.calendar.domain import AccountConfig, AppConfig

        assert isinstance(credential, OAuthCredential)
        updated_account = AccountConfig(
            name=account.name,
            url=account.url,
            username=account.username,
            credential=OAuthCredential(
                client_id=credential.client_id,
                client_secret=credential.client_secret,
                scope=credential.scope,
                token_path=token_path,
            ),
            mirror_path=account.mirror_path,
            trash_retention_days=account.trash_retention_days,
            scope=account.scope,
        )
        # Through the document: the file is Pony's, and writing a
        # calendar configuration *over* it would take the mail keys with
        # it.
        self._document().save(
            AppConfig(
                config_version=config.config_version,
                use_utf8=config.use_utf8,
                editor=config.editor,
                accounts=(updated_account,),
            )
        )

        # cmd_oauth_authorize supports an injected auth_flow; call
        # it directly for the cleanest test.
        from pony.cli import calendar as cli

        code = cli.cmd_oauth_authorize(
            self.stdout,
            self.stderr,
            document=self._document(),
            paths=CalendarPaths(root=self.tmp),
            account_name="google",
            auth_flow=fake_flow,
        )
        self.assertEqual(code, 0)
        self.assertIn("would have printed", self.stdout.getvalue())
        self.assertTrue(token_path.exists())
        self.assertIn("cid", captured["credential"].client_id)

    def test_authorize_rejects_non_oauth_account(self) -> None:
        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                "basic",
                "--url",
                "https://x/",
                "--username",
                "u",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "pw",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["oauth", "authorize", "--account", "basic"])
        self.assertEqual(code, 2)
        self.assertIn("does not use an OAuth backend", self.stderr.getvalue())

    def test_authorize_unknown_account(self) -> None:
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["oauth", "authorize", "--account", "nobody"])
        self.assertEqual(code, 1)
        self.assertIn("account not found", self.stderr.getvalue())

    def test_authorize_accepts_google_backend(self) -> None:
        """`google` is OAuth shorthand; `oauth authorize` must run the
        loopback flow with the underlying client_id/secret + Google scope."""
        from pony.calendar.domain import GOOGLE_OAUTH_SCOPE, OAuthCredential
        from pony.calendar.oauth import StoredTokens

        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--credential-backend",
                "google",
                "--client-id",
                "g-cid",
                "--client-secret",
                "g-cs",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)

        captured: dict[str, Any] = {}

        def fake_flow(credential: OAuthCredential, _stdout: Any) -> StoredTokens:
            captured["credential"] = credential
            return StoredTokens(
                access_token="at",
                refresh_token="rt",
                expiry_unix=12345.0,
                scope=credential.scope,
            )

        from pony.cli import calendar as cli

        code = cli.cmd_oauth_authorize(
            self.stdout,
            self.stderr,
            document=self._document(),
            paths=CalendarPaths(root=self.tmp),
            account_name="google",
            auth_flow=fake_flow,
        )
        self.assertEqual(code, 0)
        cred = captured["credential"]
        assert isinstance(cred, OAuthCredential)
        self.assertEqual(cred.client_id, "g-cid")
        self.assertEqual(cred.client_secret, "g-cs")
        self.assertEqual(cred.scope, GOOGLE_OAUTH_SCOPE)
        self.assertTrue(CalendarPaths(root=self.tmp).token_path("google").exists())

    def test_authorize_defaults_to_remote_browser_when_headless(self) -> None:
        from pony.calendar.oauth import StoredTokens

        self._add_oauth_account()
        flow = mock.MagicMock(
            return_value=StoredTokens(
                access_token="at",
                refresh_token="rt",
                expiry_unix=12345.0,
                scope="scope",
            )
        )
        with (
            mock.patch(
                "pony.calendar.oauth.has_local_graphical_browser", return_value=False
            ),
            mock.patch("pony.cli.calendar._default_remote_browser_flow", flow),
        ):
            code = cli.cmd_oauth_authorize(
                self.stdout,
                self.stderr,
                document=self._document(),
                paths=CalendarPaths(root=self.tmp),
                account_name="google",
            )
        self.assertEqual(code, 0)
        flow.assert_called_once()
        self.assertTrue(CalendarPaths(root=self.tmp).token_path("google").exists())

    def test_authorize_uses_loopback_when_graphical(self) -> None:
        from pony.calendar.oauth import StoredTokens

        self._add_oauth_account()
        flow = mock.MagicMock(
            return_value=StoredTokens(
                access_token="at",
                refresh_token="rt",
                expiry_unix=12345.0,
                scope="scope",
            )
        )
        with (
            mock.patch(
                "pony.calendar.oauth.has_local_graphical_browser", return_value=True
            ),
            mock.patch("pony.cli.calendar._default_loopback_flow", flow),
        ):
            code = cli.cmd_oauth_authorize(
                self.stdout,
                self.stderr,
                document=self._document(),
                paths=CalendarPaths(root=self.tmp),
                account_name="google",
            )
        self.assertEqual(code, 0)
        flow.assert_called_once()
        self.assertTrue(CalendarPaths(root=self.tmp).token_path("google").exists())


class CliAuthorizerTest(CalendarRootMixin, unittest.TestCase):
    """`default_cli_authorizer` picks a usable OAuth flow for the terminal."""

    def test_raises_when_no_tty(self) -> None:
        from pony.calendar.domain import OAuthCredential
        from pony.calendar.oauth import OAuthError

        spec = OAuthCredential(client_id="c", client_secret="s", scope="x")
        with (
            mock.patch("pony.cli.calendar.sys.stdin") as stdin,
            mock.patch("pony.cli.calendar.sys.stdout") as stdout,
        ):
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            with self.assertRaises(OAuthError) as ctx:
                cli.default_cli_authorizer("google", spec, Path("/unused"))
        self.assertIn("TTY", str(ctx.exception))

    def test_delegates_to_loopback_flow_when_graphical(self) -> None:
        from pony.calendar.domain import OAuthCredential
        from pony.calendar.oauth import StoredTokens

        spec = OAuthCredential(client_id="c", client_secret="s", scope="x")
        tokens = StoredTokens(
            access_token="at", refresh_token="rt", expiry_unix=1.0, scope="x"
        )
        flow = mock.MagicMock(return_value=tokens)
        with (
            mock.patch("pony.cli.calendar.sys.stdin") as stdin,
            mock.patch("pony.cli.calendar.sys.stdout") as stdout,
            mock.patch(
                "pony.calendar.oauth.has_local_graphical_browser", return_value=True
            ),
            mock.patch("pony.cli.calendar._default_loopback_flow", flow),
        ):
            stdin.isatty.return_value = True
            stdout.isatty.return_value = True
            result = cli.default_cli_authorizer("google", spec, Path("/unused"))
        self.assertIs(result, tokens)
        flow.assert_called_once()
        # And the user is told what's happening before the browser opens.
        write_calls = [c.args[0] for c in stdout.write.call_args_list]
        self.assertTrue(any("OAuth authorization required" in w for w in write_calls))

    def test_delegates_to_remote_browser_flow_when_headless(self) -> None:
        from pony.calendar.domain import OAuthCredential
        from pony.calendar.oauth import StoredTokens

        spec = OAuthCredential(client_id="c", client_secret="s", scope="x")
        tokens = StoredTokens(
            access_token="at", refresh_token="rt", expiry_unix=1.0, scope="x"
        )
        flow = mock.MagicMock(return_value=tokens)
        with (
            mock.patch("pony.cli.calendar.sys.stdin") as stdin,
            mock.patch("pony.cli.calendar.sys.stdout") as stdout,
            mock.patch(
                "pony.calendar.oauth.has_local_graphical_browser", return_value=False
            ),
            mock.patch("pony.cli.calendar._default_remote_browser_flow", flow),
        ):
            stdin.isatty.return_value = True
            stdout.isatty.return_value = True
            result = cli.default_cli_authorizer("google", spec, Path("/unused"))
        self.assertIs(result, tokens)
        flow.assert_called_once()
        write_calls = [c.args[0] for c in stdout.write.call_args_list]
        self.assertTrue(any("paste the final redirect URL" in w for w in write_calls))


class OAuthFlowModeTest(CalendarRootMixin, unittest.TestCase):
    def test_w3m_browser_env_is_not_graphical(self) -> None:
        self.assertFalse(
            has_local_graphical_browser(  # pyright: ignore[reportPrivateUsage]
                env={"DISPLAY": ":1", "BROWSER": "w3m"},
                platform="linux",
            )
        )

    def test_graphical_browser_env_is_graphical(self) -> None:
        self.assertTrue(
            has_local_graphical_browser(  # pyright: ignore[reportPrivateUsage]
                env={"DISPLAY": ":1", "BROWSER": "firefox"},
                platform="linux",
            )
        )

    def test_mixed_browser_env_allows_graphical_fallback(self) -> None:
        self.assertTrue(
            has_local_graphical_browser(  # pyright: ignore[reportPrivateUsage]
                env={"DISPLAY": ":1", "BROWSER": "w3m:firefox"},
                platform="linux",
            )
        )


class ConfigEditCommandTest(ConfigEditingCliTestCase):
    def test_edit_applies_user_changes(self) -> None:
        self._run(["init"])

        def editor(path: Path) -> None:
            path.write_text(self._host_config() + "\n[calendar]\nuse_utf8 = true\n")

        code = self._run(["config", "edit"], open_editor=editor)
        self.assertEqual(code, 0)
        self.assertTrue(self._document().load().use_utf8)

    def test_edit_rejects_a_file_that_breaks_the_mail_half(self) -> None:
        """The whole file has to parse, and the refusal has to be caught.

        The document validates with Pony's own loader, which raises
        Pony's `ConfigError` -- a different class from the one this
        command catches, so an edit that broke the mail keys left a
        traceback instead of "parse error, original kept".
        """
        self._run(["init"])
        original = self.config_path.read_bytes()

        def editor(path: Path) -> None:
            path.write_text("config_version = 1\n", encoding="utf-8")

        code = self._run(["config", "edit"], open_editor=editor)

        self.assertEqual(code, 1)
        self.assertIn("config parse error", self.stderr.getvalue())
        self.assertEqual(self.config_path.read_bytes(), original)

    def test_edit_rejects_invalid_toml_keeps_original(self) -> None:
        self._run(["init"])
        original_bytes = self.config_path.read_bytes()

        def editor(path: Path) -> None:
            path.write_text("this is = = not valid", encoding="utf-8")

        code = self._run(["config", "edit"], open_editor=editor)
        self.assertEqual(code, 1)
        self.assertIn("parse error", self.stderr.getvalue())
        # Original file untouched.
        self.assertEqual(self.config_path.read_bytes(), original_bytes)

    def test_edit_handles_missing_config(self) -> None:
        self.config_path.unlink()
        code = self._run(["config", "edit"], open_editor=lambda _: None)
        self.assertEqual(code, 1)
        self.assertIn("not found", self.stderr.getvalue())

    def test_edit_leaves_no_tempfile_behind(self) -> None:
        self._run(["init"])

        def editor(path: Path) -> None:
            path.write_text(self._host_config())

        self._run(["config", "edit"], open_editor=editor)
        leftovers = [
            p
            for p in self.config_path.parent.iterdir()
            if p.name.startswith("chronos-edit-")
        ]
        self.assertEqual(leftovers, [])
