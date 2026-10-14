"""The calendar subsystem's slice of Pony Express's configuration.

Mail and calendar ship as one program, so they read one configuration
file.  Mail keys stay at the top level, where they always were, and the
calendar's own settings live under ``[calendar]`` with
``[[calendar.accounts]]`` holding its CalDAV accounts::

    config_version = {CONFIG_VERSION}
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

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from chronos import config as calendar_config
from chronos.credentials import DefaultCredentialsProvider, InteractiveAuthorizer
from chronos.domain import CALENDAR_CONFIG_VERSION
from chronos.domain import AppConfig as CalendarConfig
from chronos.index_store import SqliteIndexRepository as CalendarIndexRepository
from chronos.paths import CalendarPaths
from chronos.storage import VdirMirrorRepository

from .config import ConfigError, load_config, read_raw_config
from .domain import CONFIG_VERSION

if TYPE_CHECKING:
    from chronos.cli import ConfigDocument
from .paths import AppPaths, resolve

# The example above quotes the schema version the loader actually
# accepts rather than a copy of it.  The copy it used to hold said 2
# after `CONFIG_VERSION` went to 3, so the file this module documented
# was one `read_raw_config` refuses.
if __doc__ is not None:
    __doc__ = __doc__.replace("{CONFIG_VERSION}", str(CONFIG_VERSION))

CALENDAR_SECTION = "calendar"

# Settings the two subsystems share: taken from the top level of the
# file unless `[calendar]` names its own value.
#
# The two sync keys are here because they describe the program, not a
# subsystem: the halves default them oppositely (mail off every 600 s,
# the calendar on every 3600 s), so `background_sync_enabled = false`
# at the top of the file left the calendar polling CalDAV every hour --
# which is the one setting a user turns off on purpose, on a metered
# connection or a laptop on battery.
_INHERITED_KEYS = (
    "use_utf8",
    "editor",
    "theme",
    "background_sync_enabled",
    "background_sync_interval_seconds",
)

# `chronos.config.parse` requires a schema version of its own.  The
# unified file carries one `config_version`, Pony's, at the top; the
# calendar's slice is always at the current calendar schema.


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
    #: Where the calendar's files are, so everything the host builds
    #: out of this runtime -- a CLI context, the TUI's services -- is
    #: pointed at the same root rather than at a module global.
    paths: CalendarPaths

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

    merged: dict[str, object] = {"config_version": CALENDAR_CONFIG_VERSION}
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
    path = config_path if config_path is not None else resolve().config_file
    return parse_calendar_config(read_raw_config(path))


def save_calendar_config(config: CalendarConfig, path: Path) -> None:
    """Write *config* back as the ``[calendar]`` table of *path*.

    Everything outside the table is kept byte for byte, so the mail
    half of the file -- and its comments -- survives a calendar edit.
    The table itself is re-rendered from the parsed configuration, so
    comments *inside* it do not survive; that is the cost of writing it
    from a model rather than patching text.

    Written through a temporary file and renamed, because this file is
    the one thing a user cannot lose: a half-written config is a client
    that will not start.
    """
    rendered = calendar_config.dumps(config, table=CALENDAR_SECTION).rstrip("\n")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    body = _replace_calendar_table(existing, rendered)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _replace_calendar_table(text: str, rendered: str) -> str:
    """*text* with its ``[calendar]`` span replaced by *rendered*.

    The span runs from the ``[calendar]`` header to the next top-level
    table header that is not part of it, which is what makes
    ``[[calendar.accounts]]`` belong to the calendar and ``[[accounts]]``
    belong to the mail half.  With no table there, the rendering is
    appended.
    """
    lines = text.splitlines()
    start: int | None = None
    end = len(lines)
    for number, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("["):
            continue
        header = stripped.strip("[]").strip()
        belongs = header == CALENDAR_SECTION or header.startswith(
            f"{CALENDAR_SECTION}."
        )
        if belongs and start is None:
            start = number
        elif not belongs and start is not None:
            end = number
            break
    if start is None:
        prefix = text.rstrip("\n")
        joiner = "\n\n" if prefix else ""
        return f"{prefix}{joiner}{rendered}\n"
    kept_before = lines[:start]
    kept_after = lines[end:]
    # Blank lines immediately above the table belong to whatever is
    # before it, and a trailing run inside the span is ours to drop.
    while kept_before and not kept_before[-1].strip():
        kept_before.pop()
    out = [*kept_before]
    if out:
        out.append("")
    out.extend(rendered.splitlines())
    if kept_after:
        out.append("")
        out.extend(kept_after)
    return "\n".join(out).rstrip("\n") + "\n"


def hosted_calendar_document(path: Path) -> ConfigDocument:
    """The calendar's configuration as the `[calendar]` table of *path*.

    The calendar's own commands write a configuration; inside Pony the
    configuration is a table in a file the mail client owns too, so
    they are given the reading and the writing of that table rather
    than a file of their own to write.  Without this they wrote a
    standalone file nothing here reads, and reported success.
    """
    from chronos.cli import ConfigDocument as _Document

    def _load() -> CalendarConfig:
        config = parse_calendar_config(read_raw_config(path))
        if config is not None:
            return config
        # No `[calendar]` table yet: an empty configuration, which is
        # what `account add` then adds its first account to.
        return CalendarConfig(
            config_version=CALENDAR_CONFIG_VERSION,
            use_utf8=True,
            editor=None,
            accounts=(),
        )

    def _validate(candidate: Path) -> None:
        # The whole file, not just the table: an edit that breaks the
        # mail half breaks the program just as thoroughly.
        load_config(candidate)

    return _Document(
        path=path,
        label=f"the [{CALENDAR_SECTION}] table in {path}",
        load=_load,
        save=lambda config: save_calendar_config(config, path),
        validate=_validate,
        editable_in_place=False,
    )


def calendar_paths(paths: AppPaths) -> CalendarPaths:
    """The calendar's layout inside the host's data directory.

    One function, so every entry point that opens the calendar points
    it at the same root: ``pony tui``, ``pony web``, ``pony calendar
    ...`` and the MCP tools all used to depend instead on
    ``chronos.paths.activate`` having been called at startup, and on
    nothing having called it again since.
    """
    return CalendarPaths(root=paths.calendar_dir)


def open_calendar_runtime(
    paths: CalendarPaths,
    config_path: Path | None = None,
    *,
    interactive_authorizer: InteractiveAuthorizer | None = None,
) -> CalendarRuntime | None:
    """Open the calendar's mirror and index, or None when unconfigured.

    *paths* is the root the host hands over -- `AppPaths.calendar_dir`
    inside the shared data directory -- and it travels on the runtime,
    so nothing downstream has to ask a global where the calendar is.
    """
    config = load_calendar_config(config_path)
    if config is None:
        return None
    return CalendarRuntime(
        config=config,
        mirror=VdirMirrorRepository(
            paths.mirror_dir,
            # Honour each account's declared `mirror_path`; one that
            # declared none takes the layout's `<root>/<account>`.
            account_roots=calendar_config.mirror_roots(config, paths),
        ),
        index=CalendarIndexRepository(paths.index_file),
        credentials=DefaultCredentialsProvider(
            interactive_authorizer=interactive_authorizer, paths=paths
        ),
        paths=paths,
    )


__all__ = [
    "CALENDAR_SECTION",
    "CalendarConfig",
    "CalendarRuntime",
    "calendar_paths",
    "calendar_section",
    "hosted_calendar_document",
    "save_calendar_config",
    "load_calendar_config",
    "open_calendar_runtime",
    "parse_calendar_config",
]
