from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pony.calendar.atomic import write_atomically
from pony.calendar.domain import (
    CALENDAR_CONFIG_VERSION,
    DEFAULT_BACKGROUND_SYNC_INTERVAL_SECONDS,
    GOOGLE_CALDAV_URL,
    AccountConfig,
    AppConfig,
    CommandCredential,
    CredentialSpec,
    EnvCredential,
    GoogleCredential,
    OAuthCredential,
    PlaintextCredential,
)
from pony.calendar.paths import CalendarPaths, expand_path, is_half_qualified

_DEFAULT_TRASH_RETENTION_DAYS = 30
_DEFAULT_INCLUDE = (".*",)


class ConfigError(ValueError):
    def __init__(self, message: str, key: str = "") -> None:
        self.key = key
        super().__init__(f"[{key}] {message}" if key else message)


def load(path: Path) -> AppConfig:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML parse error: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file is not UTF-8: {exc}") from exc
    return parse(cast(dict[str, object], data))


def parse(data: dict[str, object]) -> AppConfig:
    config_version = _require_int(data, "config_version", "")
    if config_version != CALENDAR_CONFIG_VERSION:
        # Read and never compared, so `config_version = 999` parsed
        # cleanly and the file was then interpreted as the current
        # schema anyway.  Hosted under Pony the number is injected and
        # always matches; a calendar reading its own config.toml is the
        # path this guards.
        raise ConfigError(
            f"unsupported config_version {config_version}; this build "
            f"requires config_version = {CALENDAR_CONFIG_VERSION}",
            "config_version",
        )
    use_utf8 = _optional_bool(data, "use_utf8", "", default=False)
    editor = _optional_str(data, "editor", "", default=None)
    theme = _optional_str(data, "theme", "", default=None)
    background_sync_enabled = _optional_bool(
        data, "background_sync_enabled", "", default=True
    )
    background_sync_interval_seconds = _optional_int(
        data,
        "background_sync_interval_seconds",
        "",
        default=DEFAULT_BACKGROUND_SYNC_INTERVAL_SECONDS,
    )
    if background_sync_interval_seconds <= 0:
        raise ConfigError(
            "'background_sync_interval_seconds' must be positive",
            "background_sync_interval_seconds",
        )
    accounts_raw = data.get("accounts", [])
    if not isinstance(accounts_raw, list):
        raise ConfigError("'accounts' must be an array of tables", "accounts")
    accounts_list = cast(list[object], accounts_raw)
    accounts = tuple(
        _parse_account(a, f"accounts[{i}]") for i, a in enumerate(accounts_list)
    )
    return AppConfig(
        config_version=config_version,
        use_utf8=use_utf8,
        editor=editor,
        accounts=accounts,
        theme=theme,
        background_sync_enabled=background_sync_enabled,
        background_sync_interval_seconds=background_sync_interval_seconds,
    )


def _parse_account(data: object, key: str) -> AccountConfig:
    if not isinstance(data, dict):
        raise ConfigError("must be a table", key)
    account = cast(dict[str, object], data)
    name = _require_str(account, "name", key)
    account_key = f"{key}[{name}]"
    credential = _parse_credential(
        account.get("credential"), f"{account_key}.credential"
    )
    # The "google" backend has well-known url + username defaults so
    # the user only has to write the OAuth client_id + client_secret.
    if isinstance(credential, GoogleCredential):
        url = (
            _optional_str(account, "url", account_key, default=GOOGLE_CALDAV_URL)
            or GOOGLE_CALDAV_URL
        )
        username = _optional_str(account, "username", account_key, default="") or ""
    else:
        url = _require_str(account, "url", account_key)
        username = _require_str(account, "username", account_key)
    mirror_path = _parse_mirror_path(account, name, account_key)
    trash_retention_days = _optional_int(
        account,
        "trash_retention_days",
        account_key,
        default=_DEFAULT_TRASH_RETENTION_DAYS,
    )
    include = _parse_regex_list(
        account.get("include", list(_DEFAULT_INCLUDE)), f"{account_key}.include"
    )
    exclude = _parse_regex_list(account.get("exclude", []), f"{account_key}.exclude")
    read_only = _parse_regex_list(
        account.get("read_only", []), f"{account_key}.read_only"
    )
    return AccountConfig(
        name=name,
        url=url,
        username=username,
        credential=credential,
        mirror_path=mirror_path,
        trash_retention_days=trash_retention_days,
        include=include,
        exclude=exclude,
        read_only=read_only,
    )


def _parse_credential(data: object, key: str) -> CredentialSpec:
    if data is None:
        raise ConfigError("missing required field", key)
    if not isinstance(data, dict):
        raise ConfigError("must be an inline table", key)
    spec = cast(dict[str, object], data)
    backend = _require_str(spec, "backend", key)
    if backend == "plaintext":
        return PlaintextCredential(password=_require_str(spec, "password", key))
    if backend == "env":
        return EnvCredential(variable=_require_str(spec, "variable", key))
    if backend == "command":
        cmd_raw = spec.get("command")
        if not isinstance(cmd_raw, list) or not cmd_raw:
            raise ConfigError("'command' must be a non-empty array of strings", key)
        cmd_parts: list[str] = []
        for i, part in enumerate(cast(list[object], cmd_raw)):
            if not isinstance(part, str):
                raise ConfigError("must be a string", f"{key}.command[{i}]")
            cmd_parts.append(part)
        return CommandCredential(command=tuple(cmd_parts))
    if backend == "encrypted":
        # Mail's `credentials_source = "encrypted"` is a working
        # DPAPI/SHAKE store; this name used to parse into a spec no
        # resolver could answer, so a calendar account configured with
        # it loaded cleanly and then failed at the first sync.  Refuse
        # the word here, and name what does work.
        raise ConfigError(
            "the 'encrypted' backend is not available for a calendar "
            "account: it would need the `keyring` package. Use "
            "'command' to read the password from a password manager, "
            "or 'env'.",
            key,
        )
    if backend == "google":
        return GoogleCredential(
            client_id=_require_str(spec, "client_id", key),
            client_secret=_require_str(spec, "client_secret", key),
        )
    if backend == "oauth":
        token_path_raw = _optional_str(spec, "token_path", key, default=None)
        return OAuthCredential(
            client_id=_require_str(spec, "client_id", key),
            client_secret=_require_str(spec, "client_secret", key),
            scope=_optional_str(
                spec,
                "scope",
                key,
                default="https://www.googleapis.com/auth/calendar",
            )
            or "https://www.googleapis.com/auth/calendar",
            token_path=expand_path(token_path_raw)
            if token_path_raw is not None
            else None,
        )
    raise ConfigError(
        f"unknown credential backend '{backend}' "
        "(expected: plaintext, env, command, oauth, google)",
        key,
    )


def _parse_mirror_path(
    account: dict[str, object], account_name: str, account_key: str
) -> Path | None:
    """What `mirror_path` declared, or None for the layout's default.

    Kept as written -- expanded, but not joined against anything.  The
    calendar's root belongs to the host that hands it over, so a
    parser is the wrong place to bake it in: `mirror_roots` resolves
    these where a `CalendarPaths` is at hand.  It used to read a
    process-wide global to answer, which meant parsing a configuration
    file depended on which root had been activated last.
    """
    del account_name
    raw = account.get("mirror_path")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigError(
            f"'mirror_path' must be a string, got {type(raw).__name__}",
            account_key,
        )
    expanded = expand_path(raw)
    if expanded.is_absolute():
        return expanded
    if is_half_qualified(str(expanded)):
        # "/mail" on Windows has a root but no drive, and "C:mail" a
        # drive but no root.  Neither is absolute, so the join below
        # would discard the base and write to C:\mail instead of a
        # directory inside the calendar's root.  Refuse rather than
        # guess, as the mail side does for `mirror.path`.
        raise ConfigError(
            f"'mirror_path' {raw!r} is neither relative nor fully qualified "
            "(a root without a drive, or a drive without a root). Use a "
            "plain relative path, or a complete one.",
            account_key,
        )
    # A relative path belongs to the calendar's root rather than to
    # whatever directory the command was run from, and `mirror_roots`
    # is where that join happens -- with the root it was given.
    return expanded


def mirror_roots(config: AppConfig, paths: CalendarPaths) -> dict[str, Path]:
    """Each account's mirror root, under *paths*.

    What the account declared, resolved against the calendar's root
    when it is relative; the layout's `<root>/mirror/<account>` when it
    declared nothing.  One function because all three callers -- the
    CLI, the host's runtime and `pony doctor` -- were building the same
    mapping with the same comment above it.
    """
    return {account.name: mirror_root(account, paths) for account in config.accounts}


def mirror_root(account: AccountConfig, paths: CalendarPaths) -> Path:
    """One account's mirror root under *paths*."""
    declared = account.mirror_path
    if declared is None:
        return paths.mirror_path(account.name)
    if declared.is_absolute():
        return declared
    return (paths.root / declared).resolve()


def _parse_regex_list(data: object, key: str) -> tuple[re.Pattern[str], ...]:
    if not isinstance(data, list):
        raise ConfigError("must be an array of regex strings", key)
    patterns: list[re.Pattern[str]] = []
    for i, item in enumerate(cast(list[object], data)):
        if not isinstance(item, str):
            raise ConfigError("must be a string", f"{key}[{i}]")
        try:
            patterns.append(re.compile(item))
        except re.error as exc:
            raise ConfigError(f"invalid regex '{item}': {exc}", f"{key}[{i}]") from exc
    return tuple(patterns)


def _require_str(data: dict[str, object], field: str, key: str) -> str:
    if field not in data:
        raise ConfigError(f"missing required field '{field}'", key)
    value = data[field]
    if not isinstance(value, str):
        raise ConfigError(
            f"'{field}' must be a string, got {type(value).__name__}", key
        )
    return value


def _require_int(data: dict[str, object], field: str, key: str) -> int:
    if field not in data:
        raise ConfigError(f"missing required field '{field}'", key)
    value = data[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(
            f"'{field}' must be an integer, got {type(value).__name__}", key
        )
    return value


def _optional_bool(
    data: dict[str, object], field: str, key: str, *, default: bool
) -> bool:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, bool):
        raise ConfigError(
            f"'{field}' must be a boolean, got {type(value).__name__}", key
        )
    return value


def _optional_str(
    data: dict[str, object], field: str, key: str, *, default: str | None
) -> str | None:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, str):
        raise ConfigError(
            f"'{field}' must be a string, got {type(value).__name__}", key
        )
    return value


def _optional_int(
    data: dict[str, object], field: str, key: str, *, default: int
) -> int:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(
            f"'{field}' must be an integer, got {type(value).__name__}", key
        )
    return value


def _toml_str(v: str) -> str:
    return (
        '"'
        + v.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        + '"'
    )


def _toml_scalar(v: str | int | bool) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return _toml_str(v)


def _toml_array(items: list[str]) -> str:
    return "[" + ", ".join(_toml_str(s) for s in items) + "]"


def _toml_inline_table(d: dict[str, object]) -> str:
    def _render(v: object) -> str:
        if isinstance(v, list):
            return _toml_array(cast(list[str], v))
        return _toml_scalar(cast(str | int | bool, v))

    pairs = [f"{k} = {_render(v)}" for k, v in d.items()]
    return "{ " + ", ".join(pairs) + " }"


def _dumps_toml(data: dict[str, object], *, table: str = "") -> str:
    """Render *data* as TOML, optionally nested under a *table*.

    With no *table* this is the standalone file: top-level keys and
    ``[[accounts]]``.  With one it is the same configuration as a table
    inside a larger file -- ``[calendar]`` and ``[[calendar.accounts]]``
    -- which is how the calendar's settings live inside Pony's config.
    One renderer for both, so a key added to the calendar's schema
    cannot appear in one spelling and not the other.
    """
    prefix = f"{table}." if table else ""
    lines: list[str] = []
    if table:
        lines.append(f"[{table}]")
    for k, v in data.items():
        if k == "accounts":
            continue
        # A table inside someone else's file does not carry a schema
        # version: the file has one, at the top, and the host rejects a
        # second one here.
        if table and k == "config_version":
            continue
        lines.append(f"{k} = {_toml_scalar(cast(str | int | bool, v))}")
    for account in cast(list[dict[str, object]], data.get("accounts", [])):
        lines.append("")
        lines.append(f"[[{prefix}accounts]]")
        for k, v in account.items():
            if isinstance(v, dict):
                lines.append(f"{k} = {_toml_inline_table(cast(dict[str, object], v))}")
            elif isinstance(v, list):
                lines.append(f"{k} = {_toml_array(cast(list[str], v))}")
            else:
                lines.append(f"{k} = {_toml_scalar(cast(str | int | bool, v))}")
    return "\n".join(lines) + "\n"


def dumps(config: AppConfig, *, table: str = "") -> str:
    """*config* as TOML text, flat or nested under *table*."""
    return _dumps_toml(dump(config), table=table)


def dump(config: AppConfig) -> dict[str, object]:
    """Serialise AppConfig to a TOML-writeable dict.

    Round-trip invariant: `parse(dump(c)) == c` for any valid config
    (regex patterns round-trip through their source strings; paths round-trip
    through `str(path)` + re-expansion).
    """
    data: dict[str, object] = {
        "config_version": config.config_version,
        "use_utf8": config.use_utf8,
    }
    if config.editor is not None:
        data["editor"] = config.editor
    if config.theme is not None:
        data["theme"] = config.theme
    if not config.background_sync_enabled:
        data["background_sync_enabled"] = False
    if (
        config.background_sync_interval_seconds
        != DEFAULT_BACKGROUND_SYNC_INTERVAL_SECONDS
    ):
        data["background_sync_interval_seconds"] = (
            config.background_sync_interval_seconds
        )
    data["accounts"] = [_dump_account(a) for a in config.accounts]
    return data


def save(config: AppConfig, path: Path) -> None:
    """Write `config` to `path` atomically (temp-file + rename)."""
    write_atomically(path, _dumps_toml(dump(config)).encode("utf-8"), suffix=".toml")


def _dump_account(account: AccountConfig) -> dict[str, object]:
    is_google = isinstance(account.credential, GoogleCredential)
    out: dict[str, object] = {"name": account.name}
    # The "google" backend defaults both fields, so omit them on dump
    # when they are at their default values; keeps round-tripped configs
    # as minimal as the hand-written form.
    if not (is_google and account.url == GOOGLE_CALDAV_URL):
        out["url"] = account.url
    if not (is_google and account.username == ""):
        out["username"] = account.username
    out["credential"] = _dump_credential(account.credential)
    out["trash_retention_days"] = account.trash_retention_days
    out["include"] = [p.pattern for p in account.include]
    out["exclude"] = [p.pattern for p in account.exclude]
    out["read_only"] = [p.pattern for p in account.read_only]
    # Only what the account declared; one that took the default
    # declared nothing and stays clean on round-trip through
    # `pony calendar account add` / `config edit`.  This used to compare the
    # resolved path against a default read from a global, so a dump
    # under one root could write a path belonging to another.
    if account.mirror_path is not None:
        out["mirror_path"] = str(account.mirror_path)
    return out


def _dump_credential(spec: CredentialSpec) -> dict[str, object]:
    if isinstance(spec, PlaintextCredential):
        return {"backend": "plaintext", "password": spec.password}
    if isinstance(spec, EnvCredential):
        return {"backend": "env", "variable": spec.variable}
    if isinstance(spec, CommandCredential):
        return {"backend": "command", "command": list(spec.command)}
    if isinstance(spec, GoogleCredential):
        return {
            "backend": "google",
            "client_id": spec.client_id,
            "client_secret": spec.client_secret,
        }
    # spec narrows to OAuthCredential by elimination.
    out: dict[str, object] = {
        "backend": "oauth",
        "client_id": spec.client_id,
        "client_secret": spec.client_secret,
        "scope": spec.scope,
    }
    if spec.token_path is not None:
        out["token_path"] = str(spec.token_path)
    return out


@dataclass(frozen=True, slots=True)
class ConfigDocument:
    """Where the configuration lives, and how to read and write it.

    Standalone, it is a file of this program's own: the whole file is
    the configuration.  Hosted by Pony, the configuration is one table
    inside a larger file that the mail client also owns, so reading and
    writing it are the host's to define and arrive here as this.

    `label` is how to name it to a user -- the path on its own when the
    file is the configuration, and the table and the path when it is
    not.
    """

    path: Path
    label: str
    load: Callable[[], AppConfig]
    save: Callable[[AppConfig], None]
    # Does this text parse as a configuration?  Raises `ConfigError`.
    # Hosted, the whole file has to parse, not just the calendar's
    # table: an edit that breaks the mail half breaks the program.
    validate: Callable[[Path], None]
    editable_in_place: bool = True


def _parses_or_raises(path: Path) -> None:
    """Raise `ConfigError` unless *path* is a loadable configuration."""
    load(path)


def file_document(path: Path) -> ConfigDocument:
    """The standalone document: this file is the configuration."""
    return ConfigDocument(
        path=path,
        label=str(path),
        load=lambda: load(path),
        save=lambda config: save(config, path),
        validate=_parses_or_raises,
    )
