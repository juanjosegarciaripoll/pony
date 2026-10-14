"""The program's MCP server: every tool it has, over one set of stores.

One server, because it is one program. `pony mcp` serves the mail tools
and -- when a `[calendar]` table is configured -- the calendar's as
well, and the terminal application serves the same set over a loopback
port so a second process can bridge to it instead of opening the same
SQLite indexes again.

`pony calendar mcp` is deliberately not this: it serves the calendar's
tools alone, so an agent given the calendar is not handed the mail
index with it.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING

from tinymcp import McpServer, run_mcp, serve_tcp

from ..calendar_runtime import calendar_paths, open_calendar_runtime
from ..config import load_config
from ..mail.accounts import build_mirrors
from ..mail.index_store import SqliteIndexRepository
from ..paths import resolve
from . import calendar as calendar_tools
from . import mail as mail_tools
from . import state as endpoint_state

if TYPE_CHECKING:
    from pathlib import Path

    from ..mail.protocols import MirrorRepository

SERVER_NAME = "Pony Express"


def build_server(config_path: Path | None = None) -> McpServer:
    """Every tool the program offers, over the stores a run would use.

    The configuration decides where the indexes are, so the paths come
    from it rather than from the platform default -- otherwise a server
    started with `--config` would read a different index than the
    application does.

    A configuration with no `[calendar]` table gets the mail tools and
    nothing else, which is what an install that does not use the
    calendar should see.
    """
    config_file = config_path if config_path is not None else resolve().config_file
    config = load_config(config_file)
    paths = resolve(config_file, data_dir=config.data_dir)
    paths.ensure_runtime_dirs()

    mcp = McpServer(SERVER_NAME)

    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    mirrors: dict[str, MirrorRepository] = build_mirrors(config)
    mail_tools.register_tools(mcp, index=index, mirrors=mirrors, config=config)

    calendar = open_calendar_runtime(calendar_paths(paths), config_file)
    if calendar is not None:
        calendar_tools.register_tools(mcp, index=calendar.index, mirror=calendar.mirror)

    return mcp


async def start_tcp_server(
    config_path: Path | None,
    state_file: Path,
) -> tuple[asyncio.Task[None], endpoint_state.Endpoint]:
    """Serve on a loopback port and publish where, for a bridge to find.

    Binds a random port with a fresh per-session token, publishes both,
    and returns the running task -- cancel it to stop the server -- and
    the endpoint it published.
    """
    token = secrets.token_hex(32)
    port, task = await serve_tcp(build_server(config_path), port=0, token=token)
    endpoint = endpoint_state.Endpoint(port=port, token=token)
    endpoint_state.publish(state_file, endpoint)
    return task, endpoint


def run_server(
    config_path: Path | None = None,
    state_file: Path | None = None,
) -> None:
    """Serve MCP over stdin/stdout, or bridge to an application that is.

    When *state_file* names an endpoint whose port answers, this process
    is a bridge and never opens an index. Otherwise it serves the tools
    itself.
    """
    asyncio.run(_run_server_async(config_path, state_file))


async def _run_server_async(
    config_path: Path | None,
    state_file: Path | None,
) -> None:
    endpoint = endpoint_state.live(state_file) if state_file is not None else None
    remote = (
        (endpoint_state.LOOPBACK_HOST, endpoint.port, endpoint.token)
        if endpoint is not None
        else None
    )
    await run_mcp(
        build_server(config_path),
        remote=remote,
        on_unreachable=(
            None if state_file is None else lambda: endpoint_state.clear(state_file)
        ),
    )


__all__ = [
    "SERVER_NAME",
    "build_server",
    "run_server",
    "start_tcp_server",
]
