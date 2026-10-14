"""The endpoint a running application publishes for a bridge to find.

An application that is already serving MCP over a loopback TCP port
writes the port and a per-session token here; `pony mcp` and
`pony calendar mcp` read it and bridge stdin/stdout to that port
instead of opening the same SQLite index a second time.

Both halves had their own copy of this -- the same JSON, the same
fields, the same atomic write -- and the copies had drifted: one caught
`OSError` from an unreadable file and the other did not, one swallowed
every exception on removal, and only one checked that the port still
answers before handing a caller a dead endpoint.

The token authorises read access to the whole index, so the file is
written `owner_only`: never readable by anyone else, not even for the
instant before a `chmod`.
"""

from __future__ import annotations

import contextlib
import json
import socket
from dataclasses import dataclass
from pathlib import Path

from pony.atomic import write_atomically

#: How long to wait for the published port to accept a connection.
REACHABLE_TIMEOUT_SECONDS = 0.1

LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where a running application's MCP server listens, and the token for it."""

    port: int
    token: str


def publish(state_file: Path, endpoint: Endpoint) -> None:
    """Write *endpoint* where a bridge will look for it."""
    payload = json.dumps({"port": endpoint.port, "token": endpoint.token}).encode()
    write_atomically(state_file, payload, suffix=".json", owner_only=True)


def read(state_file: Path) -> Endpoint | None:
    """The published endpoint, or `None` if there is none to be had.

    Anything wrong with the file -- missing, truncated, unreadable, a
    directory, written by a newer version -- reads as "no endpoint",
    because the caller's fallback is to serve the tools itself, which
    always works.
    """
    try:
        data = json.loads(state_file.read_bytes())
        return Endpoint(port=int(data["port"]), token=str(data["token"]))
    except (OSError, KeyError, ValueError, TypeError):
        return None


def clear(state_file: Path) -> None:
    """Remove the published endpoint; silent when there is nothing to remove."""
    with contextlib.suppress(OSError):
        state_file.unlink()


def live(
    state_file: Path, *, timeout: float = REACHABLE_TIMEOUT_SECONDS
) -> Endpoint | None:
    """The published endpoint if its port answers, else `None`.

    A state file outlives an application that was killed rather than
    closed, and a bridge that trusts it connects to a port nobody is
    listening on.  So the port is probed, and a stale file is removed on
    the way past: the next reader does not pay for the same timeout.
    """
    endpoint = read(state_file)
    if endpoint is None:
        return None
    try:
        with socket.create_connection((LOOPBACK_HOST, endpoint.port), timeout=timeout):
            return endpoint
    except OSError:
        clear(state_file)
        return None


__all__ = [
    "Endpoint",
    "LOOPBACK_HOST",
    "REACHABLE_TIMEOUT_SECONDS",
    "clear",
    "live",
    "publish",
    "read",
]
