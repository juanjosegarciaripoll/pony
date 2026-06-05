"""Tests for ``pony.imap_client`` — pure helpers and mocked session."""

from __future__ import annotations

import unittest
from datetime import UTC
from unittest.mock import MagicMock, patch

from pony.domain import MessageFlag
from pony.imap_client import (
    ImapAuthError,
    ImapSession,
    _decode_response,
    _extract_message_id,
    _format_imap_flags,
    _imap_errors,
    _parse_appenduid,
    _parse_copyuid,
    _parse_imap_flags,
)

# ---------------------------------------------------------------------------
# _imap_errors context manager
# ---------------------------------------------------------------------------


class ImapErrorsTest(unittest.TestCase):
    def test_passes_through_on_success(self) -> None:
        with _imap_errors("test"):
            pass  # no exception

    def test_converts_imap_error_to_oserror(self) -> None:
        from imapclient.exceptions import IMAPClientError

        with self.assertRaises(OSError) as ctx, _imap_errors("LIST"):
            raise IMAPClientError("bad response")
        self.assertIn("LIST", str(ctx.exception))
        self.assertIn("bad response", str(ctx.exception))

    def test_converts_imap_error_without_context(self) -> None:
        from imapclient.exceptions import IMAPClientError

        with self.assertRaises(OSError), _imap_errors():
            raise IMAPClientError("no context")


# ---------------------------------------------------------------------------
# _parse_imap_flags
# ---------------------------------------------------------------------------


class ParseImapFlagsTest(unittest.TestCase):
    def test_seen_flag_parsed(self) -> None:
        known, extra = _parse_imap_flags((b"\\Seen",))
        self.assertIn(MessageFlag.SEEN, known)
        self.assertEqual(extra, frozenset())

    def test_multiple_flags_parsed(self) -> None:
        known, extra = _parse_imap_flags((b"\\Seen", b"\\Answered", b"\\Flagged"))
        self.assertIn(MessageFlag.SEEN, known)
        self.assertIn(MessageFlag.ANSWERED, known)
        self.assertIn(MessageFlag.FLAGGED, known)

    def test_unknown_flag_goes_to_extra(self) -> None:
        known, extra = _parse_imap_flags((b"\\CustomFlag",))
        self.assertEqual(known, frozenset())
        self.assertIn("\\CustomFlag", extra)

    def test_server_only_flags_skipped(self) -> None:
        known, extra = _parse_imap_flags((b"\\Recent",))
        self.assertEqual(known, frozenset())
        self.assertEqual(extra, frozenset())

    def test_empty_flags_returns_empty_sets(self) -> None:
        known, extra = _parse_imap_flags(())
        self.assertEqual(known, frozenset())
        self.assertEqual(extra, frozenset())


# ---------------------------------------------------------------------------
# _format_imap_flags
# ---------------------------------------------------------------------------


class FormatImapFlagsTest(unittest.TestCase):
    def test_seen_formatted(self) -> None:
        result = _format_imap_flags(frozenset({MessageFlag.SEEN}))
        self.assertIn(b"\\Seen", result)

    def test_multiple_flags_formatted(self) -> None:
        result = _format_imap_flags(frozenset({MessageFlag.SEEN, MessageFlag.FLAGGED}))
        self.assertIn(b"\\Seen", result)
        self.assertIn(b"\\Flagged", result)

    def test_empty_flags_returns_empty_list(self) -> None:
        result = _format_imap_flags(frozenset())
        self.assertEqual(result, [])

    def test_extra_flags_appended(self) -> None:
        result = _format_imap_flags(frozenset(), extra=frozenset({"\\CustomTag"}))
        self.assertIn(b"\\CustomTag", result)


# ---------------------------------------------------------------------------
# ImapAuthError
# ---------------------------------------------------------------------------


class ImapAuthErrorTest(unittest.TestCase):
    def test_message_includes_username_and_host(self) -> None:
        err = ImapAuthError("user@example.com", "imap.example.com")
        self.assertIn("user@example.com", str(err))
        self.assertIn("imap.example.com", str(err))
        self.assertEqual(err.username, "user@example.com")
        self.assertEqual(err.host, "imap.example.com")

    def test_is_connection_error(self) -> None:
        err = ImapAuthError("user", "host")
        self.assertIsInstance(err, ConnectionError)


# ---------------------------------------------------------------------------
# ImapSession with mocked IMAPClient
# ---------------------------------------------------------------------------


def _make_mock_imap_client() -> MagicMock:
    """Return a MagicMock that acts as IMAPClient."""
    mock = MagicMock()
    mock.capabilities.return_value = []
    mock.login.return_value = b"OK logged in"
    return mock


class ImapSessionMockedTest(unittest.TestCase):
    """Tests for ImapSession using a mocked IMAPClient."""

    def _make_session(self) -> tuple[ImapSession, MagicMock]:
        mock_client = _make_mock_imap_client()
        with patch("pony.imap_client.IMAPClient", return_value=mock_client):
            session = ImapSession(
                host="imap.example.com",
                port=993,
                ssl=True,
                username="user",
                password="secret",
            )
        return session, mock_client

    def test_constructor_connects_and_logs_in(self) -> None:
        session, mock_client = self._make_session()
        mock_client.login.assert_called_once_with("user", "secret")

    def test_login_error_raises_auth_error(self) -> None:
        from imapclient.exceptions import LoginError

        mock_client = _make_mock_imap_client()
        mock_client.login.side_effect = LoginError("bad password")
        with (
            patch("pony.imap_client.IMAPClient", return_value=mock_client),
            self.assertRaises(ImapAuthError) as ctx,
        ):
            ImapSession(
                host="imap.example.com",
                port=993,
                ssl=True,
                username="user",
                password="wrong",
            )
        self.assertIn("imap.example.com", str(ctx.exception))

    def test_compress_enabled_when_supported(self) -> None:
        mock_client = _make_mock_imap_client()
        mock_client.capabilities.return_value = [b"COMPRESS=DEFLATE"]
        with patch("pony.imap_client.IMAPClient", return_value=mock_client):
            ImapSession(
                host="imap.example.com",
                port=993,
                ssl=True,
                username="user",
                password="secret",
            )
        mock_client.compress.assert_called_once()

    def test_logout(self) -> None:
        session, mock_client = self._make_session()
        session.logout()
        mock_client.logout.assert_called_once()

    def test_list_folders_returns_folder_names(self) -> None:
        session, mock_client = self._make_session()
        mock_client.list_folders.return_value = [
            (b"\\HasNoChildren", b".", "INBOX"),
            (b"\\HasNoChildren", b".", "Sent"),
        ]
        folders = session.list_folders()
        self.assertIn("INBOX", folders)
        self.assertIn("Sent", folders)

    def test_list_folders_handles_bytes_names(self) -> None:
        session, mock_client = self._make_session()
        mock_client.list_folders.return_value = [
            (b"\\HasNoChildren", b".", b"INBOX"),
        ]
        folders = session.list_folders()
        self.assertIn("INBOX", folders)

    def test_get_folder_status(self) -> None:
        session, mock_client = self._make_session()
        mock_client.folder_status.return_value = {
            b"MESSAGES": 5,
            b"UNSEEN": 2,
        }
        msg_count, unseen = session.get_folder_status("INBOX")
        self.assertEqual(msg_count, 5)
        self.assertEqual(unseen, 2)

    def test_folder_quick_status(self) -> None:
        session, mock_client = self._make_session()
        mock_client.folder_status.return_value = {
            b"UIDVALIDITY": 12345,
            b"UIDNEXT": 100,
            b"MESSAGES": 42,
        }
        qs = session.folder_quick_status("INBOX")
        self.assertEqual(qs.uid_validity, 12345)
        self.assertEqual(qs.uidnext, 100)
        self.assertEqual(qs.messages, 42)
        self.assertIsNone(qs.highest_modseq)

    def test_logout_error_ignored(self) -> None:
        session, mock_client = self._make_session()
        mock_client.logout.side_effect = OSError("already disconnected")
        # Should not raise — errors during logout are silently ignored
        try:
            session.logout()
        except OSError:
            self.fail("logout() should not propagate OSError")

    def test_compress_failure_silently_ignored(self) -> None:
        """If compress() raises, the session continues without compression."""
        mock_client = _make_mock_imap_client()
        mock_client.capabilities.return_value = [b"COMPRESS=DEFLATE"]
        mock_client.compress.side_effect = Exception("compress failed")
        with patch("pony.imap_client.IMAPClient", return_value=mock_client):
            session = ImapSession(
                host="imap.example.com",
                port=993,
                ssl=True,
                username="user",
                password="secret",
            )
        # No exception should propagate
        self.assertIsNotNone(session)

    def test_fetch_uid_to_message_id(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        mock_client.fetch.return_value = {
            42: {
                b"FLAGS": (b"\\Seen",),
                b"BODY[HEADER.FIELDS (MESSAGE-ID)]": (
                    b"Message-ID: <test@example.com>\r\n"
                ),
            }
        }
        result = session.fetch_uid_to_message_id("INBOX")
        self.assertIn(42, result)
        mid, flags = result[42]
        self.assertIn("test@example.com", mid)

    def test_fetch_flags_empty_uids(self) -> None:
        session, mock_client = self._make_session()
        result = session.fetch_flags("INBOX", [])
        self.assertEqual(result, {})

    def test_fetch_flags(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        mock_client.fetch.return_value = {
            10: {b"FLAGS": (b"\\Seen", b"\\Flagged")},
        }
        result = session.fetch_flags("INBOX", [10])
        self.assertIn(10, result)

    def test_fetch_messages_batch_empty(self) -> None:
        session, mock_client = self._make_session()
        result = session.fetch_messages_batch("INBOX", [])
        self.assertEqual(result, {})

    def test_fetch_messages_batch(self) -> None:
        raw_email = b"From: a@b.com\r\nSubject: Test\r\n\r\nbody\r\n"
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        mock_client.fetch.return_value = {
            5: {b"RFC822": raw_email},
        }
        result = session.fetch_messages_batch("INBOX", [5])
        self.assertIn(5, result)
        self.assertEqual(result[5], raw_email)

    def test_get_uid_validity(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        mock_client.folder_status.return_value = {b"UIDVALIDITY": 99999}
        validity = session.get_uid_validity("INBOX")
        self.assertEqual(validity, 99999)

    def test_fetch_flags_changed_since(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        mock_client.fetch.return_value = {
            7: {b"FLAGS": (b"\\Seen",)},
        }
        result = session.fetch_flags_changed_since("INBOX", modseq=1000)
        self.assertIn(7, result)

    def test_folder_quick_status_with_modseq(self) -> None:
        session, mock_client = self._make_session()
        mock_client.capabilities.return_value = [b"CONDSTORE"]
        mock_client.folder_status.return_value = {
            b"UIDVALIDITY": 1,
            b"UIDNEXT": 10,
            b"MESSAGES": 5,
            b"HIGHESTMODSEQ": 9999,
        }
        qs = session.folder_quick_status("INBOX")
        self.assertEqual(qs.highest_modseq, 9999)

    def test_fetch_last_message_date_returns_date(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        from datetime import datetime

        mock_client.fetch.return_value = {
            1: {b"INTERNALDATE": datetime(2024, 1, 1, tzinfo=UTC)}
        }
        result = session.fetch_last_message_date("INBOX")
        self.assertIsNotNone(result)

    def test_fetch_last_message_date_empty_folder(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        mock_client.fetch.return_value = {}
        result = session.fetch_last_message_date("INBOX")
        self.assertIsNone(result)

    def test_store_flags(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        session.store_flags("INBOX", 1, frozenset({MessageFlag.SEEN}), frozenset())
        mock_client.set_flags.assert_called_once()

    def test_append_message_returns_uid(self) -> None:
        session, mock_client = self._make_session()
        mock_client.append.return_value = b"[APPENDUID 12345 99]"
        raw = b"From: a@b.com\r\nSubject: x\r\n\r\nbody"
        session.append_message("INBOX", raw, frozenset({MessageFlag.SEEN}))
        mock_client.append.assert_called_once()

    def test_mark_deleted(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        session.mark_deleted("INBOX", 5)
        mock_client.delete_messages.assert_called_once_with([5])

    def test_expunge(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        session.expunge("INBOX")
        mock_client.expunge.assert_called_once()

    def test_create_folder_when_not_exists(self) -> None:
        session, mock_client = self._make_session()
        mock_client.folder_exists.return_value = False
        session.create_folder("NewFolder")
        mock_client.create_folder.assert_called_once_with("NewFolder")

    def test_create_folder_when_exists_no_op(self) -> None:
        session, mock_client = self._make_session()
        mock_client.folder_exists.return_value = True
        session.create_folder("Existing")
        mock_client.create_folder.assert_not_called()

    def test_fetch_uid_to_message_id_selected_already(self) -> None:
        """When folder is already selected, _ensure_selected is a no-op."""
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        mock_client.fetch.return_value = {}
        # Select the folder first
        session._selected = "INBOX"
        session.fetch_uid_to_message_id("INBOX")
        # select_folder should NOT be called again since already selected
        mock_client.select_folder.assert_not_called()

    def test_fetch_message_bytes_uid_not_found(self) -> None:
        session, mock_client = self._make_session()
        mock_client.select_folder.return_value = {}
        mock_client.fetch.return_value = {}
        with self.assertRaises(KeyError):
            session.fetch_message_bytes("INBOX", 42)

    def test_move_message_with_move_capability(self) -> None:
        session, mock_client = self._make_session()
        mock_client.capabilities.return_value = [b"MOVE"]
        mock_client.select_folder.return_value = {}
        mock_client.move.return_value = b"[COPYUID 1 1 99]"
        session.move_message("INBOX", 1, "Archive")
        mock_client.move.assert_called_once()

    def test_move_message_without_move_capability(self) -> None:
        session, mock_client = self._make_session()
        mock_client.capabilities.return_value = []
        mock_client.select_folder.return_value = {}
        mock_client.copy.return_value = b"[COPYUID 1 1 99]"
        session.move_message("INBOX", 1, "Archive")
        mock_client.copy.assert_called_once()
        mock_client.delete_messages.assert_called_once_with([1])
        mock_client.expunge.assert_called_once()

    def test_retry_on_transient_error(self) -> None:
        """_retry reconnects and retries on SSL/transient errors."""
        import ssl

        session, mock_client = self._make_session()

        # Set up reconnect to return the same mock
        with patch.object(session, "_new_connection", return_value=mock_client):
            call_count = [0]

            def _flaky_list():
                call_count[0] += 1
                if call_count[0] == 1:
                    raise ssl.SSLEOFError("connection dropped")
                return [(b"", b".", "INBOX")]

            mock_client.list_folders = _flaky_list
            with patch("pony.imap_client.time.sleep"):
                folders = session.list_folders()

        self.assertIn("INBOX", folders)


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


class ExtractMessageIdTest(unittest.TestCase):
    def test_extracts_message_id_from_header(self) -> None:
        header = b"Message-ID: <test-123@example.com>\r\n"
        result = _extract_message_id(header)
        self.assertEqual(result, "<test-123@example.com>")

    def test_returns_empty_when_no_message_id(self) -> None:
        header = b"Subject: test\r\n"
        result = _extract_message_id(header)
        self.assertEqual(result, "")

    def test_returns_empty_for_empty_bytes(self) -> None:
        result = _extract_message_id(b"")
        self.assertEqual(result, "")


class DecodeResponseTest(unittest.TestCase):
    def test_none_returns_empty_string(self) -> None:
        self.assertEqual(_decode_response(None), "")

    def test_bytes_decoded(self) -> None:
        self.assertEqual(_decode_response(b"hello"), "hello")

    def test_str_passed_through(self) -> None:
        self.assertEqual(_decode_response("world"), "world")

    def test_other_type_converted_to_str(self) -> None:
        result = _decode_response(42)
        self.assertEqual(result, "42")


class ParseAppendUidTest(unittest.TestCase):
    def test_valid_appenduid(self) -> None:
        result = _parse_appenduid(b"[APPENDUID 12345 99] APPEND completed.")
        self.assertEqual(result, 99)

    def test_no_appenduid_returns_none(self) -> None:
        result = _parse_appenduid(b"OK APPEND completed.")
        self.assertIsNone(result)

    def test_malformed_too_few_parts_returns_none(self) -> None:
        result = _parse_appenduid(b"[APPENDUID 12345]")
        self.assertIsNone(result)

    def test_invalid_uid_returns_none(self) -> None:
        result = _parse_appenduid(b"[APPENDUID 12345 notanint]")
        self.assertIsNone(result)


class ParseCopyUidTest(unittest.TestCase):
    def test_valid_copyuid(self) -> None:
        result = _parse_copyuid(b"[COPYUID 1 1 99]")
        self.assertEqual(result, 99)

    def test_no_copyuid_returns_none(self) -> None:
        result = _parse_copyuid(b"OK COPY completed.")
        self.assertIsNone(result)

    def test_multi_uid_range_returns_none(self) -> None:
        result = _parse_copyuid(b"[COPYUID 1 1:5 10:14]")
        self.assertIsNone(result)

    def test_multi_uid_comma_returns_none(self) -> None:
        result = _parse_copyuid(b"[COPYUID 1 1,2 10,11]")
        self.assertIsNone(result)

    def test_too_few_parts_returns_none(self) -> None:
        result = _parse_copyuid(b"[COPYUID 1 1]")
        self.assertIsNone(result)

    def test_invalid_uid_returns_none(self) -> None:
        result = _parse_copyuid(b"[COPYUID 1 1 notanint]")
        self.assertIsNone(result)
