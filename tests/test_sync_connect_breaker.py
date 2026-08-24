"""The per-host breaker that stops one blocked server stalling a whole sync.

Several accounts commonly live on one server.  When a packet filter
silently drops SYNs, every connect costs the full timeout, so without a
breaker N accounts on one dead host cost N timeouts back to back — the
observed failure was four accounts on one host each burning the kernel's
~130 s SYN ceiling, freezing the TUI for nine minutes.

``ImapSyncService._connect`` therefore records a host the first time a
connect times out and fails the rest of that run's accounts on it
immediately.  The service is rebuilt per sync run, so the next run
retries the host from scratch.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

from conftest import TMP_ROOT
from test_sync import FakeImapSession, _make_raw_message

from pony.domain import (
    AccountConfig,
    AppConfig,
    MessageFlag,
    MirrorConfig,
    SmtpConfig,
)
from pony.index_store import SqliteIndexRepository
from pony.protocols import ImapClientSession, MirrorRepository
from pony.storage import MaildirMirrorRepository
from pony.sync import ImapSyncService


class _FixedCredentials:
    def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
        return "test-password"


def _one_message() -> dict[int, tuple[str, frozenset[MessageFlag], bytes]]:
    """A single-message INBOX, so a successful plan is not an empty one.

    ``plan()`` raises when it collected errors *and* the surviving plan
    has no work in it, so an account that must survive the breaker needs
    something to sync.
    """
    message_id = "<hello@example.com>"
    return {
        1: (
            message_id,
            frozenset[MessageFlag](),
            _make_raw_message("Hello", message_id),
        )
    }


def _account(name: str, host: str, root: Path) -> AccountConfig:
    return AccountConfig(
        name=name,
        email_address=f"{name}@example.com",
        imap_host=host,
        smtp=SmtpConfig(host=f"smtp.{host}"),
        username=name,
        credentials_source="plaintext",
        mirror=MirrorConfig(path=root / name, format="maildir"),
    )


class ConnectBreakerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMP_ROOT / "breaker" / uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)

    def _service(
        self,
        accounts: tuple[AccountConfig, ...],
        session_factory: object,
    ) -> ImapSyncService:
        index = SqliteIndexRepository(database_path=self.root / "index.sqlite3")
        index.initialize()

        def _mirror_factory(acc: AccountConfig) -> MirrorRepository:
            return MaildirMirrorRepository(
                account_name=acc.name, root_dir=self.root / acc.name
            )

        return ImapSyncService(
            config=AppConfig(accounts=accounts),
            mirror_factory=_mirror_factory,
            index=index,
            credentials=_FixedCredentials(),
            session_factory=session_factory,  # type: ignore[arg-type]
        )

    def test_one_timeout_skips_the_remaining_accounts_on_that_host(self) -> None:
        """The factory must be called once, not once per account."""
        accounts = tuple(
            _account(n, "imap.example.com", self.root) for n in ("one", "two", "three")
        )
        attempts: list[str] = []

        def _factory(acc: AccountConfig, _pw: str) -> ImapClientSession:
            attempts.append(acc.name)
            raise TimeoutError("[Errno 110] Connection timed out")

        service = self._service(accounts, _factory)

        # Every account failed, so the plan is empty and plan() reports it.
        with self.assertRaises(RuntimeError) as ctx:
            service.plan()

        self.assertEqual(attempts, ["one"])
        message = str(ctx.exception)
        self.assertIn("two", message)
        self.assertIn("three", message)

    def test_a_different_host_still_gets_its_connection(self) -> None:
        """The breaker is per host, not global."""
        accounts = (
            _account("blocked", "dead.example.com", self.root),
            _account("reachable", "live.example.com", self.root),
        )
        attempts: list[str] = []

        def _factory(acc: AccountConfig, _pw: str) -> ImapClientSession:
            attempts.append(acc.imap_host)
            if acc.imap_host == "dead.example.com":
                raise TimeoutError("[Errno 110] Connection timed out")
            return FakeImapSession(folders={"INBOX": _one_message()})

        service = self._service(accounts, _factory)
        plan = service.plan()

        self.assertEqual(attempts, ["dead.example.com", "live.example.com"])
        self.assertEqual([a.account_name for a in plan.accounts], ["reachable"])

    def test_a_refused_connection_does_not_trip_the_breaker(self) -> None:
        """Only timeouts are expensive; a refusal fails fast on its own."""
        accounts = tuple(
            _account(n, "imap.example.com", self.root) for n in ("one", "two", "three")
        )
        attempts: list[str] = []

        def _factory(acc: AccountConfig, _pw: str) -> ImapClientSession:
            attempts.append(acc.name)
            raise ConnectionRefusedError("[Errno 111] Connection refused")

        service = self._service(accounts, _factory)
        with self.assertRaises(RuntimeError):
            service.plan()

        self.assertEqual(attempts, ["one", "two", "three"])

    def test_a_host_that_dies_between_plan_and_execute_trips_once(self) -> None:
        """The breaker spans both halves of a run — plan() and execute().

        Both accounts plan cleanly, then the host stops answering.  The
        first execution attempt must be the only one to pay the timeout.
        """
        accounts = (
            _account("one", "imap.example.com", self.root),
            _account("two", "imap.example.com", self.root),
        )
        reachable = True
        attempts: list[str] = []

        def _factory(acc: AccountConfig, _pw: str) -> ImapClientSession:
            attempts.append(acc.name)
            if not reachable:
                raise TimeoutError("[Errno 110] Connection timed out")
            return FakeImapSession(folders={"INBOX": _one_message()})

        service = self._service(accounts, _factory)
        plan = service.plan()
        self.assertEqual(len(plan.accounts), 2)

        reachable = False
        attempts.clear()
        result = service.execute(plan)

        self.assertEqual(attempts, ["one"])
        self.assertEqual(result.accounts, ())
