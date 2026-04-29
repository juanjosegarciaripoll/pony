"""MCP server for Pony Express.

Exposes read-only mail and contacts operations as MCP tools.

When the TUI is running it starts a TCP server on ``127.0.0.1`` with a
per-session auth token stored in a state file.  ``pony mcp`` checks for this
file and proxies stdin/stdout to the TCP server (bridge mode), avoiding a
competing SQLite opener.  When no TUI is running ``pony mcp`` opens its own
connections and serves tools directly via stdio.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_config
from .domain import AccountConfig, AnyAccount, SearchQuery
from .index_store import SqliteIndexRepository
from .paths import AppPaths
from .protocols import MirrorRepository
from .storage import MaildirMirrorRepository, MboxMirrorRepository
from .tui.message_renderer import AttachmentPayload, extract_attachment, render_message

# ---------------------------------------------------------------------------
# MCP state (TUI ↔ bridge IPC)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpState:
    """Port and auth token published by the TUI's embedded TCP MCP server."""

    port: int
    token: str


def write_mcp_state(state_file: Path, state: McpState) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"port": state.port, "token": state.token}))


def read_mcp_state(state_file: Path) -> McpState | None:
    try:
        data = json.loads(state_file.read_text())
        return McpState(port=int(data["port"]), token=str(data["token"]))
    except Exception:
        return None


def clear_mcp_state(state_file: Path) -> None:
    with contextlib.suppress(Exception):
        state_file.unlink()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_mirror(acc: AnyAccount) -> MirrorRepository:
    if acc.mirror.format == "maildir":
        return MaildirMirrorRepository(account_name=acc.name, root_dir=acc.mirror.path)
    return MboxMirrorRepository(account_name=acc.name, root_dir=acc.mirror.path)


def _msg_to_dict(msg: Any) -> dict[str, Any]:
    """Serialise an IndexedMessage to a JSON-safe dict.

    Body text is not included — the index is metadata-only at the
    caller-visible layer.  Call ``get_message_body`` to read the full
    text from the local mirror.
    """
    ref = msg.message_ref
    return {
        "id": ref.id,
        "account": ref.account_name,
        "folder": ref.folder_name,
        "message_id": msg.message_id,
        "sender": msg.sender,
        "recipients": msg.recipients,
        "cc": msg.cc,
        "subject": msg.subject,
        "has_attachments": msg.has_attachments,
        "flags": sorted(f.value for f in msg.local_flags),
        "status": msg.local_status.value,
        "received_at": msg.received_at.isoformat(),
        "uid": msg.uid,
    }


def _contact_to_dict(c: Any) -> dict[str, Any]:
    """Serialise a Contact to a JSON-safe dict."""
    return {
        "id": c.id,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "emails": list(c.emails),
        "organization": c.organization,
        "aliases": list(c.aliases),
        "notes": c.notes,
        "message_count": c.message_count,
        "last_seen": c.last_seen.isoformat() if c.last_seen else None,
    }


def _attachment_to_dict(payload: AttachmentPayload) -> dict[str, Any]:
    """Serialise an :class:`AttachmentPayload` for MCP transport.

    ``data_base64`` is always present (transport-safe for any byte
    sequence).  ``text`` is added when the content type starts with
    ``text/`` and the bytes decode cleanly — letting AI agents read
    textual attachments without base64-decoding on the client side.
    Decode is best-effort: utf-8 first, then latin-1 (a total mapping);
    if both fail the field is omitted and callers fall back to
    ``data_base64``.
    """
    result: dict[str, Any] = {
        "filename": payload.filename,
        "content_type": payload.content_type,
        "size_bytes": payload.size_bytes,
        "data_base64": base64.b64encode(payload.data).decode("ascii"),
    }
    if payload.content_type.startswith("text/"):
        try:
            result["text"] = payload.data.decode("utf-8")
        except UnicodeDecodeError:
            with contextlib.suppress(UnicodeDecodeError):
                result["text"] = payload.data.decode("latin-1")
    return result


def _sync_state_to_dict(s: Any) -> dict[str, Any]:
    """Serialise a FolderSyncState to a JSON-safe dict."""
    return {
        "account": s.account_name,
        "folder": s.folder_name,
        "uid_validity": s.uid_validity,
        "highest_uid": s.highest_uid,
        "synced_at": s.synced_at,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_mcp_server(config_path: Path | None = None) -> Any:
    """Build and return a configured FastMCP instance.

    All tools are registered as closures that capture the index and mirror
    objects built at startup — no per-call reconnection overhead.
    """
    from mcp.server.fastmcp import FastMCP  # deferred: only when subcommand runs

    paths = AppPaths.default()
    paths.ensure_runtime_dirs()
    config = load_config(config_path or paths.config_file)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    mirrors: dict[str, MirrorRepository] = {
        acc.name: _make_mirror(acc) for acc in config.accounts
    }

    mcp: Any = FastMCP("Pony Express")

    # ------------------------------------------------------------------
    # Tool: search_messages
    # ------------------------------------------------------------------

    @mcp.tool()  # type: ignore[untyped-decorator]
    def search_messages(  # pyright: ignore[reportUnusedFunction]
        query: str = "",
        from_address: str = "",
        to_address: str = "",
        subject: str = "",
        body: str = "",
        account: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search the local mail index.

        Returns message metadata only (no body text).  Call
        *get_message_body* to read the full content of a specific
        message.  Matching is always case- and diacritic-insensitive.

        At least one of query / from_address / to_address / subject / body
        must be non-empty, otherwise all messages are returned (up to limit).
        """
        sq = SearchQuery(
            text=query,
            from_address=from_address,
            to_address=to_address,
            subject=subject,
            body=body,
        )
        msgs = index.search(query=sq, account_name=account)
        return [_msg_to_dict(m) for m in msgs[:limit]]

    # ------------------------------------------------------------------
    # Tool: list_folders
    # ------------------------------------------------------------------

    @mcp.tool()  # type: ignore[untyped-decorator]
    def list_folders(  # pyright: ignore[reportUnusedFunction]
        account: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all accounts and their local mirror folders.

        Returns one entry per account.  Each folder entry carries its name,
        the number of indexed messages, the highest-known UID, and the
        last-sync timestamp (null if never synced).  Pass *account* to
        restrict to one account; omit for all accounts.
        """
        results: list[dict[str, Any]] = []
        for acc in config.accounts:
            if account and acc.name != account:
                continue
            mirror = mirrors.get(acc.name)
            if mirror is None:
                continue
            sync_by_folder = (
                {
                    s.folder_name: s
                    for s in index.list_folder_sync_states(account_name=acc.name)
                }
                if isinstance(acc, AccountConfig)
                else {}
            )
            folder_refs = sorted(
                mirror.list_folders(account_name=acc.name),
                key=lambda r: r.folder_name,
            )
            folders = [
                {
                    "name": ref.folder_name,
                    "message_count": index.count_folder_messages(folder=ref),
                    "highest_uid": (
                        sync_by_folder[ref.folder_name].highest_uid
                        if ref.folder_name in sync_by_folder
                        else None
                    ),
                    "synced_at": (
                        sync_by_folder[ref.folder_name].synced_at
                        if ref.folder_name in sync_by_folder
                        else None
                    ),
                }
                for ref in folder_refs
            ]
            results.append({"account": acc.name, "folders": folders})
        return results

    # ------------------------------------------------------------------
    # Tool: list_messages
    # ------------------------------------------------------------------

    @mcp.tool()  # type: ignore[untyped-decorator]
    def list_messages(  # pyright: ignore[reportUnusedFunction]
        account: str,
        folder: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List messages in a specific folder (metadata only, no body).

        Use *search_messages* for full-text search across folders.
        """
        from .domain import FolderRef

        folder_ref = FolderRef(account_name=account, folder_name=folder)
        msgs = index.list_folder_messages(folder=folder_ref)
        return [_msg_to_dict(m) for m in msgs[:limit]]

    # ------------------------------------------------------------------
    # Tool: get_message
    # ------------------------------------------------------------------

    def _resolve_message(
        account: str,
        folder: str,
        message_id: str,
    ) -> Any | None:
        """Resolve display Message-ID → indexed row.

        Returns ``None`` when no row matches.  When multiple rows
        share the same Message-ID, the most recent is returned.
        Callers that need to disambiguate should fall back to
        ``find_messages_by_message_id``.
        """
        hits = index.find_messages_by_message_id(
            account_name=account,
            folder_name=folder,
            message_id=message_id,
        )
        return hits[0] if hits else None

    def _fetch_raw_bytes(
        account: str,
        folder: str,
        message_id: str,
    ) -> bytes | None:
        """Resolve Message-ID → mirror bytes (or ``None`` on miss)."""
        from .domain import FolderRef

        mirror = mirrors.get(account)
        if mirror is None:
            return None
        indexed = _resolve_message(account, folder, message_id)
        if indexed is None:
            return None
        try:
            raw = mirror.get_message_bytes(
                folder=FolderRef(account_name=account, folder_name=folder),
                storage_key=indexed.storage_key,
            )
        except (KeyError, FileNotFoundError, OSError):
            return None
        return raw or None

    @mcp.tool()  # type: ignore[untyped-decorator]
    def get_message(  # pyright: ignore[reportUnusedFunction]
        account: str,
        folder: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve metadata for one message by display Message-ID.

        Returns ``None`` when no row matches.  When multiple rows
        share the Message-ID (legitimate: alias delivery, mailing-list
        dup), the most recent is returned.
        """
        msg = _resolve_message(account, folder, message_id)
        if msg is None:
            return None
        result = _msg_to_dict(msg)
        raw = _fetch_raw_bytes(account, folder, message_id)
        if raw is not None:
            rendered = render_message(raw)
            result["attachments"] = [
                {
                    "index": a.index,
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size_bytes": a.size_bytes,
                }
                for a in rendered.attachments
            ]
        return result

    # ------------------------------------------------------------------
    # Tool: get_message_body
    # ------------------------------------------------------------------

    @mcp.tool()  # type: ignore[untyped-decorator]
    def get_message_body(  # pyright: ignore[reportUnusedFunction]
        account: str,
        folder: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve the full text of a message.

        Returns a dict with keys: subject, from, to, cc, date, body,
        attachments.  Returns None if the message is not in the local
        mirror.  Use *get_attachment* to fetch any attachment's bytes
        by its 1-based ``index``.
        """
        raw = _fetch_raw_bytes(account, folder, message_id)
        if raw is None:
            return None
        rendered = render_message(raw)
        return {
            "subject": rendered.subject,
            "from": rendered.from_,
            "to": rendered.to,
            "cc": rendered.cc,
            "date": rendered.date,
            "body": rendered.body,
            "attachments": [
                {
                    "index": a.index,
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size_bytes": a.size_bytes,
                }
                for a in rendered.attachments
            ],
        }

    # ------------------------------------------------------------------
    # Tool: get_attachment
    # ------------------------------------------------------------------

    @mcp.tool()  # type: ignore[untyped-decorator]
    def get_attachment(  # pyright: ignore[reportUnusedFunction]
        account: str,
        folder: str,
        message_id: str,
        index: int,
    ) -> dict[str, Any] | None:
        """Retrieve one attachment's bytes by its 1-based ``index``.

        ``index`` matches the ``attachments[*].index`` values returned
        by *get_message* and *get_message_body*.  Returns a dict with:

        - ``filename``, ``content_type``, ``size_bytes``: metadata
        - ``data_base64``: always present — base64-encoded raw bytes,
          transport-safe for any attachment type
        - ``text``: present only when the attachment is ``text/*`` and
          can be decoded; the already-decoded string, so AI agents
          don't have to base64-decode it themselves

        Prefer ``text`` when it is present; fall back to
        ``data_base64`` for binary formats.  Returns None when the
        message isn't in the local mirror or the index is out of range.
        """
        raw = _fetch_raw_bytes(account, folder, message_id)
        if raw is None:
            return None
        payload = extract_attachment(raw, index)
        if payload is None:
            return None
        return _attachment_to_dict(payload)

    # ------------------------------------------------------------------
    # Tool: search_contacts
    # ------------------------------------------------------------------

    @mcp.tool()  # type: ignore[untyped-decorator]
    def search_contacts(  # pyright: ignore[reportUnusedFunction]
        prefix: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search contacts by name or email prefix."""
        contacts = index.search_contacts(prefix=prefix, limit=limit)
        return [_contact_to_dict(c) for c in contacts]

    # ------------------------------------------------------------------
    # Tool: get_sync_status
    # ------------------------------------------------------------------

    @mcp.tool()  # type: ignore[untyped-decorator]
    def get_sync_status(  # pyright: ignore[reportUnusedFunction]
        account: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the last-sync timestamp and highest UID for each folder.

        Pass *account* to restrict to one account; omit for all accounts.
        """
        results: list[dict[str, Any]] = []
        for acc in config.accounts:
            if account and acc.name != account:
                continue
            if not isinstance(acc, AccountConfig):
                continue
            states = index.list_folder_sync_states(account_name=acc.name)
            results.extend(_sync_state_to_dict(s) for s in states)
        return results

    return mcp


async def _handle_mcp_tcp_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    token: str,
    mcp: Any,
) -> None:
    """Authenticate and run one MCP session over a TCP connection."""
    import anyio
    import mcp.types as types
    from mcp.shared.message import SessionMessage

    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    except TimeoutError:
        writer.close()
        return
    if line.decode("utf-8").strip() != f"TOKEN:{token}":
        writer.close()
        return

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async def from_tcp() -> None:
        async with read_stream_writer:
            while not reader.at_eof():
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    msg = types.JSONRPCMessage.model_validate_json(raw)
                    await read_stream_writer.send(SessionMessage(msg))
                except Exception as exc:
                    await read_stream_writer.send(exc)

    async def to_tcp() -> None:
        async with write_stream_reader:
            async for sm in write_stream_reader:
                json_line = (
                    sm.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                )
                writer.write(json_line.encode("utf-8"))
                await writer.drain()
        writer.close()

    from_task = asyncio.create_task(from_tcp())
    to_task = asyncio.create_task(to_tcp())
    try:
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )
    finally:
        from_task.cancel()
        to_task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(from_task, to_task)


async def start_tcp_mcp_server(
    config_path: Path | None,
    state_file: Path,
) -> tuple[asyncio.Server, McpState]:
    """Start the embedded TCP MCP server for the TUI.

    Binds to a random loopback port, writes the port and a fresh auth token
    to *state_file*, and returns the running server and its state.  Call
    ``asyncio.create_task(server.serve_forever())`` to keep it alive in the
    background.
    """
    token = secrets.token_hex(32)
    mcp = build_mcp_server(config_path)

    def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        asyncio.create_task(_handle_mcp_tcp_client(reader, writer, token, mcp))

    server = await asyncio.start_server(_on_client, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    state = McpState(port=port, token=token)
    write_mcp_state(state_file, state)
    return server, state


async def _bridge_stdio_to_tcp(state: McpState) -> bool:
    """Proxy stdin/stdout to the TUI's TCP MCP server.

    Returns ``True`` when the session completes normally, ``False`` when the
    TCP server cannot be reached (stale state file).
    """
    try:
        tcp_reader, tcp_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", state.port), timeout=2.0
        )
    except Exception:
        return False

    tcp_writer.write(f"TOKEN:{state.token}\n".encode())
    await tcp_writer.drain()

    loop = asyncio.get_running_loop()
    stdin_buf = sys.stdin.buffer
    stdout_buf = sys.stdout.buffer

    async def pipe_in() -> None:
        while True:
            line = await loop.run_in_executor(None, stdin_buf.readline)
            if not line:
                break
            tcp_writer.write(line)
            await tcp_writer.drain()
        with contextlib.suppress(Exception):
            tcp_writer.close()
            await tcp_writer.wait_closed()

    async def pipe_out() -> None:
        while True:
            chunk = await tcp_reader.read(65536)
            if not chunk:
                break
            stdout_buf.write(chunk)
            stdout_buf.flush()

    await asyncio.gather(pipe_in(), pipe_out(), return_exceptions=True)
    return True


def run_mcp_server(
    config_path: Path | None = None,
    state_file: Path | None = None,
) -> None:
    """Start the MCP server.

    When *state_file* exists and points to a reachable TUI TCP server, act as
    a bridge (stdin/stdout ↔ TCP).  Otherwise open local connections and serve
    via stdio directly.
    """
    if state_file is not None:
        state = read_mcp_state(state_file)
        if state is not None and asyncio.run(_bridge_stdio_to_tcp(state)):
            return
    build_mcp_server(config_path).run(transport="stdio")
