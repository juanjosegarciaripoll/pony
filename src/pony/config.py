"""Configuration loading and parsing for Pony Express.

The public surface is intentionally small:

- ``ConfigError``   – the single exception type for all config problems
- ``AppConfig``     – re-exported from ``pony.domain`` for caller convenience
- ``load_config``   – read a TOML/JSON file and return a validated AppConfig
- ``parse_config``  – validate an already-loaded raw dict and return AppConfig

Everything else is a private implementation detail.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import cast

from .domain import (
    CONFIG_VERSION,
    AccountConfig,
    AnyAccount,
    CredentialsSource,
    FolderConfig,
    LocalAccountConfig,
    McpConfig,
    MirrorConfig,
    MirrorFormat,
    SmtpConfig,
)
from .domain import (
    AppConfig as AppConfig,  # explicit re-export
)
from .paths import AppPaths


class ConfigError(ValueError):
    """Raised when configuration is missing, structurally invalid, or contains
    values that fail validation."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load application configuration from a TOML or JSON file."""
    path = config_path or AppPaths.default().config_file
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    try:
        data = _read_config_data(path)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"invalid config syntax in file: {path}") from error

    return parse_config(data, base_dir=path.parent)


def parse_config(
    data: object, *, base_dir: Path | None = None  # noqa: ARG001
) -> AppConfig:
    """Validate raw TOML/JSON data and return a typed ``AppConfig``.

    ``base_dir`` is accepted for backwards compatibility but no longer
    used — relative mirror paths now resolve against Pony's standard
    data directory.
    """
    return _parse_app_config(data)


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------


def _read_config_data(path: Path) -> object:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return tomllib.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Recursive parser – constructs domain objects directly
# ---------------------------------------------------------------------------


def _parse_app_config(raw: object) -> AppConfig:
    if not isinstance(raw, dict):
        raise ConfigError("top-level config must be an object")
    data = cast("dict[str, object]", raw)

    _require_config_version(data)

    accounts_raw = data.get("accounts", [])
    if not isinstance(accounts_raw, list):
        raise ConfigError("'accounts' must be a list of tables")
    # isinstance narrows to list[Unknown]; cast to list[object] for element access.
    accounts: tuple[AnyAccount, ...] = tuple(
        _parse_any_account(item)
        for item in cast("list[object]", accounts_raw)
    )
    use_utf8 = _require_bool(data, "use_utf8", default=False)
    editor = _optional_string(data, "editor")
    markdown_compose = _require_bool(data, "markdown_compose", default=False)
    bbdb_raw = _optional_string(data, "bbdb_path")
    bbdb_path = _expand_path(bbdb_raw) if bbdb_raw else None
    mcp_raw = data.get("mcp")
    if isinstance(mcp_raw, dict):
        mcp_data = cast("dict[str, object]", mcp_raw)
        mcp = McpConfig(
            host=_optional_string(mcp_data, "host") or "127.0.0.1",
            port=_require_int(mcp_data, "port", default=8765),
        )
    else:
        mcp = None
    return AppConfig(
        accounts=accounts,
        use_utf8=use_utf8,
        editor=editor,
        markdown_compose=markdown_compose,
        bbdb_path=bbdb_path,
        mcp=mcp,
    )


def _parse_any_account(raw: object) -> AnyAccount:
    """Dispatch to the correct account parser based on ``account_type``."""
    if not isinstance(raw, dict):
        raise ConfigError("each account must be an object")
    data = cast("dict[str, object]", raw)
    account_type = data.get("account_type", "imap")
    if account_type == "local":
        return _parse_local_account(data)
    if account_type == "imap":
        return _parse_imap_account(data)
    raise ConfigError(
        f"account_type must be 'imap' or 'local', got {account_type!r}"
    )


def _parse_local_account(data: dict[str, object]) -> LocalAccountConfig:
    smtp_raw = data.get("smtp")
    if smtp_raw is None:
        smtp: SmtpConfig | None = None
        username: str | None = None
        creds: CredentialsSource | None = None
        password: str | None = None
        password_command: tuple[str, ...] | None = None
    else:
        # SMTP block present — credentials become mandatory for this
        # account (local accounts don't have IMAP creds to fall back on).
        smtp = _parse_smtp(_require_mapping(data, "smtp"))
        username = _require_string(data, "username")
        creds = _require_credentials_source(data)
        password = _optional_password(data)
        password_command = _optional_password_command(data)

    return LocalAccountConfig(
        name=_require_string(data, "name"),
        email_address=_require_string(data, "email_address"),
        mirror=_parse_mirror(_require_mapping(data, "mirror")),
        sent_folder=_optional_string(data, "sent_folder"),
        drafts_folder=_optional_string(data, "drafts_folder"),
        markdown_compose=_require_bool(data, "markdown_compose", default=False),
        signature=_optional_string(data, "signature"),
        smtp=smtp,
        username=username,
        credentials_source=creds,
        password=password,
        password_command=password_command,
    )


def _parse_imap_account(raw: object) -> AccountConfig:
    if not isinstance(raw, dict):
        raise ConfigError("each account must be an object")
    data = cast("dict[str, object]", raw)

    folders_raw = data.get("folders")
    folders = (
        _parse_folder_config(_require_mapping(data, "folders"))
        if folders_raw is not None
        else FolderConfig()
    )

    imap_ssl = _require_bool(data, "imap_ssl", default=True)

    return AccountConfig(
        name=_require_string(data, "name"),
        email_address=_require_string(data, "email_address"),
        imap_host=_require_string(data, "imap_host"),
        smtp=_parse_smtp(_require_mapping(data, "smtp")),
        username=_require_string(data, "username"),
        credentials_source=_require_credentials_source(data),
        mirror=_parse_mirror(_require_mapping(data, "mirror")),
        imap_port=_require_int(data, "imap_port", default=993 if imap_ssl else 143),
        imap_ssl=imap_ssl,
        password=_optional_password(data),
        password_command=_optional_password_command(data),
        folders=folders,
        sent_folder=_optional_string(data, "sent_folder"),
        drafts_folder=_optional_string(data, "drafts_folder"),
        archive_folder=_optional_string(data, "archive_folder"),
        markdown_compose=_require_bool(data, "markdown_compose", default=False),
        signature=_optional_string(data, "signature"),
    )


def _parse_smtp(data: dict[str, object]) -> SmtpConfig:
    ssl = _require_bool(data, "ssl", default=True)
    return SmtpConfig(
        host=_require_string(data, "host"),
        port=_require_int(data, "port", default=465 if ssl else 587),
        ssl=ssl,
    )


def _optional_password(data: dict[str, object]) -> str | None:
    raw = data.get("password")
    return raw if isinstance(raw, str) else None


def _optional_password_command(data: dict[str, object]) -> tuple[str, ...] | None:
    raw = data.get("password_command")
    if raw is None:
        return None
    items = cast("list[object]", raw) if isinstance(raw, list) else None
    if items is None or not all(isinstance(s, str) for s in items):
        raise ConfigError("'password_command' must be a list of strings")
    return tuple(cast("list[str]", raw))


def _require_config_version(data: dict[str, object]) -> None:
    """Require the top-level ``config_version`` to match ``CONFIG_VERSION``.

    Missing or mismatched values are rejected loudly rather than
    silently migrated — the user is expected to update the file to
    the current schema when Pony's format changes.
    """
    raw = data.get("config_version")
    if raw is None:
        raise ConfigError(
            "missing 'config_version' at the top of config.toml.\n"
            f"Add a line:\n  config_version = {CONFIG_VERSION}\n"
            "and update the schema to match this version of Pony Express. "
            "See the sample config for the current format."
        )
    if not isinstance(raw, int):
        raise ConfigError("'config_version' must be an integer")
    if raw != CONFIG_VERSION:
        raise ConfigError(
            f"unsupported config_version {raw}; this build of Pony "
            f"Express requires config_version = {CONFIG_VERSION}.  "
            "Update your config.toml to the current schema (see the "
            "sample config and CHANGELOG for migration notes)."
        )


def _parse_folder_config(data: dict[str, object]) -> FolderConfig:
    return FolderConfig(
        include=_require_string_list(data, "include"),
        exclude=_require_string_list(data, "exclude"),
        read_only=_require_string_list(data, "read_only"),
    )


def _parse_mirror(data: dict[str, object]) -> MirrorConfig:
    path_str = _require_string(data, "path")
    # Expand environment variables (e.g. $HOME, %USERPROFILE%).
    mirror_path = _expand_path(path_str)
    if not mirror_path.is_absolute():
        # Relative paths are resolved against Pony's standard data
        # directory, not the config file's location.  Use an absolute
        # path to store mirrors elsewhere.
        mirror_path = (AppPaths.default().data_dir / mirror_path).resolve()

    trash_retention_days = _require_int(data, "trash_retention_days", default=30)
    if trash_retention_days < 0:
        raise ConfigError("mirror.trash_retention_days must be non-negative")

    return MirrorConfig(
        path=mirror_path,
        format=_require_mirror_format(data),
        trash_retention_days=trash_retention_days,
    )


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------

_VALID_MIRROR_FORMATS: frozenset[str] = frozenset({"maildir", "mbox"})
_VALID_CREDENTIALS_SOURCES: frozenset[str] = frozenset(
    {"plaintext", "env", "command", "encrypted"}
)


def _require_mapping(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key!r} must be an object")
    return cast("dict[str, object]", value)


def _require_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key!r} must be a non-empty string")
    return value


def _require_string_list(data: dict[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"{key!r} must be a list of strings")
    items = cast("list[object]", value)
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item:
            raise ConfigError(f"{key!r}[{i}] must be a non-empty string")
        if item != "*":  # glob shorthand for .*, not valid regex
            try:
                re.compile(item)
            except re.error as exc:
                raise ConfigError(
                    f"{key!r}[{i}] is not a valid regex pattern: {exc}"
                ) from exc
    return tuple(cast("list[str]", items))


def _require_int(data: dict[str, object], key: str, *, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int):
        raise ConfigError(f"{key!r} must be an integer")
    return value


def _expand_path(raw: str) -> Path:
    """Expand ``~``, ``$VAR``, and ``%VAR%`` in a path string."""
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def _optional_string(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key!r} must be a non-empty string")
    return value


def _require_bool(data: dict[str, object], key: str, *, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key!r} must be a boolean")
    return value


def _require_mirror_format(data: dict[str, object]) -> MirrorFormat:
    """Validate and narrow 'format' to the ``MirrorFormat`` literal type.

    Python's type system cannot narrow ``str`` to a ``Literal`` via set
    membership, so we validate first and then ``cast``.  The cast is safe
    because the preceding check guarantees the value is one of the allowed
    string literals.
    """
    raw = _require_string(data, "format")
    if raw not in _VALID_MIRROR_FORMATS:
        raise ConfigError("mirror.format must be 'maildir' or 'mbox'")
    return cast("MirrorFormat", raw)



def _require_credentials_source(data: dict[str, object]) -> CredentialsSource:
    """Validate and narrow 'credentials_source' to the ``CredentialsSource`` literal.

    Defaults to 'plaintext' if omitted. Same narrowing rationale as
    ``_require_mirror_format``.
    """
    raw = data.get("credentials_source", "plaintext")
    if not isinstance(raw, str) or raw not in _VALID_CREDENTIALS_SOURCES:
        raise ConfigError(
            "credentials_source must be one of: plaintext, env, command, encrypted"
        )
    return cast("CredentialsSource", raw)
