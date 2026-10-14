"""The MCP front end: the program's tools, over the same models.

A front end like the terminal and the browser, not a library for them:
`mail` and `calendar` register the tools each half offers, `state`
publishes the endpoint a running application listens on so a second
process can bridge to it instead of opening the index twice.

Reached with `pony mcp` and `pony calendar mcp`, and started inside the
terminal application, which publishes its port and token.
"""

from __future__ import annotations

__all__: list[str] = []
