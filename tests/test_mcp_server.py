"""The program's MCP server: one server, both halves' tools, one endpoint.

TCP auth, bridge forwarding and the bridge-or-standalone decision are
tinymcp's own tests. What is covered here is this program's half: which
tools go on the server, that a running application publishes where it
listens, and that a second process bridges to a live endpoint rather
than to a file that outlived the application that wrote it.

The endpoint file itself is `tests/test_mcp_state.py`.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pony.mcp.server as mcp_server
from pony.calendar.index_store import SqliteIndexRepository as CalendarIndex
from pony.calendar.storage import VdirMirrorRepository
from pony.mcp.state import Endpoint, publish, read

if TYPE_CHECKING:
    from tinymcp import McpServer


def _tool_names(server: McpServer) -> set[str]:
    """What the server would advertise, without driving a session for it."""
    return {tool.name for tool in server._tools}  # pyright: ignore[reportPrivateUsage]


class CalendarStoresMixin(unittest.TestCase):
    """A calendar index and mirror in a directory of their own."""

    def setUp(self) -> None:
        super().setUp()
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = CalendarIndex(tmp / "index.sqlite3")
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.addCleanup(self.index.close)

    def _calendar_server(self) -> McpServer:
        from pony.mcp.calendar import build_mcp_server

        return build_mcp_server(index=self.index, mirror=self.mirror)


class BothHalvesOnOneServerTest(CalendarStoresMixin):
    """`pony mcp` serves the whole program, not the mail half of it.

    The calendar's tools used to be reachable only through
    `pony calendar mcp`, so an agent talking to a running application
    could read mail and knew nothing about the calendar in the same
    process.
    """

    def _build(self, *, calendar: object | None) -> McpServer:
        from pony.mail.domain import AppConfig

        paths = mcp_server.resolve(None)
        with (
            patch.object(
                mcp_server, "load_config", return_value=AppConfig(accounts=())
            ),
            patch.object(mcp_server, "resolve", return_value=paths),
            patch.object(
                mcp_server, "open_calendar_runtime", return_value=calendar
            ) as opened,
        ):
            server = mcp_server.build_server(None)
        self.opened = opened
        return server

    def test_an_unconfigured_calendar_leaves_the_mail_tools_alone(self) -> None:
        names = _tool_names(self._build(calendar=None))

        self.assertIn("search_messages", names)
        self.assertNotIn("search_events", names)

    def test_a_configured_calendar_puts_its_tools_on_the_same_server(self) -> None:
        class _Runtime:
            def __init__(self, index: object, mirror: object) -> None:
                self.index = index
                self.mirror = mirror

        names = _tool_names(self._build(calendar=_Runtime(self.index, self.mirror)))

        self.assertIn("search_messages", names)
        self.assertIn("search_events", names)
        self.assertIn("import_ics", names)

    def test_the_calendar_is_opened_under_the_run_s_own_root(self) -> None:
        """Not the platform default: a `--config` run reads its own stores."""
        self._build(calendar=None)

        (layout, config_file), _ = self.opened.call_args
        self.assertTrue(str(layout.root).endswith("calendar"))
        self.assertEqual(config_file, mcp_server.resolve(None).config_file)


class PublishedEndpointTest(CalendarStoresMixin, unittest.IsolatedAsyncioTestCase):
    """What a running application publishes, and what it serves on it."""

    def setUp(self) -> None:
        super().setUp()
        self.state_file = (
            Path(self.enterContext(tempfile.TemporaryDirectory())) / "mcp.json"
        )

    async def _wait_for_endpoint(self) -> Endpoint:
        for _ in range(40):
            endpoint = read(self.state_file)
            if endpoint is not None:
                return endpoint
            await asyncio.sleep(0.05)
        self.fail("endpoint not published within 2 s")

    async def test_the_endpoint_is_published_and_answers_the_token(self) -> None:
        with patch.object(
            mcp_server, "build_server", return_value=self._calendar_server()
        ):
            task, endpoint = await mcp_server.start_tcp_server(None, self.state_file)
        try:
            self.assertEqual(await self._wait_for_endpoint(), endpoint)
            self.assertGreater(endpoint.port, 0)
            self.assertTrue(endpoint.token)

            reader, writer = await asyncio.open_connection("127.0.0.1", endpoint.port)
            writer.write(json.dumps({"auth": endpoint.token}).encode() + b"\n")
            writer.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "endpoint-test", "version": "0"},
                        },
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            answer = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
            writer.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(answer.get("id"), 1)
        self.assertIn("result", answer)


class BridgeOrServeTest(CalendarStoresMixin):
    """`pony mcp` bridges to a running application, or serves the tools itself.

    The decision used to be made on the endpoint file *existing*: an
    application that was killed rather than closed leaves its file
    behind, and the bridge then connected to a port nobody is listening
    on instead of falling back. The endpoint is probed now, and a stale
    file is cleared on the way past.
    """

    def setUp(self) -> None:
        super().setUp()
        self.state_file = (
            Path(self.enterContext(tempfile.TemporaryDirectory())) / "mcp.json"
        )

    def _remote(self) -> object:
        with (
            patch.object(
                mcp_server, "build_server", return_value=self._calendar_server()
            ),
            patch.object(mcp_server, "run_mcp", new_callable=AsyncMock) as served,
        ):
            asyncio.run(mcp_server._run_server_async(None, self.state_file))  # pyright: ignore[reportPrivateUsage]
        return served.await_args.kwargs["remote"] if served.await_args else None

    def test_no_endpoint_serves_the_tools_here(self) -> None:
        self.assertIsNone(self._remote())

    def test_a_live_endpoint_is_bridged_to(self) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            publish(self.state_file, Endpoint(port=port, token="tok"))

            self.assertEqual(self._remote(), ("127.0.0.1", port, "tok"))

    def test_an_endpoint_nobody_answers_is_not_bridged_to(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        publish(self.state_file, Endpoint(port=dead_port, token="tok"))

        self.assertIsNone(self._remote())
        self.assertFalse(self.state_file.exists())


class CalendarOnlyServerTest(CalendarStoresMixin, unittest.IsolatedAsyncioTestCase):
    """`pony calendar mcp` hands over the calendar and nothing else.

    It does not bridge to a running application either: that
    application serves the program's whole tool set, and an agent given
    the calendar is not to be handed the mail index with it.
    """

    async def test_it_serves_the_calendar_tools_and_no_mail_ones(self) -> None:
        from pony.mcp.calendar import run_mcp_stdio

        with patch("tinymcp.run_mcp", new_callable=AsyncMock) as served:
            await run_mcp_stdio(index=self.index, mirror=self.mirror)

        served.assert_awaited_once()
        call = served.await_args
        assert call is not None
        names = _tool_names(call.args[0])
        self.assertIn("search_events", names)
        self.assertNotIn("search_messages", names)
        # No `remote=`: it never bridges to the program's server.
        self.assertEqual(call.kwargs, {})
