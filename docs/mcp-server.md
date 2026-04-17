---
title: MCP Server
---

# MCP Server

Pony Express includes a built-in [Model Context Protocol](https://modelcontextprotocol.io/)
(MCP) server.  MCP is an open standard that lets AI assistants call tools
provided by external applications.  By running `pony mcp-server`, you give an
AI assistant — such as Claude — the ability to search, list, and read your
local mail and contacts directly, without you having to copy-paste content into
the chat window.

The server exposes **read-only** tools only.  It never modifies your mail,
flags, contacts, or index.  It works entirely against the local mirror and
SQLite index that `pony sync` keeps up to date; no IMAP connection is made
while the MCP server is running.

## Modes at a glance

There are three ways to run the MCP server, depending on whether you also use
the TUI and whether the AI client is on the same machine:

| Mode | How to start | Transport | Can run with TUI? |
|---|---|---|---|
| **Embedded** (auto-start) | Add `[mcp]` to `config.toml` | HTTP | Yes — same process |
| **Standalone stdio** | `pony mcp-server` | stdio | No — conflicts with TUI |
| **Standalone HTTP** | `pony mcp-server --port N` | HTTP | Yes — separate process |

### Embedded mode — MCP starts with the TUI

This is the recommended setup if you normally work with `pony tui` open.
Add an `[mcp]` section to your `config.toml`:

```toml
[mcp]
host = "127.0.0.1"
port = 8765
```

When you run `pony tui`, the MCP HTTP server starts automatically in a
background thread.  A notification appears in the TUI showing the URL.
The server stops when you quit the TUI.

**What this means in practice:**

- You use `pony tui` as normal — reading, writing, syncing mail.
- At the same time, Claude (or any other MCP client) can call the Pony tools
  to search and read your mail on your behalf.
- No second terminal needed; no separate process to manage.
- The MCP server is read-only, so it never interferes with what the TUI writes.

!!! note "Why HTTP and not stdio in embedded mode?"
    stdio transport takes over the process's standard input and output —
    the same channels the TUI uses to draw its interface.  The embedded server
    therefore always uses HTTP, which works over a network socket and has no
    conflict with the TUI.

Register the embedded server with Claude Code once, then forget about it:

```bash
claude mcp add --transport http pony http://127.0.0.1:8765/mcp
```

---

### Standalone stdio — for Claude Desktop and Claude Code without the TUI

Use this when you do **not** run `pony tui` at the same time (for example,
you only use Pony via the CLI or `pony sync`).  The AI client starts Pony as a
child process and communicates over stdin/stdout.

```bash
pony mcp-server          # listens on stdio
```

Because stdio transport occupies the terminal, **you cannot run `pony tui` in
the same session**.  If you want both the TUI and MCP access at the same time,
use embedded mode or standalone HTTP instead.

---

### Standalone HTTP — always-on or remote server

Use this when you want the MCP server to run independently of both the TUI and
any particular AI client session — for example in Docker, on a remote machine,
or if you want it always running in the background.

```bash
pony mcp-server --port 8765              # localhost only
pony mcp-server --host 0.0.0.0 --port 8765   # all interfaces
```

This mode **can** run alongside `pony tui` — they are separate processes that
share the SQLite database read-only, so there are no write conflicts.

---

## Setup: Claude Desktop (stdio)

Claude Desktop reads a JSON configuration file and starts each MCP server
as a child process when needed.

**Configuration file location:**

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

Open (or create) that file and add a `mcpServers` section:

```json
{
  "mcpServers": {
    "pony": {
      "command": "pony",
      "args": ["mcp-server"]
    }
  }
}
```

If `pony` is not on your `PATH` (e.g. you run it via `uv`), use the full path
to the executable:

```json
{
  "mcpServers": {
    "pony": {
      "command": "/home/you/.local/bin/pony",
      "args": ["mcp-server"]
    }
  }
}
```

Restart Claude Desktop after saving.  The Pony Express tools will appear in
the tools panel.

---

## Setup: Claude Code (stdio)

From the terminal, run:

```bash
claude mcp add pony -- pony mcp-server
```

This registers Pony as a project-scoped MCP server for the current working
directory.  To register it globally (available in every project):

```bash
claude mcp add --scope user pony -- pony mcp-server
```

Verify it was added:

```bash
claude mcp list
```

---

## Setup: Claude Code (HTTP)

If you have Pony running as an HTTP server (e.g. in Docker or on another
machine), register it by URL:

```bash
claude mcp add --transport http pony http://localhost:8765/mcp
```

Replace `localhost:8765` with the actual host and port where the server is
reachable.  For a remote server:

```bash
claude mcp add --transport http pony http://my-server.example.com:8765/mcp
```

---

## Setup: Claude Desktop (HTTP)

For a network-accessible server, use the `streamable-http` type in
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pony": {
      "type": "streamable-http",
      "url": "http://localhost:8765/mcp"
    }
  }
}
```

---

## Starting the HTTP server

Start the server on localhost (accessible only from the same machine):

```bash
pony mcp-server --port 8765
```

Bind to all network interfaces (required for Docker or remote access):

```bash
pony mcp-server --host 0.0.0.0 --port 8765
```

The MCP endpoint is available at `http://<host>:<port>/mcp`.

### Docker example

```dockerfile
FROM python:3.13-slim
RUN pip install pony
COPY config.toml /root/.config/pony/config.toml
EXPOSE 8765
CMD ["pony", "mcp-server", "--host", "0.0.0.0", "--port", "8765"]
```

```bash
docker build -t pony-mcp .
docker run \
  -p 8765:8765 \
  -v /path/to/mirrors:/mirrors \
  -v /path/to/index:/root/.local/share/pony \
  pony-mcp
```

Mount the same mirror and index directories that `pony sync` writes to so
the container sees your up-to-date mail.

---

## Available tools

### `search_messages`

Full-text search across the local index.  Returns message metadata (sender,
subject, date, preview) but not the full body — use `get_message_body` for
that.

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

Return every folder present in the local mirror, grouped by account.  Useful
for discovering what folders exist before calling `list_messages`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `account` | string | all | Restrict to one account name |

---

### `list_messages`

List messages in a specific folder.  Returns the same metadata as
`search_messages` (no body text).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `account` | string | — | Account name (required) |
| `folder` | string | — | Folder name (required) |
| `limit` | int | `100` | Maximum number of results |

---

### `get_message`

Retrieve the index metadata for a single message identified by its
`message_id`.  Does not fetch the body from the mirror — use
`get_message_body` for that.

| Parameter | Type | Description |
|---|---|---|
| `account` | string | Account name |
| `folder` | string | Folder name |
| `message_id` | string | The `Message-ID` header value |

Returns `null` if the message is not found in the index.

---

### `get_message_body`

Read the full plain-text body of a message from the local mirror.  HTML-only
messages are automatically converted to plain text (style and script blocks
stripped).  Attachment metadata (filename, type, size) is included but
attachment content is not.

| Parameter | Type | Description |
|---|---|---|
| `account` | string | Account name |
| `folder` | string | Folder name |
| `message_id` | string | The `Message-ID` header value |

Returns a dict with keys: `subject`, `from`, `to`, `cc`, `date`, `body`
(plain text string), `attachments` (list of `{index, filename, content_type,
size_bytes}`).  Returns `null` if the message is not in the local mirror.

---

### `search_contacts`

Search the contacts store by name or email address prefix.  The search is
prefix-based and case-insensitive.

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

---

## Example prompts

Once connected, you can ask an AI assistant things like:

- *"Search my email for messages from alice@example.com about the budget"*
- *"What are the most recent unread messages in my INBOX?"*
- *"Read the message from Bob with subject 'Q3 report' and give me a summary"*
- *"List all my mail folders and tell me which ones have recent messages"*
- *"Find all messages with attachments received in the last week"*
- *"When did I last sync my Work account, and how many folders does it have?"*
- *"Search my contacts for anyone at example.com"*

The AI will call the appropriate tools automatically and present the results
as part of its response.
