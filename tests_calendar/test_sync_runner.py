"""`build_sync_runner`: every account synced, each failure its own.

The runner is the model's, not a front end's -- the terminal, the
browser and `pony calendar sync` all run a sync through it -- so what it
takes is a `CalendarServices` bundle and what it returns is a
`SyncResult` per account, errors folded in so one bad account does not
take the whole run down.

These tests used to live in the command line's own test file and reach
the runner through a `CliContext`, which is how the runner came to take
one in the first place.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pony.calendar.credentials import DefaultCredentialsProvider
from pony.calendar.domain import (
    AccountConfig,
    AppConfig,
    EnvCredential,
    PlaintextCredential,
)
from pony.calendar.index_store import SqliteIndexRepository
from pony.calendar.paths import CalendarPaths
from pony.calendar.protocols import CredentialsProvider
from pony.calendar.services import (
    CalendarServices,
    SessionFactory,
    build_sync_runner,
)
from pony.calendar.storage import VdirMirrorRepository
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
        url="https://cal.example.com/",
        username="user@example.com",
        credential=credential or PlaintextCredential(password="s3cret"),
        mirror_path=None,
        trash_retention_days=30,
        include=(re.compile(".*"),),
        exclude=(),
        read_only=(),
    )


def _config(*accounts: AccountConfig) -> AppConfig:
    return AppConfig(
        config_version=1,
        use_utf8=False,
        editor=None,
        accounts=tuple(accounts) or (_account(),),
    )


class ServicesTestCase(unittest.TestCase):
    """A mirror, an index and a fake server, bundled as the model wants."""

    calendar_paths: CalendarPaths

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.session = FakeCalDAVSession()

    def _services(
        self,
        *,
        config: AppConfig | None = None,
        session_factory: SessionFactory | None = None,
        creds: CredentialsProvider | None = None,
        creds_env: dict[str, str] | None = None,
        paths: CalendarPaths | None = None,
    ) -> CalendarServices:
        layout = paths or self.calendar_paths
        return CalendarServices(
            config=config or _config(),
            mirror=self.mirror,
            index=self.index,
            creds=creds
            or DefaultCredentialsProvider(env=creds_env or {}, paths=layout),
            paths=layout,
            now=NOW,
            session_factory=session_factory,
        )


class SyncRunnerTest(ServicesTestCase):
    """`build_sync_runner` is the closure handed to the TUI's `_run_sync`.

    It mirrors `cmd_sync`'s flow but returns per-account `SyncResult`s
    instead of writing to stdout, with errors folded into the result's
    `errors` tuple so a single bad account doesn't take the whole sync
    down.
    """

    def _seed_server(self) -> None:
        self.session.add_calendar(url="https://cal.example.com/work/", name="work")
        self.session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

    def test_happy_path_returns_one_result_per_account(self) -> None:
        self._seed_server()
        from pony.calendar.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        runner = build_sync_runner(self._services(session_factory=factory))
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].account_name, "personal")
        self.assertEqual(results[0].errors, ())
        self.assertEqual(results[0].components_added, 1)

    def test_credential_failure_lands_in_errors(self) -> None:
        bad_config = _config(
            _account(credential=EnvCredential(variable="UNSET_FOR_TEST"))
        )
        runner = build_sync_runner(self._services(config=bad_config, creds_env={}))
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].errors[0].count("UNSET_FOR_TEST"), 1)
        self.assertEqual(results[0].components_added, 0)

    def test_caldav_error_lands_in_errors(self) -> None:
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

        runner = build_sync_runner(self._services(session_factory=broken_factory))
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertIn("simulated network failure", results[0].errors[0])

    def test_session_factory_not_implemented_lands_in_errors(self) -> None:
        from pony.calendar.authorization import Authorization

        def unimplemented_factory(
            _account: AccountConfig, _auth: Authorization
        ) -> FakeCalDAVSession:
            raise NotImplementedError("session factory not configured")

        runner = build_sync_runner(
            self._services(session_factory=unimplemented_factory)
        )
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertIn("session factory not configured", results[0].errors[0])

    def test_session_factory_caldav_error_lands_in_errors(self) -> None:
        """The default session factory may raise CalDAVError when
        constructing the session (e.g. Google email-discovery failing).
        That must land in the per-account errors tuple, not crash."""
        from pony.calendar.authorization import Authorization
        from pony.calendar.caldav import CalDAVError

        def broken_factory(
            _account: AccountConfig, _auth: Authorization
        ) -> FakeCalDAVSession:
            raise CalDAVError("session construction blew up")

        runner = build_sync_runner(self._services(session_factory=broken_factory))
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertIn("session construction blew up", results[0].errors[0])

    def test_oauth_on_commit_called_on_success(self) -> None:
        self._seed_server()

        from pony.calendar.authorization import Authorization

        committed: list[int] = []

        class StubCreds:
            def build_auth(self, account: AccountConfig) -> Authorization:
                _ = account
                return Authorization(
                    basic=("user@example.com", "pw"),
                    on_commit=lambda: committed.append(1),
                )

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        runner = build_sync_runner(
            self._services(creds=StubCreds(), session_factory=factory)
        )
        runner()
        self.assertEqual(committed, [1])

    def test_keyboard_interrupt_during_runner_releases_lock(self) -> None:
        from pony.calendar.authorization import Authorization

        self._seed_server()
        # Make discover_principal raise KeyboardInterrupt to simulate
        # a Ctrl-C landing mid-flight.
        flaky_session = FakeCalDAVSession()
        flaky_session.add_calendar(url="https://cal.example.com/work/", name="work")

        def boom() -> str:
            raise KeyboardInterrupt

        flaky_session.discover_principal = boom  # type: ignore[method-assign]

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return flaky_session

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        under_tmp = CalendarPaths(root=tmp)
        runner = build_sync_runner(
            self._services(session_factory=factory, paths=under_tmp)
        )
        with self.assertRaises(KeyboardInterrupt):
            runner()

        # Lock must be released — a fresh runner must be able to
        # acquire it without blocking or raising SyncLockError.
        again = self._services(session_factory=lambda *_: self.session, paths=under_tmp)
        self._seed_server()  # idempotent
        runner2 = build_sync_runner(again)
        results = runner2()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].errors, ())
