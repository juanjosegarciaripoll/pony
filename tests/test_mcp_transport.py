"""The calendar's MCP transport: a TCP server, and bridge-or-stdio.

TCP auth, bridge forwarding and the bridge-or-standalone decision are
tinymcp's own tests. What is covered here is this program's half:
`start_tcp_server` publishes an endpoint and serves authenticated MCP
over it, and `run_mcp_stdio` finds that endpoint and bridges to it, or
serves the tools itself when there is none.

The endpoint file itself is `tests/test_mcp_state.py`.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
import unittest
from pathlib import Path

from tinymcp import serve_tcp

from pony.calendar.index_store import SqliteIndexRepository
from pony.calendar.storage import VdirMirrorRepository
from pony.mcp.state import Endpoint, read

# ---------------------------------------------------------------------------
# start_tcp_server + run_mcp_stdio integration tests
# ---------------------------------------------------------------------------


class TcpTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.addCleanup(self.index.close)

    def _server_and_token(self) -> tuple[object, str]:
        from pony.mcp.calendar import build_mcp_server

        return (
            build_mcp_server(index=self.index, mirror=self.mirror),
            secrets.token_hex(16),
        )

    async def _start_tcp(
        self, server: object, token: str
    ) -> tuple[asyncio.Task[None], int]:
        port, task = await serve_tcp(server, port=0, token=token)  # type: ignore[arg-type]
        return task, port


class StartTcpServerTest(TcpTestCase):
    """start_tcp_server writes the state file; run_mcp_stdio routes via it."""

    def setUp(self) -> None:
        super().setUp()
        self.state_file = (
            Path(self.enterContext(tempfile.TemporaryDirectory())) / "mcp_server.json"
        )

    async def _wait_for_state(self) -> Endpoint:
        for _ in range(40):
            state = read(self.state_file)
            if state is not None:
                return state
            await asyncio.sleep(0.05)
        self.fail("state file not written within 2 s")

    async def test_server_writes_state_file_and_accepts_connections(self) -> None:
        from pony.mcp.calendar import start_tcp_server

        task = asyncio.create_task(
            start_tcp_server(
                index=self.index, mirror=self.mirror, state_file=self.state_file
            )
        )
        try:
            state = await self._wait_for_state()
            self.assertGreater(state.port, 0)
            self.assertTrue(state.token)

            reader, writer = await asyncio.open_connection("127.0.0.1", state.port)
            writer.write(json.dumps({"auth": state.token}).encode() + b"\n")
            await writer.drain()
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "state-test", "version": "0"},
                },
            }
            writer.write(json.dumps(init).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            resp = json.loads(line)
            self.assertEqual(resp.get("id"), 1)
            self.assertIn("result", resp)
            writer.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertIsNone(read(self.state_file))

    async def test_run_mcp_stdio_without_state_goes_standalone(self) -> None:
        """No state file → run_mcp_stdio uses standalone mode directly."""
        from unittest.mock import AsyncMock, patch

        from pony.mcp.calendar import run_mcp_stdio

        standalone = AsyncMock()
        with patch("tinymcp.transport.run_stdio_standalone", standalone):
            await run_mcp_stdio(
                index=self.index, mirror=self.mirror, state_file=self.state_file
            )
        standalone.assert_awaited_once()

    async def test_run_mcp_stdio_bridges_to_running_server(self) -> None:
        """run_mcp_stdio detects the state file and delegates to the bridge."""
        from unittest.mock import patch

        from pony.mcp.calendar import run_mcp_stdio, start_tcp_server

        task = asyncio.create_task(
            start_tcp_server(
                index=self.index, mirror=self.mirror, state_file=self.state_file
            )
        )
        try:
            state = await self._wait_for_state()

            bridged_port: int | None = None
            bridged_token: str | None = None

            async def _capture_bridge(*, host: str, port: int, token: str) -> None:  # noqa: ARG001
                nonlocal bridged_port, bridged_token
                bridged_port = port
                bridged_token = token

            with patch("tinymcp.transport.run_stdio_bridge", _capture_bridge):
                await run_mcp_stdio(
                    index=self.index, mirror=self.mirror, state_file=self.state_file
                )

            self.assertEqual(bridged_port, state.port)
            self.assertEqual(bridged_token, state.token)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
