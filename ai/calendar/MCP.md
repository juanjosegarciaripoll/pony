# MCP.md

Current MCP architecture.

## Modes

- `pony mcp` serves **the program's** tools: the mail half's and, when a
  `[calendar]` table is configured, the calendar's, on one server
  (`pony.mcp.server`).
- `pony mcp` in stdio-to-TCP bridge mode when a running application has
  published its endpoint and its port answers.
- TCP server started by the terminal application on `127.0.0.1` with a
  per-session auth token, published in `<data_dir>/mcp.json`.
- `pony calendar mcp` serves the calendar's tools **alone**, over stdio,
  and never bridges: an agent given the calendar is not handed the mail
  index with it.

## Current tools

- `list_calendars`
- `query_range`
- `search_events`
- `get_event`
- `get_todo`
- `import_ics`

## Policy

- MCP can query local calendar state.
- MCP can add data through `.ics` import.
- MCP must not expose destructive tools.

## Transport notes

- The endpoint file carries the TCP port and the auth token, and is
  written owner-only through `pony.atomic`.
- Bridge mode exists to avoid opening the same local state from
  competing processes. The port is probed before bridging, because an
  application that was killed leaves its file behind.
- Implementation uses standard `asyncio` plus `tinymcp`; no web server
  stack is involved.
