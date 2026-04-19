"""MCP server for Pony Express.

Exposes read-only mail and contacts operations as MCP tools.

Transports
----------
stdio (default)
    ``pony mcp-server``
    Use with Claude Desktop or any local MCP client.

Streamable HTTP
    ``pony mcp-server --port 8765``
    Use in Docker or remote deployments; any MCP client that supports HTTP.

Compatibility with the TUI
--------------------------
HTTP mode can run alongside ``pony tui`` — each process opens its own
SQLite connection; the MCP server only reads, so there are no write
conflicts.  stdio mode cannot run alongside the TUI (both own stdin/stdout).
"""

from __future__ import annotations

import base64
import contextlib
import threading
from pathlib import Path
from typing import Any

from .config import load_config
from .domain import AccountConfig, AnyAccount, McpConfig, SearchQuery
from .index_store import SqliteIndexRepository
from .paths import AppPaths
from .protocols import MirrorRepository
from .storage import MaildirMirrorRepository, MboxMirrorRepository
from .tui.message_renderer import AttachmentPayload, extract_attachment, render_message

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
        "account": ref.account_name,
        "folder": ref.folder_name,
        "message_id": ref.rfc5322_id,
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
            sync_by_folder = {
                s.folder_name: s
                for s in index.list_folder_sync_states(account_name=acc.name)
            } if isinstance(acc, AccountConfig) else {}
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
                        if ref.folder_name in sync_by_folder else None
                    ),
                    "synced_at": (
                        sync_by_folder[ref.folder_name].synced_at
                        if ref.folder_name in sync_by_folder else None
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

    def _fetch_raw_bytes(
        account: str, folder: str, message_id: str,
    ) -> bytes | None:
        """Resolve RFC 5322 Message-ID → mirror storage_key → raw bytes.

        Returns ``None`` if the account has no mirror, the index doesn't
        know the message, or the mirror can't produce the bytes.
        """
        from .domain import FolderRef, MessageRef

        mirror = mirrors.get(account)
        if mirror is None:
            return None
        indexed = index.get_message(
            message_ref=MessageRef(
                account_name=account,
                folder_name=folder,
                rfc5322_id=message_id,
            ),
        )
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
        """Retrieve metadata for a single message.

        Returns None if the message is not in the local index.  The
        ``attachments`` field lists every attachment with its 1-based
        ``index``, filename, content type, and size — use
        *get_attachment* to fetch any one of them by index.  Use
        *get_message_body* to read the full text of the message.
        """
        from .domain import MessageRef

        ref = MessageRef(
            account_name=account,
            folder_name=folder,
            rfc5322_id=message_id,
        )
        msg = index.get_message(message_ref=ref)
        if msg is None:
            return None
        result = _msg_to_dict(msg)
        # Add the per-attachment list when the body is locally
        # available.  For messages we only know by metadata (e.g. not
        # yet fetched to the mirror) the has_attachments bool above is
        # still the best we can offer.
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


def start_mcp_thread(
    config_path: Path | None,
    mcp_config: McpConfig,
) -> threading.Thread:
    """Start the MCP HTTP server in a background daemon thread.

    Returns the thread object (already started).  Because the thread is a
    daemon, it terminates automatically when the main process exits — no
    explicit shutdown is required.

    Only HTTP transport is supported here; stdio cannot run alongside the TUI
    because both would compete for stdin/stdout.
    """
    import uvicorn  # deferred: only when embedded MCP is enabled

    server = build_mcp_server(config_path)
    uv_config = uvicorn.Config(
        server.streamable_http_app(),
        host=mcp_config.host,
        port=mcp_config.port,
        log_level="warning",
    )
    uv_server = uvicorn.Server(uv_config)
    thread = threading.Thread(target=uv_server.run, daemon=True, name="mcp-http")
    thread.start()
    return thread


def run_mcp_server(
    config_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> None:
    """Start the MCP server.

    Uses stdio when *port* is None (local / Claude Desktop use).
    Uses Streamable HTTP on *host*:*port* when *port* is given (Docker / remote).
    """
    server = build_mcp_server(config_path)
    if port is not None:
        server.run(transport="streamable-http", host=host, port=port)
    else:
        server.run(transport="stdio")
