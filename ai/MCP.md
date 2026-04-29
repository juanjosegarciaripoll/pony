# MCP.md

Desired MCP architecture.

## Modes

- `pony mcp` in self-contained stdio mode when no running app instance is detected.
- `pony mcp` in stdio-to-TCP bridge mode when a running TUI instance has published MCP server state.
- TCP server started by the TUI on `127.0.0.1` with a per-session auth token stored in the MCP state file.

## Policy

- MCP provides full read-only access to email.
- Read operations and summaries aim for economy (e.g. by providing date ranges)
- MCP provides access to writing drafts in the Draft folder of the selected account.

## Transport notes

- State file carries TCP port and auth token.
- Bridge mode exists to avoid opening the same local state from competing processes.
- Implementation uses standard `asyncio` plus the MCP SDK; no web server stack is involved.
