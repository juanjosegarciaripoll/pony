"""Credential provider implementations for Pony Express.

Four backends are supported, selected by ``AccountConfig.credential_backend``:

- ``plaintext``  – password stored as-is in ``config.toml``
- ``env``        – read from ``PONY_PASSWORD_<ACCOUNT_NAME>`` (uppercased,
                   spaces replaced by underscores)
- ``command``    – run ``password_command`` and read stdout
- ``encrypted``  – AES-like blob stored in the SQLite index; key derived from
                   OS/machine identity (DPAPI on Windows, PBKDF2+SHAKE-256 on
                   macOS/Linux)

The ``build_credentials_provider`` factory inspects the account config and
returns the appropriate provider.  All providers implement the
``CredentialsProvider`` protocol (``get_password(*, account_name) -> str``).
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ConfigError

if TYPE_CHECKING:
    from .domain import AppConfig
    from .index_store import SqliteIndexRepository


# ---------------------------------------------------------------------------
# Plaintext
# ---------------------------------------------------------------------------


class PlaintextCredentialsProvider:
    """Reads passwords from the ``password`` field in ``config.toml``."""

    def __init__(self, config: AppConfig) -> None:
        from .domain import AccountConfig

        self._by_name = {
            a.name: a.password
            for a in config.accounts
            if isinstance(a, AccountConfig)
        }

    def get_password(self, *, account_name: str) -> str:
        password = self._by_name.get(account_name)
        if not password:
            raise ConfigError(
                f"no password configured for account {account_name!r} — "
                "add a 'password' field to the account in config.toml"
            )
        return password


# ---------------------------------------------------------------------------
# Environment variable
# ---------------------------------------------------------------------------


class EnvVarCredentialsProvider:
    """Reads passwords from ``PONY_PASSWORD_<ACCOUNT_NAME>``.

    The account name is uppercased and spaces are replaced with underscores,
    so an account named ``work email`` maps to ``PONY_PASSWORD_WORK_EMAIL``.
    """

    def get_password(self, *, account_name: str) -> str:
        env_key = "PONY_PASSWORD_" + account_name.upper().replace(" ", "_")
        value = os.environ.get(env_key)
        if not value:
            raise ConfigError(
                f"no password found for account {account_name!r} — "
                f"set the {env_key} environment variable"
            )
        return value


# ---------------------------------------------------------------------------
# External command
# ---------------------------------------------------------------------------


class CommandCredentialsProvider:
    """Runs a command and reads the password from its stdout.

    ``password_command`` must be a non-empty sequence of strings passed
    directly to ``subprocess.run`` (no shell interpolation).
    """

    def __init__(self, config: AppConfig) -> None:
        from .domain import AccountConfig

        self._by_name = {
            a.name: a.password_command
            for a in config.accounts
            if isinstance(a, AccountConfig) and a.password_command
        }

    def get_password(self, *, account_name: str) -> str:
        command = self._by_name.get(account_name)
        if not command:
            raise ConfigError(
                f"no password_command configured for account {account_name!r}"
            )
        try:
            result = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                f"password_command for {account_name!r} not found: {command[0]!r}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise ConfigError(
                f"password_command for {account_name!r} exited with code"
                f" {exc.returncode}"
            ) from exc
        return result.stdout.strip()


# ---------------------------------------------------------------------------
# Encrypted (DB-backed)
# ---------------------------------------------------------------------------


class EncryptedCredentialsProvider:
    """Decrypts passwords stored as blobs in the SQLite credentials table.

    Encryption uses DPAPI on Windows; PBKDF2-HMAC-SHA256 key derivation +
    SHAKE-256 keystream XOR on macOS/Linux.  The key is derived from
    machine-specific information (machine ID + OS username) and is therefore
    tied to the local machine and user account.

    If no stored blob exists, a ``ConfigError`` is raised directing the
    user to ``pony account set-password``.  No interactive prompting.
    """

    def __init__(self, index: SqliteIndexRepository) -> None:
        self._index = index

    def get_password(self, *, account_name: str) -> str:
        blob = self._index.get_credential(account_name=account_name)
        if blob is None:
            raise ConfigError(
                f"no password stored for account {account_name!r} — "
                f"run `pony account set-password {account_name}` to set one"
            )
        return _decrypt(blob)

    def invalidate(self, *, account_name: str) -> None:
        """Delete the stored blob so the next ``get_password`` errors."""
        self._index.delete_credential(account_name=account_name)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_credentials_provider(
    config: AppConfig,
    index: SqliteIndexRepository,
) -> MultiProvider:
    """Return a provider that dispatches per account based on credential_backend."""
    return MultiProvider(config, index)


class MultiProvider:
    """Dispatches ``get_password`` to the correct backend per account."""

    def __init__(self, config: AppConfig, index: SqliteIndexRepository) -> None:
        from .domain import AccountConfig

        self._plaintext = PlaintextCredentialsProvider(config)
        self._env = EnvVarCredentialsProvider()
        self._command = CommandCredentialsProvider(config)
        self._encrypted = EncryptedCredentialsProvider(index)

        self._backend = {
            a.name: a.credentials_source
            for a in config.accounts
            if isinstance(a, AccountConfig)
        }

    def get_password(self, *, account_name: str) -> str:
        backend = self._backend.get(account_name, "plaintext")
        if backend == "plaintext":
            return self._plaintext.get_password(account_name=account_name)
        if backend == "env":
            return self._env.get_password(account_name=account_name)
        if backend == "command":
            return self._command.get_password(account_name=account_name)
        if backend == "encrypted":
            return self._encrypted.get_password(account_name=account_name)
        raise ConfigError(f"unknown credentials_source {backend!r}")

    def invalidate(self, *, account_name: str) -> None:
        """Clear a stored encrypted credential so the next call re-prompts.

        No-op for non-encrypted backends.
        """
        if self._backend.get(account_name) == "encrypted":
            self._encrypted.invalidate(account_name=account_name)


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    """Return True on Windows. Extracted to prevent static branch elimination."""
    return sys.platform == "win32"


def encrypt_password(plaintext: str) -> bytes:
    """Encrypt *plaintext* using the platform-appropriate method."""
    if _is_windows():
        return _dpapi_encrypt(plaintext)
    return _shake_encrypt(plaintext, _derive_key())


def _decrypt(blob: bytes) -> str:
    """Decrypt a blob produced by ``encrypt_password``."""
    if _is_windows():
        return _dpapi_decrypt(blob)
    return _shake_decrypt(blob, _derive_key())


# -- PBKDF2 + SHAKE-256 (macOS / Linux) ------------------------------------


def _derive_key() -> bytes:
    """Derive a 32-byte key from machine ID and OS username."""
    machine_id = _get_machine_id().encode()
    username = (os.environ.get("USER") or os.environ.get("USERNAME") or "").encode()
    return hashlib.pbkdf2_hmac(
        "sha256",
        machine_id + b":" + username,
        b"pony-credential-v1",
        iterations=100_000,
        dklen=32,
    )


def _get_machine_id() -> str:
    """Return a stable machine-unique string."""
    if sys.platform == "linux":
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                return Path(path).read_text().strip()
            except OSError:
                pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        except (OSError, subprocess.CalledProcessError, IndexError):
            pass
    # Fallback: hostname — weak but always available
    return platform.node()


def _shake_encrypt(plaintext: str, key: bytes) -> bytes:
    pt = plaintext.encode("utf-8")
    nonce = os.urandom(16)
    keystream = hashlib.shake_256(key + nonce).digest(len(pt))
    ciphertext = bytes(a ^ b for a, b in zip(pt, keystream, strict=True))
    return nonce + ciphertext


def _shake_decrypt(blob: bytes, key: bytes) -> str:
    nonce, ciphertext = blob[:16], blob[16:]
    keystream = hashlib.shake_256(key + nonce).digest(len(ciphertext))
    plain = bytes(a ^ b for a, b in zip(ciphertext, keystream, strict=True))
    return plain.decode("utf-8")


# -- DPAPI (Windows only) --------------------------------------------------


def _dpapi_encrypt(plaintext: str) -> bytes:
    import ctypes
    import ctypes.wintypes

    data = plaintext.encode("utf-8")

    class _BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    blob_in = _BLOB(
        len(data),
        ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_char)),
    )
    blob_out = _BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ConfigError("DPAPI CryptProtectData failed")
    result = bytes(blob_out.pbData[: blob_out.cbData])
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _dpapi_decrypt(blob: bytes) -> str:
    import ctypes
    import ctypes.wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    blob_in = _BLOB(
        len(blob),
        ctypes.cast(ctypes.c_char_p(blob), ctypes.POINTER(ctypes.c_char)),
    )
    blob_out = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ConfigError("DPAPI CryptUnprotectData failed")
    result = bytes(blob_out.pbData[: blob_out.cbData])
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result.decode("utf-8")
