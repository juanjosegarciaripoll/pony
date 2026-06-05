"""Tests for ``pony.mcp_server`` helpers.

Covers the serialisation helpers (_attachment_to_dict, _msg_to_dict,
_contact_to_dict, _sync_state_to_dict) and the full tool call round-trips
for build_mcp_server tools (search_messages, list_folders, list_messages,
get_message, get_message_body, get_attachment, search_contacts,
get_sync_status).
"""

from __future__ import annotations

import base64
import dataclasses
import unittest
from datetime import UTC
from email.message import EmailMessage
from uuid import uuid4

import corpus
from conftest import TMP_ROOT

from pony.domain import (
    AccountConfig,
    AppConfig,
    FolderRef,
    MessageRef,
    MessageStatus,
    MirrorConfig,
    SmtpConfig,
)
from pony.index_store import SqliteIndexRepository
from pony.mcp_server import (
    _attachment_to_dict,
    _contact_to_dict,
    _msg_to_dict,
    _sync_state_to_dict,
    build_mcp_server,
)
from pony.message_projection import project_rfc822_message
from pony.paths import AppPaths
from pony.storage import MaildirMirrorRepository
from pony.tui.message_renderer import AttachmentPayload


class AttachmentToDictTest(unittest.TestCase):
    """Per-attachment dict shape returned by the MCP ``get_attachment`` tool."""

    def test_binary_attachment_has_base64_only(self) -> None:
        payload = AttachmentPayload(
            filename="report.pdf",
            content_type="application/pdf",
            size_bytes=5,
            data=b"%PDF-",
        )
        result = _attachment_to_dict(payload)
        self.assertEqual(result["filename"], "report.pdf")
        self.assertEqual(result["content_type"], "application/pdf")
        self.assertEqual(result["size_bytes"], 5)
        self.assertEqual(
            base64.b64decode(result["data_base64"]),
            b"%PDF-",
        )
        # No `text` field for non-text content types — clients must
        # decode data_base64 themselves (or use a suitable reader).
        self.assertNotIn("text", result)

    def test_text_plain_attachment_includes_decoded_text(self) -> None:
        payload = AttachmentPayload(
            filename="notes.txt",
            content_type="text/plain",
            size_bytes=11,
            data=b"hello world",
        )
        result = _attachment_to_dict(payload)
        self.assertEqual(result["text"], "hello world")
        # Base64 is still present as the canonical, lossless form.
        self.assertEqual(
            base64.b64decode(result["data_base64"]),
            b"hello world",
        )

    def test_text_attachment_decodes_utf8(self) -> None:
        data = "Café — résumé".encode()
        payload = AttachmentPayload(
            filename="notes.txt",
            content_type="text/plain",
            size_bytes=len(data),
            data=data,
        )
        result = _attachment_to_dict(payload)
        self.assertEqual(result["text"], "Café — résumé")

    def test_text_attachment_falls_back_to_latin1_on_bad_utf8(self) -> None:
        """If the bytes aren't valid UTF-8 but the content type claims
        text/*, we still surface *something* readable rather than
        dropping the attachment on the floor."""
        # 0xe9 is 'é' in latin-1 but is an invalid UTF-8 lead byte on its own.
        data = b"\xe9claire"
        payload = AttachmentPayload(
            filename="bad.txt",
            content_type="text/plain",
            size_bytes=len(data),
            data=data,
        )
        result = _attachment_to_dict(payload)
        self.assertIn("text", result)
        self.assertEqual(result["text"], "éclaire")

    def test_text_html_attachment_also_gets_text(self) -> None:
        payload = AttachmentPayload(
            filename="page.html",
            content_type="text/html",
            size_bytes=13,
            data=b"<p>hi</p>",
        )
        result = _attachment_to_dict(payload)
        self.assertEqual(result["text"], "<p>hi</p>")


# ---------------------------------------------------------------------------
# MCP server build + tool call round-trips
# ---------------------------------------------------------------------------


def _make_mcp_env(
    label: str,
) -> tuple[
    AppPaths, AppConfig, SqliteIndexRepository, MaildirMirrorRepository, AccountConfig
]:
    """Build isolated paths, config, index, and mirror for MCP tests."""
    root = TMP_ROOT / f"mcp-{label}" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    mirror_dir = root / "data" / "mirrors" / "personal"
    mirror_dir.mkdir(parents=True, exist_ok=True)
    paths = AppPaths(
        config_file=root / "config" / "config.toml",
        data_dir=root / "data",
        state_dir=root / "state",
        cache_dir=root / "cache",
        log_dir=root / "state" / "logs",
        index_db_file=root / "data" / "index.sqlite3",
    )
    paths.ensure_runtime_dirs()
    account = AccountConfig(
        name="personal",
        email_address="personal@example.com",
        imap_host="imap.example.com",
        smtp=SmtpConfig(host="smtp.example.com"),
        username="personal",
        credentials_source="plaintext",
        mirror=MirrorConfig(path=mirror_dir, format="maildir"),
        password="secret",
    )
    config = AppConfig(accounts=(account,))
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    mirror = MaildirMirrorRepository(account_name="personal", root_dir=mirror_dir)
    return paths, config, index, mirror, account


def _seed_mcp_message(
    index: SqliteIndexRepository,
    mirror: MaildirMirrorRepository,
    account: AccountConfig,
    *,
    subject: str = "MCP test message",
    folder: str = "INBOX",
    body: str = "Hello from MCP test.",
) -> tuple[str, str]:
    """Seed one message; returns (storage_key, message_id)."""
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = account.email_address
    msg["Subject"] = subject
    msg["Date"] = "Mon, 1 Jan 2024 12:00:00 +0000"
    rfc5322_id = f"<mcp-{uuid4().hex}@example.com>"
    msg["Message-ID"] = rfc5322_id
    msg.set_content(body)
    raw = msg.as_bytes()

    folder_ref = FolderRef(account_name=account.name, folder_name=folder)
    storage_key = mirror.store_message(folder=folder_ref, raw_message=raw)
    projected = project_rfc822_message(
        message_ref=MessageRef(account_name=account.name, folder_name=folder, id=0),
        raw_message=raw,
        storage_key=storage_key,
    )
    stored = dataclasses.replace(
        projected, message_id=rfc5322_id, local_status=MessageStatus.ACTIVE
    )
    index.insert_message(message=stored)
    return storage_key, rfc5322_id


def _build_mcp_from_env(paths: AppPaths, config: AppConfig) -> object:
    """Build an McpServer from an in-memory config (bypassing disk config load)."""
    from unittest.mock import patch

    from pony.mcp_server import _make_mirror

    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    # mirrors is built by build_mcp_server internally; local var unused here
    _ = {acc.name: _make_mirror(acc) for acc in config.accounts}

    with (
        patch("pony.mcp_server.load_config", return_value=config),
        patch("pony.mcp_server.AppPaths.default", return_value=paths),
    ):
        mcp = build_mcp_server(config_path=None)
    return mcp


class McpToolSearchMessagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths, self.config, self.index, self.mirror, self.account = _make_mcp_env(
            "search"
        )
        _seed_mcp_message(
            self.index, self.mirror, self.account, subject="Quarterly report"
        )
        self.mcp = _build_mcp_from_env(self.paths, self.config)

    def _tool(self, name: str):
        return next(t.fn for t in self.mcp._tools if t.name == name)  # type: ignore[attr-defined]

    def test_search_messages_returns_results(self) -> None:
        results = self._tool("search_messages")(query="Quarterly")
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        self.assertIn("subject", results[0])

    def test_search_messages_empty_query_returns_all(self) -> None:
        results = self._tool("search_messages")()
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)

    def test_search_messages_with_account_filter(self) -> None:
        results = self._tool("search_messages")(query="Quarterly", account="personal")
        self.assertGreater(len(results), 0)

    def test_search_messages_unknown_account_returns_empty(self) -> None:
        results = self._tool("search_messages")(query="anything", account="unknown")
        self.assertEqual(results, [])


def _call_tool(mcp, name: str, **kwargs):
    fn = next(t.fn for t in mcp._tools if t.name == name)  # type: ignore[attr-defined]
    return fn(**kwargs)


class McpToolListFoldersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths, self.config, self.index, self.mirror, self.account = _make_mcp_env(
            "list-folders"
        )
        _seed_mcp_message(self.index, self.mirror, self.account)
        self.mcp = _build_mcp_from_env(self.paths, self.config)

    def test_list_folders_returns_accounts(self) -> None:
        results = _call_tool(self.mcp, "list_folders")
        self.assertIsInstance(results, list)
        self.assertTrue(any(r["account"] == "personal" for r in results))

    def test_list_folders_with_account_filter(self) -> None:
        results = _call_tool(self.mcp, "list_folders", account="personal")
        self.assertEqual(len(results), 1)

    def test_list_folders_unknown_account_empty(self) -> None:
        results = _call_tool(self.mcp, "list_folders", account="nobody")
        self.assertEqual(results, [])


class McpToolListMessagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths, self.config, self.index, self.mirror, self.account = _make_mcp_env(
            "list-msgs"
        )
        self.storage_key, self.rfc5322_id = _seed_mcp_message(
            self.index, self.mirror, self.account, subject="Listed message"
        )
        self.mcp = _build_mcp_from_env(self.paths, self.config)

    def test_list_messages_returns_list(self) -> None:
        results = _call_tool(
            self.mcp, "list_messages", account="personal", folder="INBOX"
        )
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        self.assertIn("subject", results[0])


class McpToolGetMessageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths, self.config, self.index, self.mirror, self.account = _make_mcp_env(
            "get-msg"
        )
        self.storage_key, self.rfc5322_id = _seed_mcp_message(
            self.index, self.mirror, self.account, subject="Get me"
        )
        self.mcp = _build_mcp_from_env(self.paths, self.config)

    def test_get_message_returns_dict(self) -> None:
        result = _call_tool(
            self.mcp,
            "get_message",
            account="personal",
            folder="INBOX",
            message_id=self.rfc5322_id,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["subject"], "Get me")

    def test_get_message_not_found_returns_none(self) -> None:
        result = _call_tool(
            self.mcp,
            "get_message",
            account="personal",
            folder="INBOX",
            message_id="<no-such@x.com>",
        )
        self.assertIsNone(result)

    def test_get_message_includes_attachments_key(self) -> None:
        result = _call_tool(
            self.mcp,
            "get_message",
            account="personal",
            folder="INBOX",
            message_id=self.rfc5322_id,
        )
        self.assertIn("attachments", result)


class McpToolGetMessageBodyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths, self.config, self.index, self.mirror, self.account = _make_mcp_env(
            "get-body"
        )
        self.storage_key, self.rfc5322_id = _seed_mcp_message(
            self.index, self.mirror, self.account, body="Body text here."
        )
        self.mcp = _build_mcp_from_env(self.paths, self.config)

    def test_get_message_body_returns_text(self) -> None:
        result = _call_tool(
            self.mcp,
            "get_message_body",
            account="personal",
            folder="INBOX",
            message_id=self.rfc5322_id,
        )
        self.assertIsNotNone(result)
        self.assertIn("Body text here", result["body"])

    def test_get_message_body_not_found_returns_none(self) -> None:
        result = _call_tool(
            self.mcp,
            "get_message_body",
            account="personal",
            folder="INBOX",
            message_id="<no@x.com>",
        )
        self.assertIsNone(result)


class McpToolGetAttachmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths, self.config, self.index, self.mirror, self.account = _make_mcp_env(
            "get-att"
        )
        raw = corpus.multipart_mixed_attachment()
        folder_ref = FolderRef(account_name="personal", folder_name="INBOX")
        key = self.mirror.store_message(folder=folder_ref, raw_message=raw)
        projected = project_rfc822_message(
            message_ref=MessageRef(account_name="personal", folder_name="INBOX", id=0),
            raw_message=raw,
            storage_key=key,
        )
        self.rfc5322_id = "<att1-fixture@example.com>"
        stored = dataclasses.replace(
            projected, message_id=self.rfc5322_id, local_status=MessageStatus.ACTIVE
        )
        self.index.insert_message(message=stored)
        self.mcp = _build_mcp_from_env(self.paths, self.config)

    def test_get_attachment_returns_dict(self) -> None:
        result = _call_tool(
            self.mcp,
            "get_attachment",
            account="personal",
            folder="INBOX",
            message_id=self.rfc5322_id,
            index=1,
        )
        self.assertIsNotNone(result)
        self.assertIn("data_base64", result)
        self.assertIn("filename", result)

    def test_get_attachment_out_of_range_returns_none(self) -> None:
        result = _call_tool(
            self.mcp,
            "get_attachment",
            account="personal",
            folder="INBOX",
            message_id=self.rfc5322_id,
            index=99,
        )
        self.assertIsNone(result)

    def test_get_attachment_message_not_found_returns_none(self) -> None:
        result = _call_tool(
            self.mcp,
            "get_attachment",
            account="personal",
            folder="INBOX",
            message_id="<no-such@x.com>",
            index=1,
        )
        self.assertIsNone(result)


class McpToolSearchContactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths, self.config, self.index, self.mirror, self.account = _make_mcp_env(
            "contacts"
        )
        self.mcp = _build_mcp_from_env(self.paths, self.config)

    def test_search_contacts_returns_list(self) -> None:
        results = _call_tool(self.mcp, "search_contacts", prefix="alice")
        self.assertIsInstance(results, list)


class McpToolGetSyncStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths, self.config, self.index, self.mirror, self.account = _make_mcp_env(
            "sync-status"
        )
        self.mcp = _build_mcp_from_env(self.paths, self.config)

    def test_get_sync_status_returns_list(self) -> None:
        results = _call_tool(self.mcp, "get_sync_status")
        self.assertIsInstance(results, list)

    def test_get_sync_status_with_account_filter(self) -> None:
        results = _call_tool(self.mcp, "get_sync_status", account="personal")
        self.assertIsInstance(results, list)


class McpHelperSerializersTest(unittest.TestCase):
    def test_msg_to_dict_has_expected_keys(self) -> None:
        from datetime import datetime
        from unittest.mock import MagicMock

        msg = MagicMock()
        msg.message_ref.id = 1
        msg.message_ref.account_name = "personal"
        msg.message_ref.folder_name = "INBOX"
        msg.message_id = "<test@x.com>"
        msg.sender = "alice@example.com"
        msg.recipients = "bob@example.com"
        msg.cc = ""
        msg.subject = "Test"
        msg.has_attachments = False
        msg.local_flags = frozenset()
        msg.local_status.value = "active"
        msg.received_at = datetime(2024, 1, 1, tzinfo=UTC)
        msg.uid = 42

        result = _msg_to_dict(msg)
        for key in ("id", "account", "folder", "message_id", "subject", "uid"):
            self.assertIn(key, result)

    def test_contact_to_dict_has_expected_keys(self) -> None:
        from unittest.mock import MagicMock

        contact = MagicMock()
        contact.id = 1
        contact.first_name = "Alice"
        contact.last_name = "Smith"
        contact.emails = ["alice@example.com"]
        contact.organization = "ACME"
        contact.aliases = []
        contact.notes = ""
        contact.message_count = 5
        contact.last_seen = None

        result = _contact_to_dict(contact)
        self.assertIn("emails", result)
        self.assertIn("last_seen", result)
        self.assertIsNone(result["last_seen"])

    def test_sync_state_to_dict_has_expected_keys(self) -> None:
        from unittest.mock import MagicMock

        state = MagicMock()
        state.account_name = "personal"
        state.folder_name = "INBOX"
        state.uid_validity = 12345
        state.highest_uid = 100
        state.synced_at = "2024-01-01T00:00:00"

        result = _sync_state_to_dict(state)
        for key in ("account", "folder", "uid_validity", "highest_uid", "synced_at"):
            self.assertIn(key, result)
