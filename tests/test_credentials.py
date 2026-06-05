"""Tests for credential providers and MCP state helpers."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4

from conftest import TMP_ROOT

from pony.config import ConfigError
from pony.credentials import (
    CommandCredentialsProvider,
    EnvVarCredentialsProvider,
    PlaintextCredentialsProvider,
    build_credentials_provider,
)
from pony.domain import AccountConfig, AppConfig, MirrorConfig, SmtpConfig
from pony.index_store import SqliteIndexRepository
from pony.mcp_server import McpState, clear_mcp_state, read_mcp_state, write_mcp_state

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_account(
    name: str = "testacct",
    *,
    credentials_source: str = "plaintext",
    password: str | None = "test-pw",
    password_command: list[str] | None = None,
) -> AccountConfig:
    mirror_dir = TMP_ROOT / "creds-mirrors" / uuid4().hex
    mirror_dir.mkdir(parents=True, exist_ok=True)
    return AccountConfig(
        name=name,
        email_address=f"{name}@example.com",
        imap_host="imap.example.com",
        smtp=SmtpConfig(host="smtp.example.com"),
        username=name,
        credentials_source=credentials_source,  # type: ignore[arg-type]
        mirror=MirrorConfig(path=mirror_dir, format="maildir"),
        password=password,
        password_command=tuple(password_command) if password_command else None,
    )


def _make_config(*accounts: AccountConfig) -> AppConfig:
    return AppConfig(accounts=tuple(accounts))


def _make_index() -> SqliteIndexRepository:
    db = TMP_ROOT / f"creds-test-{uuid4().hex}.sqlite3"
    idx = SqliteIndexRepository(database_path=db)
    idx.initialize()
    return idx


# ---------------------------------------------------------------------------
# PlaintextCredentialsProvider
# ---------------------------------------------------------------------------


class TestPlaintextCredentialsProvider(unittest.TestCase):
    def test_get_password_returns_configured_value(self) -> None:
        account = _make_account(name="myacct", password="hunter2")
        config = _make_config(account)
        provider = PlaintextCredentialsProvider(config)
        result = provider.get_password(account_name="myacct")
        self.assertEqual(result, "hunter2")

    def test_get_password_raises_for_unknown_account(self) -> None:
        account = _make_account(name="myacct", password="hunter2")
        config = _make_config(account)
        provider = PlaintextCredentialsProvider(config)
        with self.assertRaises(ConfigError):
            provider.get_password(account_name="no-such-account")

    def test_get_password_raises_when_password_is_none(self) -> None:
        account = _make_account(name="myacct", password=None)
        config = _make_config(account)
        provider = PlaintextCredentialsProvider(config)
        with self.assertRaises(ConfigError):
            provider.get_password(account_name="myacct")


# ---------------------------------------------------------------------------
# EnvVarCredentialsProvider
# ---------------------------------------------------------------------------


class TestEnvVarCredentialsProvider(unittest.TestCase):
    def setUp(self) -> None:
        self._env_key = "PONY_PASSWORD_TESTENVACCT"
        # Ensure env var is absent before each test
        os.environ.pop(self._env_key, None)

    def tearDown(self) -> None:
        os.environ.pop(self._env_key, None)

    def test_get_password_reads_env_var(self) -> None:
        os.environ[self._env_key] = "env-secret"
        provider = EnvVarCredentialsProvider()
        result = provider.get_password(account_name="testenvacct")
        self.assertEqual(result, "env-secret")

    def test_get_password_env_var_name_uppercased_spaces_to_underscores(self) -> None:
        env_key = "PONY_PASSWORD_MY_WORK_ACCOUNT"
        os.environ.pop(env_key, None)
        try:
            os.environ[env_key] = "work-secret"
            provider = EnvVarCredentialsProvider()
            result = provider.get_password(account_name="my work account")
            self.assertEqual(result, "work-secret")
        finally:
            os.environ.pop(env_key, None)

    def test_get_password_raises_when_env_var_missing(self) -> None:
        provider = EnvVarCredentialsProvider()
        with self.assertRaises(ConfigError):
            provider.get_password(account_name="testenvacct")


# ---------------------------------------------------------------------------
# CommandCredentialsProvider
# ---------------------------------------------------------------------------


class TestCommandCredentialsProvider(unittest.TestCase):
    def _echo_command(self) -> list[str]:
        # On Windows, 'echo' is a shell built-in; use python -c to be portable.
        return [sys.executable, "-c", "print('secret-pw')"]

    def test_get_password_runs_command_and_strips(self) -> None:
        account = _make_account(
            name="cmdacct",
            credentials_source="command",
            password=None,
            password_command=self._echo_command(),
        )
        config = _make_config(account)
        provider = CommandCredentialsProvider(config)
        result = provider.get_password(account_name="cmdacct")
        self.assertEqual(result, "secret-pw")

    def test_get_password_raises_for_missing_command_config(self) -> None:
        # Account with credentials_source=command but no password_command entry
        account = _make_account(
            name="cmdacct2",
            credentials_source="command",
            password=None,
            password_command=None,
        )
        config = _make_config(account)
        provider = CommandCredentialsProvider(config)
        with self.assertRaises(ConfigError):
            provider.get_password(account_name="cmdacct2")

    def test_get_password_raises_for_nonexistent_executable(self) -> None:
        account = _make_account(
            name="cmdacct3",
            credentials_source="command",
            password=None,
            password_command=["no-such-binary-that-exists-xyz"],
        )
        config = _make_config(account)
        provider = CommandCredentialsProvider(config)
        with self.assertRaises(ConfigError):
            provider.get_password(account_name="cmdacct3")


# ---------------------------------------------------------------------------
# build_credentials_provider (MultiProvider dispatch)
# ---------------------------------------------------------------------------


class TestBuildCredentialsProvider(unittest.TestCase):
    def test_dispatches_plaintext(self) -> None:
        account = _make_account(
            name="plain", credentials_source="plaintext", password="plain-pw"
        )
        config = _make_config(account)
        idx = _make_index()
        provider = build_credentials_provider(config, idx)
        result = provider.get_password(account_name="plain")
        self.assertEqual(result, "plain-pw")

    def test_dispatches_env(self) -> None:
        account = _make_account(
            name="envacct",
            credentials_source="env",
            password=None,
        )
        config = _make_config(account)
        idx = _make_index()
        env_key = "PONY_PASSWORD_ENVACCT"
        os.environ[env_key] = "dispatched-secret"
        try:
            provider = build_credentials_provider(config, idx)
            result = provider.get_password(account_name="envacct")
            self.assertEqual(result, "dispatched-secret")
        finally:
            os.environ.pop(env_key, None)

    def test_dispatches_command(self) -> None:
        cmd = [sys.executable, "-c", "print('cmd-dispatched')"]
        account = _make_account(
            name="cmddisp",
            credentials_source="command",
            password=None,
            password_command=cmd,
        )
        config = _make_config(account)
        idx = _make_index()
        provider = build_credentials_provider(config, idx)
        result = provider.get_password(account_name="cmddisp")
        self.assertEqual(result, "cmd-dispatched")


# ---------------------------------------------------------------------------
# MCP state write/read round-trip
# ---------------------------------------------------------------------------


class TestMcpStateRoundTrip(unittest.TestCase):
    def _state_file(self) -> Path:
        return TMP_ROOT / "mcp-state" / f"{uuid4().hex}.json"

    def test_write_then_read_returns_same_state(self) -> None:
        state_file = self._state_file()
        original = McpState(port=54321, token="tok-abc-123")
        write_mcp_state(state_file, original)
        recovered = read_mcp_state(state_file)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.port, original.port)
        self.assertEqual(recovered.token, original.token)

    def test_clear_mcp_state_removes_file(self) -> None:
        state_file = self._state_file()
        write_mcp_state(state_file, McpState(port=1234, token="tok"))
        self.assertTrue(state_file.exists())
        clear_mcp_state(state_file)
        self.assertFalse(state_file.exists())

    def test_clear_mcp_state_noop_when_file_absent(self) -> None:
        state_file = self._state_file()
        # Must not raise
        clear_mcp_state(state_file)

    def test_read_mcp_state_missing_file_returns_none(self) -> None:
        missing = TMP_ROOT / "no-such-dir" / "no-such-file.json"
        result = read_mcp_state(missing)
        self.assertIsNone(result)

    def test_read_mcp_state_corrupt_file_returns_none(self) -> None:
        state_file = self._state_file()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("not-valid-json")
        result = read_mcp_state(state_file)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
