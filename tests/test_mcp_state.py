"""The endpoint a running application publishes for a bridge to find.

One record for both halves of the program. Each had its own copy, and
they had drifted: one read a file it could not open as "no endpoint" and
the other let the `OSError` out, one swallowed every exception on
removal, and only one probed the port before handing a caller an
endpoint that nobody is listening on.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path

from pony.mcp.state import Endpoint, clear, live, publish, read


class EndpointFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_file = self._tmp / "mcp.json"

    def test_write_and_read_round_trip(self) -> None:
        endpoint = Endpoint(port=12345, token="abc123")
        publish(self.state_file, endpoint)
        self.assertEqual(read(self.state_file), endpoint)

    def test_the_file_holds_the_port_and_the_token(self) -> None:
        publish(self.state_file, Endpoint(port=9876, token="secret"))
        raw = json.loads(self.state_file.read_bytes())
        self.assertEqual(raw, {"port": 9876, "token": "secret"})

    def test_the_token_is_owner_only_and_never_half_written(self) -> None:
        """It authorises read access to a whole index.

        `write_text` created it at the ambient umask -- world-readable
        on most systems -- and a reader that arrived between the open
        and the flush saw a truncated file. `pony.atomic` does
        mkstemp -> fsync -> chmod 0600 -> replace instead, so the file
        is never found half written and never readable by anyone else,
        not even for the instant before the chmod.
        """
        publish(self.state_file, Endpoint(port=9876, token="secret"))

        if os.name != "nt":
            mode = stat.S_IMODE(self.state_file.stat().st_mode)
            self.assertEqual(mode, 0o600, oct(mode))
        leftovers = [
            path.name for path in self._tmp.iterdir() if path.name != "mcp.json"
        ]
        self.assertEqual(leftovers, [])

    def test_reading_nothing_is_no_endpoint(self) -> None:
        self.assertIsNone(read(self.state_file))

    def test_reading_rubbish_is_no_endpoint(self) -> None:
        self.state_file.write_bytes(b"not json {{{")
        self.assertIsNone(read(self.state_file))

    def test_a_file_that_cannot_be_opened_is_no_endpoint(self) -> None:
        """A directory where the file should be, or one we may not read.

        Both halves meant to treat this as "serve the tools yourself",
        and one of them raised instead.
        """
        self.state_file.mkdir()
        self.assertIsNone(read(self.state_file))

    def test_clearing_nothing_is_silent(self) -> None:
        clear(self.state_file)  # must not raise

    def test_clearing_removes_the_file(self) -> None:
        publish(self.state_file, Endpoint(port=1, token="t"))
        clear(self.state_file)
        self.assertIsNone(read(self.state_file))


class LiveEndpointTest(unittest.TestCase):
    """`live` is `read` plus the question the callers actually have."""

    def setUp(self) -> None:
        self._tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_file = self._tmp / "mcp.json"

    def test_no_file_is_no_endpoint(self) -> None:
        self.assertIsNone(live(self.state_file))

    def test_a_port_that_answers_is_the_endpoint(self) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            publish(self.state_file, Endpoint(port=port, token="tok"))
            self.assertEqual(live(self.state_file), Endpoint(port=port, token="tok"))

    def test_a_dead_port_is_no_endpoint_and_the_file_goes(self) -> None:
        """An application that was killed leaves its file behind.

        A bridge that trusts it connects to nobody; `pony calendar
        reset` refuses to run because it believes the index is held.
        """
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        publish(self.state_file, Endpoint(port=dead_port, token="tok"))

        self.assertIsNone(live(self.state_file))
        self.assertFalse(self.state_file.exists())
