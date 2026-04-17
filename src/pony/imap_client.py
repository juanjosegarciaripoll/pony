"""IMAP session wrapper.

Wraps :mod:`imapclient` with a typed interface that matches
:class:`pony.protocols.ImapClientSession`.  All wire-level parsing is
confined here; the sync engine works only with domain types.

Transient errors (``ssl.SSLEOFError``, connection resets, timeouts) are
retried transparently: the session reconnects and replays the failed
command up to ``max_retries`` times with exponential back-off.
"""

from __future__ import annotations

import contextlib
import logging
import ssl
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TypeVar, cast

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .domain import FlagSet, MessageFlag

_T = TypeVar("_T")


@contextmanager
def _imap_errors(context: str = "") -> Iterator[None]:
    """Convert IMAPClientError to OSError at the boundary."""
    try:
        yield
    except IMAPClientError as exc:
        msg = f"{context}: {exc}" if context else str(exc)
        raise OSError(msg) from exc


# Errors that indicate a dropped connection worth retrying.
_TRANSIENT = (
    ssl.SSLEOFError,
    ConnectionResetError,
    BrokenPipeError,
    TimeoutError,
    EOFError,
)

logger = logging.getLogger(__name__)


class ImapAuthError(ConnectionError):
    """Raised when IMAP login is rejected by the server."""

    def __init__(self, username: str, host: str) -> None:
        self.username = username
        self.host = host
        super().__init__(f"IMAP authentication failed for {username}@{host}")


# ---------------------------------------------------------------------------
# IMAP flag mapping
# ---------------------------------------------------------------------------

_IMAP_TO_LOCAL: dict[bytes, MessageFlag] = {
    b"\\Seen": MessageFlag.SEEN,
    b"\\Answered": MessageFlag.ANSWERED,
    b"\\Flagged": MessageFlag.FLAGGED,
    b"\\Deleted": MessageFlag.DELETED,
    b"\\Draft": MessageFlag.DRAFT,
}

_LOCAL_TO_IMAP: dict[MessageFlag, bytes] = {v: k for k, v in _IMAP_TO_LOCAL.items()}


def _parse_imap_flags(
    flags: tuple[bytes, ...] | Sequence[bytes],
) -> FlagSet:
    """Convert an imapclient flag tuple to (known_flags, extra_flags)."""
    known: set[MessageFlag] = set()
    extra: set[str] = set()
    for flag in flags:
        local = _IMAP_TO_LOCAL.get(flag)
        if local is not None:
            known.add(local)
        else:
            decoded = flag.decode(errors="replace")
            if decoded:
                extra.add(decoded)
    return frozenset(known), frozenset(extra)


def _format_imap_flags(
    flags: frozenset[MessageFlag],
    extra: frozenset[str] = frozenset(),
) -> list[bytes]:
    """Build a flag list for imapclient's set_flags / append."""
    result = [_LOCAL_TO_IMAP[f] for f in sorted(flags) if f in _LOCAL_TO_IMAP]
    result.extend(s.encode() for s in sorted(extra))
    return result


# ---------------------------------------------------------------------------
# ImapSession
# ---------------------------------------------------------------------------


class ImapSession:
    """One authenticated IMAP session over TLS.

    Implements :class:`pony.protocols.ImapClientSession`.
    Uses :mod:`imapclient` for robust protocol handling, UTF-7 folder
    names, and optional COMPRESS=DEFLATE.

    Transient connection errors (SSL EOF, reset, timeout) are retried
    automatically: the session reconnects and replays the failed command
    up to *max_retries* times with exponential back-off.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 993,
        ssl: bool = True,
        username: str,
        password: str,
        max_retries: int = 3,
    ) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._username = username
        self._password = password
        self._max_retries = max_retries
        self._conn = self._new_connection()
        self._selected: str | None = None

    def _new_connection(self) -> IMAPClient:
        """Open, authenticate, and optionally compress a fresh connection."""
        logger.debug(
            "Connecting to %s:%d (ssl=%s)",
            self._host, self._port, self._ssl,
        )
        conn = IMAPClient(self._host, port=self._port, ssl=self._ssl)
        logger.debug("Logging in as %s", self._username)
        try:
            conn.login(self._username, self._password)
        except LoginError as exc:
            raise ImapAuthError(self._username, self._host) from exc
        logger.debug("Logged in as %s@%s", self._username, self._host)

        if b"COMPRESS=DEFLATE" in conn.capabilities():
            try:
                conn.compress()  # pyright: ignore[reportAttributeAccessIssue]
                logger.debug("COMPRESS=DEFLATE enabled")
            except Exception:  # noqa: BLE001
                logger.debug("COMPRESS=DEFLATE not available")
        return conn

    def _reconnect(self) -> None:
        """Drop the current connection and open a fresh one."""
        logger.info(
            "Reconnecting to %s:%d", self._host, self._port,
        )
        with contextlib.suppress(Exception):
            self._conn.logout()
        self._conn = self._new_connection()
        self._selected = None

    def _retry(self, fn: Callable[[], _T], label: str = "") -> _T:
        """Call *fn*, retrying on transient connection errors.

        On each failure the session reconnects and re-selects the
        previously selected folder before retrying.
        """
        delay = 1.0
        for attempt in range(1, self._max_retries + 1):
            try:
                return fn()
            except _TRANSIENT as exc:
                if attempt == self._max_retries:
                    raise
                logger.info(
                    "%s: transient error (attempt %d/%d): %s"
                    " — reconnecting",
                    label or "IMAP", attempt,
                    self._max_retries, exc,
                )
                self._reconnect()
                time.sleep(delay)
                delay = min(delay * 2, 10.0)
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------
    # Folder management
    # ------------------------------------------------------------------

    def list_folders(self) -> Sequence[str]:
        """Return all visible mailbox names."""
        def _do() -> Sequence[str]:
            with _imap_errors("LIST"):
                raw = self._conn.list_folders()
            folders: list[str] = []
            for _flags, _delimiter, name in raw:
                if isinstance(name, bytes):
                    name = name.decode(errors="replace")
                folders.append(name)
            return folders
        return self._retry(_do, "LIST")

    def get_uid_validity(self, folder_name: str) -> int:
        """SELECT the folder and return its UIDVALIDITY."""
        def _do() -> int:
            self._do_select(folder_name)
            with _imap_errors(f"STATUS {folder_name!r}"):
                info = self._conn.folder_status(
                    folder_name, ["UIDVALIDITY"],
                )
            return int(info.get(b"UIDVALIDITY", 0))
        return self._retry(_do, f"UIDVALIDITY {folder_name}")

    def _do_select(self, folder_name: str) -> None:
        logger.debug("SELECT %s", folder_name)
        with _imap_errors(f"SELECT {folder_name!r}"):
            self._conn.select_folder(folder_name)
        self._selected = folder_name

    def _ensure_selected(self, folder_name: str) -> None:
        if self._selected != folder_name:
            self._do_select(folder_name)

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def fetch_uid_to_message_id(
        self, folder_name: str
    ) -> dict[int, tuple[str, FlagSet]]:
        """Fetch UID -> (Message-ID, flags) mapping for all messages in the folder."""
        def _do() -> dict[int, tuple[str, FlagSet]]:
            self._ensure_selected(folder_name)
            logger.debug(
                "FETCH all (FLAGS, Message-ID header) on %s",
                folder_name,
            )
            with _imap_errors(f"FETCH on {folder_name!r}"):
                data = self._conn.fetch(
                    "1:*",
                    ["FLAGS",
                     "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"],
                )
            result: dict[int, tuple[str, FlagSet]] = {}
            for uid, msg_data in data.items():
                header_bytes = cast(bytes, msg_data.get(
                    b"BODY[HEADER.FIELDS (MESSAGE-ID)]", b"",
                ))
                mid = _extract_message_id(header_bytes)
                raw_flags = cast(tuple[bytes, ...], msg_data.get(b"FLAGS", ()))
                result[uid] = (mid, _parse_imap_flags(raw_flags))
            return result
        return self._retry(_do, f"FETCH MID {folder_name}")

    def fetch_flags(
        self, folder_name: str, uids: Sequence[int],
    ) -> dict[int, FlagSet]:
        """Return current flags for the given UIDs (batched)."""
        if not uids:
            return {}
        def _do() -> dict[int, FlagSet]:
            self._ensure_selected(folder_name)
            result: dict[int, FlagSet] = {}
            batch_size = 500
            for start in range(0, len(uids), batch_size):
                batch = uids[start : start + batch_size]
                logger.debug(
                    "FETCH %d UIDs (FLAGS) on %s",
                    len(batch), folder_name,
                )
                with _imap_errors(f"FETCH FLAGS on {folder_name!r}"):
                    data = self._conn.fetch(batch, ["FLAGS"])
                for uid, msg_data in data.items():
                    raw_flags = cast(tuple[bytes, ...], msg_data.get(b"FLAGS", ()))
                    result[uid] = _parse_imap_flags(raw_flags)
            return result
        return self._retry(_do, f"FETCH FLAGS {folder_name}")

    def fetch_message_bytes(self, folder_name: str, uid: int) -> bytes:
        """Fetch the full RFC 5322 message for one UID."""
        result = self.fetch_messages_batch(folder_name, [uid])
        if uid not in result:
            raise KeyError(
                f"no body data for UID {uid} in {folder_name!r}",
            )
        return result[uid]

    def fetch_messages_batch(
        self, folder_name: str, uids: Sequence[int],
    ) -> dict[int, bytes]:
        """Fetch full RFC 5322 messages for multiple UIDs (batched)."""
        if not uids:
            return {}
        def _do() -> dict[int, bytes]:
            self._ensure_selected(folder_name)
            result: dict[int, bytes] = {}
            batch_size = 25
            for start in range(0, len(uids), batch_size):
                batch = uids[start : start + batch_size]
                logger.debug(
                    "FETCH %d msgs (RFC822) on %s",
                    len(batch), folder_name,
                )
                with _imap_errors(
                    f"FETCH RFC822 on {folder_name!r}",
                ):
                    data = self._conn.fetch(batch, ["RFC822"])
                for uid, msg_data in data.items():
                    body = msg_data.get(b"RFC822", b"")
                    if isinstance(body, bytes) and body:
                        result[uid] = body
            return result
        return self._retry(_do, f"FETCH RFC822 {folder_name}")

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def store_flags(
        self,
        folder_name: str,
        uid: int,
        flags: frozenset[MessageFlag],
        extra_imap_flags: frozenset[str] = frozenset(),
    ) -> None:
        """Replace flags on the server (absolute STORE)."""
        flag_list = _format_imap_flags(flags, extra_imap_flags)
        def _do() -> None:
            self._ensure_selected(folder_name)
            logger.debug(
                "STORE %d FLAGS %s on %s", uid, flag_list,
                folder_name,
            )
            with _imap_errors(f"STORE on {folder_name!r}"):
                self._conn.set_flags([uid], flag_list, silent=True)
        self._retry(_do, f"STORE {folder_name}")

    def append_message(
        self,
        folder_name: str,
        raw_message: bytes,
        flags: frozenset[MessageFlag],
        extra_imap_flags: frozenset[str] = frozenset(),
    ) -> None:
        """Upload a message to the server via IMAP APPEND."""
        flag_list = _format_imap_flags(flags, extra_imap_flags)
        def _do() -> None:
            logger.debug(
                "APPEND to %s FLAGS %s (%d bytes)",
                folder_name, flag_list, len(raw_message),
            )
            with _imap_errors(f"APPEND to {folder_name!r}"):
                self._conn.append(
                    folder_name, raw_message, flag_list,
                )
        self._retry(_do, f"APPEND {folder_name}")

    def mark_deleted(self, folder_name: str, uid: int) -> None:
        """Set \\Deleted on one message."""
        def _do() -> None:
            self._ensure_selected(folder_name)
            logger.debug("DELETE %d on %s", uid, folder_name)
            with _imap_errors(f"DELETE on {folder_name!r}"):
                self._conn.delete_messages([uid])
        self._retry(_do, f"DELETE {folder_name}")

    def expunge(self, folder_name: str) -> None:
        """Expunge all \\Deleted messages in the given folder."""
        def _do() -> None:
            self._ensure_selected(folder_name)
            logger.debug("EXPUNGE on %s", folder_name)
            with _imap_errors(f"EXPUNGE on {folder_name!r}"):
                self._conn.expunge()
        self._retry(_do, f"EXPUNGE {folder_name}")

    def create_folder(self, folder_name: str) -> None:
        """Create a folder on the server (idempotent — no-op if it exists)."""
        def _do() -> None:
            if self._conn.folder_exists(folder_name):
                return
            logger.debug("CREATE %s", folder_name)
            with _imap_errors(f"CREATE {folder_name!r}"):
                self._conn.create_folder(folder_name)
        self._retry(_do, f"CREATE {folder_name}")

    def move_message(
        self, source_folder: str, uid: int, target_folder: str,
    ) -> None:
        """Move one message from *source_folder* to *target_folder*.

        The target folder must already exist on the server; callers
        should invoke :meth:`create_folder` first when in doubt.  Uses
        ``UID MOVE`` (RFC 6851) when the server advertises it, otherwise
        falls back to ``UID COPY`` + ``STORE +FLAGS \\Deleted`` +
        ``EXPUNGE`` on the source.
        """
        def _do() -> None:
            self._ensure_selected(source_folder)
            if b"MOVE" in self._conn.capabilities():
                logger.debug(
                    "UID MOVE %d from %s to %s",
                    uid, source_folder, target_folder,
                )
                with _imap_errors(
                    f"MOVE on {source_folder!r} -> {target_folder!r}",
                ):
                    self._conn.move([uid], target_folder)
                return
            logger.debug(
                "UID COPY %d from %s to %s (MOVE not supported)",
                uid, source_folder, target_folder,
            )
            with _imap_errors(
                f"COPY on {source_folder!r} -> {target_folder!r}",
            ):
                self._conn.copy([uid], target_folder)
            with _imap_errors(f"DELETE on {source_folder!r}"):
                self._conn.delete_messages([uid])
            with _imap_errors(f"EXPUNGE on {source_folder!r}"):
                self._conn.expunge()

        self._retry(_do, f"MOVE {source_folder}->{target_folder}")

    # ------------------------------------------------------------------
    # Server summary helpers (not part of ImapClientSession protocol)
    # ------------------------------------------------------------------

    def get_folder_status(self, folder_name: str) -> tuple[int, int]:
        """Return ``(message_count, unseen_count)`` via IMAP STATUS."""
        def _do() -> tuple[int, int]:
            logger.debug("STATUS %s (MESSAGES UNSEEN)", folder_name)
            with _imap_errors(f"STATUS {folder_name!r}"):
                info = self._conn.folder_status(
                    folder_name, ["MESSAGES", "UNSEEN"],
                )
            messages = int(info.get(b"MESSAGES", 0))
            unseen = int(info.get(b"UNSEEN", 0))
            return messages, unseen
        return self._retry(_do, f"STATUS {folder_name}")

    def fetch_last_message_date(
        self, folder_name: str,
    ) -> str | None:
        """Return the INTERNALDATE of the last message, or None."""
        def _do() -> str | None:
            self._do_select(folder_name)
            try:
                data = self._conn.fetch("*", ["INTERNALDATE"])
            except Exception:  # noqa: BLE001
                return None
            for _uid, msg_data in data.items():
                date = msg_data.get(b"INTERNALDATE")
                if date is not None:
                    return str(date)
            return None
        return self._retry(_do, f"INTERNALDATE {folder_name}")

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def logout(self) -> None:
        """Close the session cleanly."""
        with contextlib.suppress(Exception):
            self._conn.logout()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_message_id(header_bytes: bytes) -> str:
    """Pull the Message-ID value from a partial header block."""
    text = header_bytes.decode(errors="replace")
    for line in text.splitlines():
        if line.lower().startswith("message-id:"):
            return line.split(":", 1)[1].strip()
    return ""
