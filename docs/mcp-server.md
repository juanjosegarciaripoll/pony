---
title: MCP Server
---

# MCP Server

Pony Express ships a built-in [Model Context Protocol](https://modelcontextprotocol.io/)
server. MCP is an open JSON-RPC standard that lets external programs call
tools exposed by another application; running `pony mcp` makes Pony's
read-only mail and contact tools available to any MCP-speaking client.

The server is **read-only**. It never modifies mail, flags, contacts, or the
index. It works entirely against the local mirror and SQLite index that
`pony sync` maintains; no IMAP connection is opened while the MCP server is
running.

## Architecture

There is a single command — `pony mcp` — and a single transport — stdio
(newline-delimited JSON-RPC 2.0). The behaviour switches automatically
depending on whether `pony tui` is already running:

| Situation | What `pony mcp` does |
|---|---|
| TUI is **not** running | Opens its own SQLite handle and serves tools directly over stdio. |
| TUI **is** running | Reads the auth token from the TUI's state file, opens a TCP socket to the loopback server the TUI is hosting, and proxies stdin/stdout ↔ TCP. |

The bridge mode exists because SQLite refuses concurrent writers from
different processes. When the TUI is running it owns the database and
exposes the MCP tools over a per-session TCP server bound to `127.0.0.1`
behind a one-time random token written to a state file under the data
directory. A second `pony mcp` invocation never opens its own SQLite
handle in that case — it simply forwards JSON-RPC frames to the running
TUI and back. When the TUI exits, the state file is removed and the next
`pony mcp` falls back to stdio-direct mode.

There is no HTTP transport and no separate `--port` flag. MCP clients
always speak stdio to `pony mcp`; the bridge is invisible to them.

## Setup

Most MCP clients launch the server as a child process and talk to it over
stdio. The exact configuration file varies by client; the command line is
always the same:

```
pony mcp
```

If `pony` is not on `PATH` (for example you run it via `uv tool`), use the
absolute path to the executable, e.g. `~/.local/bin/pony` on Linux/macOS or
`%LOCALAPPDATA%\uv\tools\pony\Scripts\pony.exe` on Windows.

A typical client config entry looks like:

```json
{
  "mcpServers": {
    "pony": {
      "command": "pony",
      "args": ["mcp"]
    }
  }
}
```

After saving the config, restart the client. Pony's tools should then
appear in its tool list.

## Available tools

### `search_messages`

Full-text search across the local index. Returns message metadata (sender,
subject, date, preview); use `get_message_body` for the full body.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | `""` | Bare-word search across all fields |
| `from_address` | string | `""` | Filter by sender address |
| `to_address` | string | `""` | Filter by recipient address |
| `subject` | string | `""` | Match against subject line |
| `body` | string | `""` | Match against body preview |
| `account` | string | all | Restrict to one account name |
| `case_sensitive` | bool | `false` | Enable case-sensitive matching |
| `limit` | int | `50` | Maximum number of results |

At least one field must be non-empty; otherwise all indexed messages are
returned up to `limit`.

---

### `list_folders`

Return all accounts and their folders. Each folder entry includes
`message_count`, `highest_uid`, and `synced_at` so a client can decide
whether the local index is fresh enough to query.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `account` | string | all | Restrict to one account name |

---

### `list_messages`

List messages in a specific folder. Returns the same metadata shape as
`search_messages` (no body text).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `account` | string | — | Account name (required) |
| `folder` | string | — | Folder name (required) |
| `limit` | int | `100` | Maximum number of results |

---

### `get_message`

Retrieve the index metadata for a single message identified by its
`Message-ID`. Includes the same `attachments` array as `get_message_body`
when the mirror holds the bytes, so a client can discover what's available
without pulling the full body.

| Parameter | Type | Description |
|---|---|---|
| `account` | string | Account name |
| `folder` | string | Folder name |
| `message_id` | string | The `Message-ID` header value |

Returns `null` if the message is not in the index.

---

### `get_message_body`

Read the full plain-text body of a message from the local mirror. HTML-only
messages are converted to plain text (style and script blocks stripped).
Attachment metadata is included; attachment bytes are not.

| Parameter | Type | Description |
|---|---|---|
| `account` | string | Account name |
| `folder` | string | Folder name |
| `message_id` | string | The `Message-ID` header value |

Returns a dict with keys: `subject`, `from`, `to`, `cc`, `date`, `body`
(plain text string), `attachments` (list of `{index, filename,
content_type, size_bytes}`). Returns `null` if the message is not in the
local mirror.

---

### `get_attachment`

Return one attachment's bytes. `data_base64` is always present
(transport-safe). For text attachments a decoded `text` field is included
as a convenience.

| Parameter | Type | Description |
|---|---|---|
| `account` | string | Account name |
| `folder` | string | Folder name |
| `message_id` | string | The `Message-ID` header value |
| `index` | int | Attachment index from `get_message_body` |

---

### `search_contacts`

Search the contacts store by name or email-address prefix. Case-insensitive,
prefix-based, ranked by message count.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `prefix` | string | — | Name or email prefix to search (required) |
| `limit` | int | `20` | Maximum number of results |

---

### `get_sync_status`

Return the last-sync timestamp and highest IMAP UID seen for each folder.
Useful for checking how fresh the local index is before doing a search.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `account` | string | all | Restrict to one account name |
