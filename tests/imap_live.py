"""Harness for running the sync engine against a real IMAP server.

Everything here is inert unless ``PONY_LIVE_IMAP=1`` is set, so an
ordinary ``pytest`` run is unaffected and needs no server.

``FakeImapSession`` covers the sync engine's logic well — the message
populations it mishandles are exactly what a fake is good at exploring —
but it decides for itself what a UID is, when UIDVALIDITY changes, and
what ``APPEND`` returns.  Those are the server's decisions, and the
UIDVALIDITY recovery path depends on all three.  This drives a real
Dovecot, started unprivileged by ``scripts/dovecot_userspace.sh``.

Enable with::

    PONY_LIVE_IMAP=1 uv run python -m pytest tests/test_imap_live.py

The first run downloads and unpacks Dovecot under ``~/.cache``; later
runs reuse it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

from pony.domain import AccountConfig, AppConfig, MirrorConfig, SmtpConfig
from pony.imap_client import ImapSession
from pony.protocols import ImapClientSession

ENV_FLAG = "PONY_LIVE_IMAP"
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dovecot_userspace.sh"
_HOST = "127.0.0.1"
_PORT = int(os.environ.get("PONY_DOVECOT_PORT", "14300"))
_PASSWORD = "pony"


def live_imap_enabled() -> bool:
    """Whether the caller asked for live-server tests."""
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def skip_reason() -> str | None:
    """``None`` when live tests can run, else why they cannot."""
    if not live_imap_enabled():
        return f"set {ENV_FLAG}=1 to run tests against a real IMAP server"
    if not _SCRIPT.exists():
        return f"{_SCRIPT} is missing"
    return None


def _reachable(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((_HOST, _PORT), timeout=timeout):
            return True
    except OSError:
        return False


class Dovecot:
    """Controls the throwaway server for one test.

    Deliberately not a fixture shared across the session: several
    scenarios need the mailbox emptied or its UID epoch changed, and both
    are server-lifecycle operations rather than IMAP commands.
    """

    username = os.environ.get("USER", "pony")

    def _run(self, *args: str) -> str:
        result = subprocess.run(  # noqa: S603
            [str(_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{_SCRIPT.name} {' '.join(args)} failed:\n{result.stderr}"
            )
        return result.stdout

    def ensure_installed(self) -> None:
        self._run("install")

    def start(self) -> None:
        self._run("start")
        for _ in range(50):
            if _reachable():
                return
            time.sleep(0.2)
        raise RuntimeError("dovecot did not start listening")

    def stop(self) -> None:
        self._run("stop")

    def reset(self) -> None:
        """Wipe all mail and restart with an empty store."""
        self._run("reset")
        self.start()

    def bump_uidvalidity(self, folder: str = "INBOX") -> None:
        """Force a new UID epoch, the way a restore from backup does."""
        self._run("bump", folder)
        self.start()

    # -- wiring into pony -------------------------------------------------

    def account(self, *, name: str, mirror_root: Path) -> AccountConfig:
        return AccountConfig(
            name=name,
            email_address=f"{self.username}@example.test",
            imap_host=_HOST,
            imap_port=_PORT,
            imap_ssl=False,
            smtp=SmtpConfig(host="smtp.invalid"),
            username=self.username,
            credentials_source="plaintext",
            password=_PASSWORD,
            mirror=MirrorConfig(path=mirror_root, format="maildir"),
        )

    def config(self, account: AccountConfig) -> AppConfig:
        return AppConfig(accounts=(account,))

    def session_factory(self):  # type: ignore[no-untyped-def]
        """A factory the sync service can call for real sessions."""

        def factory(_account: AccountConfig, password: str) -> ImapClientSession:
            return ImapSession(
                host=_HOST,
                port=_PORT,
                ssl=False,
                username=self.username,
                password=password,
            )

        return factory

    def raw_session(self) -> ImapSession:
        """A session for the test itself to set the server up with."""
        return ImapSession(
            host=_HOST,
            port=_PORT,
            ssl=False,
            username=self.username,
            password=_PASSWORD,
        )


class FixedCredentials:
    """The password the harness configured, for the sync service."""

    def get_password(self, *, account_name: str = "") -> str:  # noqa: ARG002
        return _PASSWORD
