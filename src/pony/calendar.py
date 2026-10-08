"""The calendar subsystem's slice of Pony Express's configuration.

Mail and calendar ship as one program, so they read one configuration
file.  Mail keys stay at the top level, where they always were, and the
calendar's own settings live under ``[calendar]`` with
``[[calendar.accounts]]`` holding its CalDAV accounts::

    config_version = 2
    [[accounts]]            # mail
    name = "work"
    ...

    [calendar]
    background_sync_interval_seconds = 3600
    [[calendar.accounts]]   # CalDAV
    name = "personal"
    url = "https://caldav.example.com/dav/"

``use_utf8``, ``editor`` and ``theme`` are read from the top level
unless ``[calendar]`` overrides them: they describe the terminal and the
user, not one subsystem.  There is exactly one ``config_version``, at
the top of the file.

Absent ``[calendar]`` the calendar is simply not configured: this module
returns ``None`` and the mail client runs on its own.

This module stays clear of ``chronos.tui`` so that importing it does not
drag Textual into the CLI and MCP import graphs (see
``tests/test_layering.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from chronos import config as calendar_config
from chronos.credentials import DefaultCredentialsProvider, InteractiveAuthorizer
from chronos.domain import AppConfig as CalendarConfig
from chronos.index_store import SqliteIndexRepository as CalendarIndexRepository
from chronos.paths import default_index_path, user_data_dir
from chronos.storage import VdirMirrorRepository

from .config import ConfigError, read_raw_config
from .paths import AppPaths

CALENDAR_SECTION = "calendar"

# Settings the two subsystems share: taken from the top level of the
# file unless `[calendar]` names its own value.
_INHERITED_KEYS = ("use_utf8", "editor", "theme")

# `chronos.config.parse` requires a schema version of its own.  The
# unified file carries one `config_version`, Pony's, at the top; the
# calendar's slice is always at the current calendar schema.
_CALENDAR_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CalendarRuntime:
    """Everything the calendar needs to run, already opened.

    The repositories are the same ones ``chronos``'s own CLI builds, at
    the same paths, so an existing calendar install keeps its mirror,
    index and OAuth tokens when Pony takes over as the front end.
    """

    config: CalendarConfig
    mirror: VdirMirrorRepository
    index: CalendarIndexRepository
    credentials: DefaultCredentialsProvider

    def close(self) -> None:
        self.index.close()


def calendar_section(raw: object) -> dict[str, object] | None:
    """Return the ``[calendar]`` table of an already-read config, or None."""
    if not isinstance(raw, dict):
        raise ConfigError("top-level config must be an object")
    data = cast("dict[str, object]", raw)
    section = data.get(CALENDAR_SECTION)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(f"'{CALENDAR_SECTION}' must be a table")
    return cast("dict[str, object]", section)


def parse_calendar_config(raw: object) -> CalendarConfig | None:
    """Build the calendar's config from Pony's raw config mapping.

    Returns ``None`` when the file has no ``[calendar]`` table.  Raises
    :class:`pony.config.ConfigError` for a malformed one, with the
    calendar's own message attached so the key it names can be found in
    the same file.
    """
    section = calendar_section(raw)
    if section is None:
        return None
    data = cast("dict[str, object]", raw)

    if "config_version" in section:
        raise ConfigError(
            f"'{CALENDAR_SECTION}.config_version' is not a setting: the "
            "unified config file carries one 'config_version' at the top."
        )

    merged: dict[str, object] = {"config_version": _CALENDAR_SCHEMA_VERSION}
    for key in _INHERITED_KEYS:
        if key in section:
            merged[key] = section[key]
        elif key in data:
            merged[key] = data[key]
    for key, value in section.items():
        if key in _INHERITED_KEYS:
            continue
        merged[key] = value

    try:
        return calendar_config.parse(merged)
    except calendar_config.ConfigError as error:
        raise ConfigError(f"[{CALENDAR_SECTION}] {error}") from error


def load_calendar_config(config_path: Path | None = None) -> CalendarConfig | None:
    """Read Pony's config file and return the calendar's slice of it."""
    path = config_path or AppPaths.default().config_file
    return parse_calendar_config(read_raw_config(path))


def open_calendar_runtime(
    config_path: Path | None = None,
    *,
    interactive_authorizer: InteractiveAuthorizer | None = None,
) -> CalendarRuntime | None:
    """Open the calendar's mirror and index, or None when unconfigured."""
    config = load_calendar_config(config_path)
    if config is None:
        return None
    return CalendarRuntime(
        config=config,
        mirror=VdirMirrorRepository(
            user_data_dir() / "mirror",
            # Honour each account's `mirror_path`. Its default is the
            # same `<root>/<account>` the layout would choose, so an
            # account that sets nothing keeps its files where they are.
            account_roots={a.name: a.mirror_path for a in config.accounts},
        ),
        index=CalendarIndexRepository(default_index_path()),
        credentials=DefaultCredentialsProvider(
            interactive_authorizer=interactive_authorizer
        ),
    )


__all__ = [
    "CALENDAR_SECTION",
    "CalendarConfig",
    "CalendarRuntime",
    "calendar_section",
    "load_calendar_config",
    "open_calendar_runtime",
    "parse_calendar_config",
]
