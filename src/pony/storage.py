"""Local mirror storage backends."""

from __future__ import annotations

import atexit
import concurrent.futures
import mailbox
import os
import re
import socket
import sys
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path

from .domain import FolderRef, MessageFlag
from .protocols import MirrorRepository

# ---------------------------------------------------------------------------
# Maildir backend
# ---------------------------------------------------------------------------


class MaildirMirrorRepository(MirrorRepository):
    """Maildir-backed mirror repository."""

    def __init__(self, *, account_name: str, root_dir: Path) -> None:
        self._account_name = account_name
        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)
        for subdir in ("cur", "new", "tmp"):
            (self._root_dir / subdir).mkdir(parents=True, exist_ok=True)
        self._ensured_dirs: set[str] = set()
        self._write_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._write_futures: list[concurrent.futures.Future[int]] = []

    def list_folders(self, *, account_name: str) -> tuple[FolderRef, ...]:
        self._require_account(account_name)
        folder_names = ["INBOX"]
        for candidate in sorted(self._root_dir.glob(".*")):
            if not candidate.is_dir():
                continue
            name = candidate.name[1:]
            if not name:
                continue
            # The on-disk name is already sanitized; use it as-is.
            # For IMAP accounts the server provides the real folder names.
            folder_names.append(name)
        return tuple(
            FolderRef(account_name=self._account_name, folder_name=name)
            for name in folder_names
        )

    def _ensure_folder_dirs(self, folder_name: str) -> Path:
        """Create Maildir subdirs for *folder_name* (cached)."""
        if folder_name not in self._ensured_dirs:
            folder_path = self._maildir_folder_path(folder_name)
            for subdir in ("cur", "new", "tmp"):
                (folder_path / subdir).mkdir(parents=True, exist_ok=True)
            self._ensured_dirs.add(folder_name)
            return folder_path
        return self._maildir_folder_path(folder_name)

    def _make_filename(self) -> str:
        """Generate a unique Maildir filename."""
        hostname = socket.gethostname()
        pid = os.getpid()
        timestamp = time.time()
        return f"{timestamp:.6f}.{pid}.{hostname}"

    def store_message(self, *, folder: FolderRef, raw_message: bytes) -> str:
        self._require_folder(folder)
        folder_path = self._ensure_folder_dirs(folder.folder_name)
        filename = self._make_filename()
        new_path = folder_path / "new" / filename
        new_path.write_bytes(raw_message)
        return filename

    def store_message_async(
        self,
        *,
        folder: FolderRef,
        raw_message: bytes,
    ) -> str:
        """Generate the filename and return immediately; write in background.

        The actual ``write_bytes`` is submitted to a thread pool.  Call
        :meth:`flush_writes` to wait for all pending writes to complete.
        """
        self._require_folder(folder)
        folder_path = self._ensure_folder_dirs(folder.folder_name)
        filename = self._make_filename()
        new_path = folder_path / "new" / filename
        if self._write_pool is None:
            self._write_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="mirror-write",
            )
        future = self._write_pool.submit(new_path.write_bytes, raw_message)
        self._write_futures.append(future)
        return filename

    def flush_writes(self) -> None:
        """Wait for all pending async writes to complete.

        Raises the first exception encountered, if any.
        """
        futures = self._write_futures
        self._write_futures = []
        if not futures:
            return
        done, _ = concurrent.futures.wait(futures)
        for f in done:
            f.result()  # raises if the write failed

    def list_messages(self, *, folder: FolderRef) -> tuple[str, ...]:
        self._require_folder(folder)
        maildir = self._open_maildir(folder_name=folder.folder_name)
        return tuple(sorted(str(key) for key in maildir.keys()))  # noqa: SIM118

    def get_message_bytes(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
    ) -> bytes:
        self._require_folder(folder)
        path = self._find_message_file(
            folder_name=folder.folder_name,
            storage_key=storage_key,
        )
        if path is None:
            raise KeyError(f"message not found: {storage_key}")
        return path.read_bytes()

    def _find_message_file(
        self,
        *,
        folder_name: str,
        storage_key: str,
    ) -> Path | None:
        """Locate the Maildir file for a storage_key (handles flag suffixes).

        Uses glob instead of a full directory scan so lookup is fast even
        in folders with thousands of messages.
        """
        folder_path = self._maildir_folder_path(folder_name)
        for subdir in ("cur", "new"):
            d = folder_path / subdir
            if not d.exists():
                continue
            # Exact match first (no flag suffix).
            exact = d / storage_key
            if exact.exists():
                return exact
        # Suffix fallback: escape glob meta-characters only when needed.
        escaped = _glob_escape(storage_key)
        for subdir in ("cur", "new"):
            d = folder_path / subdir
            if not d.exists():
                continue
            for pattern in (f"{escaped}!*", f"{escaped}:*"):
                matches = list(d.glob(pattern))
                if matches:
                    return matches[0]
        return None

    def set_flags(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
        flags: frozenset[MessageFlag],
    ) -> None:
        self._require_folder(folder)
        path = self._find_message_file(
            folder_name=folder.folder_name,
            storage_key=storage_key,
        )
        if path is None:
            raise KeyError(f"message not found: {storage_key}")
        # Rename the file with Maildir flag suffix in cur/.
        flag_str = _maildir_flags(flags)
        folder_path = self._maildir_folder_path(folder.folder_name)
        cur_dir = folder_path / "cur"
        cur_dir.mkdir(parents=True, exist_ok=True)
        new_name = f"{storage_key}!2,{flag_str}"
        dest = cur_dir / new_name
        path.rename(dest)

    def delete_message(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
    ) -> None:
        self._require_folder(folder)
        path = self._find_message_file(
            folder_name=folder.folder_name,
            storage_key=storage_key,
        )
        if path is not None:
            path.unlink(missing_ok=True)

    def move_message_to_folder(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
        target_folder: str,
    ) -> str:
        """Rename the Maildir file into *target_folder*'s directory.

        The filename (and therefore the storage_key) is preserved —
        Maildir filenames are globally unique, so the returned key still
        identifies the same physical message.
        """
        self._require_folder(folder)
        if target_folder == folder.folder_name:
            return storage_key
        src = self._find_message_file(
            folder_name=folder.folder_name,
            storage_key=storage_key,
        )
        if src is None:
            raise KeyError(f"message not found: {storage_key}")
        self._ensure_folder_dirs(target_folder)
        target_path = self._maildir_folder_path(target_folder)
        subdir = src.parent.name  # "cur" or "new"
        dest_dir = target_path / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        src.rename(dest)
        return storage_key

    def create_folder(self, *, account_name: str, folder_name: str) -> None:
        """Create an empty Maildir-backed folder (idempotent)."""
        self._require_account(account_name)
        self._ensure_folder_dirs(folder_name)

    def folder_mtime_ns(self, *, folder: FolderRef) -> int:
        """Max mtime across ``cur/`` and ``new/`` — the directories that
        receive file renames when mail is delivered or classified."""
        self._require_folder(folder)
        folder_path = self._maildir_folder_path(folder.folder_name)
        latest = 0
        for subdir in ("cur", "new"):
            path = folder_path / subdir
            try:
                mtime = path.stat().st_mtime_ns
            except (FileNotFoundError, OSError):
                continue
            if mtime > latest:
                latest = mtime
        return latest

    def _open_maildir(self, *, folder_name: str) -> mailbox.Maildir:
        if folder_name == "INBOX":
            maildir = mailbox.Maildir(self._root_dir, create=True)
            maildir.colon = "!"
            return maildir

        folder_path = self._maildir_folder_path(folder_name)
        folder_path.mkdir(parents=True, exist_ok=True)
        for subdir in ("cur", "new", "tmp"):
            (folder_path / subdir).mkdir(parents=True, exist_ok=True)
        maildir = mailbox.Maildir(folder_path, create=True)
        maildir.colon = "!"
        return maildir

    def _maildir_folder_path(self, folder_name: str) -> Path:
        if folder_name == "INBOX":
            return self._root_dir
        encoded = _sanitize_for_path(folder_name)
        return self._root_dir / f".{encoded}"

    def _require_account(self, account_name: str) -> None:
        if account_name != self._account_name:
            raise ValueError(f"unknown account: {account_name}")

    def _require_folder(self, folder: FolderRef) -> None:
        self._require_account(folder.account_name)


# ---------------------------------------------------------------------------
# mbox backend
# ---------------------------------------------------------------------------


class MboxMirrorRepository(MirrorRepository):
    """mbox-backed mirror repository.

    mbox files are kept open for the lifetime of the repository.  Parsing the
    table of contents on every operation is O(file size) and would make bulk
    access quadratic.  Handles are flushed after every write and released via
    an atexit handler and ``__del__`` so clean-up happens automatically on
    normal process exit or garbage collection.

    mbox is not crash-safe: a hard kill before a flush can leave the file in
    an inconsistent state.  Prefer Maildir for accounts where durability
    matters.
    """

    def __init__(self, *, account_name: str, root_dir: Path) -> None:
        self._account_name = account_name
        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._open_handles: dict[str, mailbox.mbox] = {}
        atexit.register(self._close_all)

    def _close_all(self) -> None:
        """Flush and close every open mbox handle."""
        if self._open_handles:
            print(
                "Flushing mail storage — do not interrupt…",
                file=sys.stderr,
                flush=True,
            )
        for mbox in self._open_handles.values():
            try:
                mbox.flush()
                mbox.close()
            except Exception:  # noqa: BLE001
                pass
        self._open_handles.clear()

    def __del__(self) -> None:
        self._close_all()

    def list_folders(self, *, account_name: str) -> tuple[FolderRef, ...]:
        self._require_account(account_name)
        folder_names: list[str] = []
        for candidate in sorted(self._root_dir.glob("*.mbox")):
            folder_names.append(_decode_folder_name(candidate.stem))
        if "INBOX" not in folder_names:
            folder_names.insert(0, "INBOX")
        return tuple(
            FolderRef(account_name=self._account_name, folder_name=name)
            for name in sorted(set(folder_names))
        )

    def store_message(self, *, folder: FolderRef, raw_message: bytes) -> str:
        self._require_folder(folder)
        mbox = self._open_mbox(folder_name=folder.folder_name)
        parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
        message = mailbox.mboxMessage(parsed)
        key = str(mbox.add(message))
        mbox.flush()
        return key

    def list_messages(self, *, folder: FolderRef) -> tuple[str, ...]:
        self._require_folder(folder)
        mbox = self._open_mbox(folder_name=folder.folder_name)
        # mbox.keys() calls iteritems() which calls get_message() for every
        # entry.  get_message() decodes the "From " envelope line as ASCII and
        # raises UnicodeDecodeError on non-ASCII mail.  _toc maps int keys to
        # byte-offset pairs; read it directly to avoid per-message I/O.
        # _toc is None until _generate_toc() is called (it's lazy — triggered
        # by _lookup() on first message access).  Force it here so empty
        # mailboxes return [] rather than crashing.
        if mbox._toc is None:  # type: ignore[attr-defined]
            mbox._generate_toc()  # type: ignore[attr-defined]
        return tuple(sorted(str(k) for k in mbox._toc))  # type: ignore[attr-defined]

    def get_message_bytes(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
    ) -> bytes:
        self._require_folder(folder)
        mbox = self._open_mbox(folder_name=folder.folder_name)
        key = int(storage_key)
        return _mbox_get_bytes(mbox, key, storage_key)

    def set_flags(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
        flags: frozenset[MessageFlag],
    ) -> None:
        self._require_folder(folder)
        mbox = self._open_mbox(folder_name=folder.folder_name)
        key = int(storage_key)
        updated = mailbox.mboxMessage(_mbox_get_message(mbox, key, storage_key))
        _set_mbox_flags(updated, flags=flags)
        mbox[key] = updated  # type: ignore[index]  # typeshed: str; runtime: int
        mbox.flush()

    def delete_message(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
    ) -> None:
        self._require_folder(folder)
        mbox = self._open_mbox(folder_name=folder.folder_name)
        key = int(storage_key)
        del mbox[key]  # type: ignore[arg-type]  # typeshed: str; runtime: int
        mbox.flush()

    def move_message_to_folder(
        self,
        *,
        folder: FolderRef,
        storage_key: str,
        target_folder: str,
    ) -> str:
        """Copy the message into *target_folder*'s mbox and remove from source.

        mbox keys are per-file, so the returned storage_key is new.
        """
        self._require_folder(folder)
        if target_folder == folder.folder_name:
            return storage_key
        src = self._open_mbox(folder_name=folder.folder_name)
        key = int(storage_key)
        message = _mbox_get_message(src, key, storage_key)
        dest = self._open_mbox(folder_name=target_folder)
        new_key = str(dest.add(mailbox.mboxMessage(message)))
        dest.flush()
        del src[key]  # type: ignore[arg-type]  # typeshed: str; runtime: int
        src.flush()
        return new_key

    def create_folder(self, *, account_name: str, folder_name: str) -> None:
        """Create an empty mbox-backed folder (idempotent)."""
        self._require_account(account_name)
        # _open_mbox opens the file with create=True; flush materialises
        # an empty file on disk even with no messages added.
        mbox = self._open_mbox(folder_name=folder_name)
        mbox.flush()

    def folder_mtime_ns(self, *, folder: FolderRef) -> int:
        """mbox files are a single flat file per folder — stat it."""
        self._require_folder(folder)
        path = self._folder_file(folder.folder_name)
        try:
            return path.stat().st_mtime_ns
        except (FileNotFoundError, OSError):
            return 0

    def _open_mbox(self, *, folder_name: str) -> mailbox.mbox:
        # mailbox.mbox uses int keys at runtime; typeshed stubs incorrectly
        # declare str keys inherited from the Mailbox base class.  Call sites
        # that pass int keys carry a targeted type: ignore comment.
        if folder_name not in self._open_handles:
            path = self._folder_file(folder_name)
            self._open_handles[folder_name] = mailbox.mbox(path, create=True)
        return self._open_handles[folder_name]

    def _folder_file(self, folder_name: str) -> Path:
        encoded = _encode_folder_name(folder_name)
        return self._root_dir / f"{encoded}.mbox"

    def _require_account(self, account_name: str) -> None:
        if account_name != self._account_name:
            raise ValueError(f"unknown account: {account_name}")

    def _require_folder(self, folder: FolderRef) -> None:
        self._require_account(folder.account_name)


# ---------------------------------------------------------------------------
# mbox helpers
# ---------------------------------------------------------------------------


def _mbox_get_bytes(mbox: mailbox.mbox, key: int, storage_key: str) -> bytes:
    """Return the raw RFC 5322 bytes for *key* without touching the From line.

    ``mbox.get()`` decodes the "From " envelope line as pure ASCII, which
    raises ``UnicodeDecodeError`` on any non-ASCII byte in that line.
    ``get_file()`` returns a binary view of the message body (From line
    excluded when *from_* is False, the default) and never triggers that
    decode.
    """
    try:
        f = mbox.get_file(key)  # type: ignore[arg-type]  # typeshed: str; runtime: int
    except KeyError:
        raise KeyError(f"message not found: {storage_key}") from None
    return f.read()


def _mbox_get_message(
    mbox: mailbox.mbox, key: int, storage_key: str
) -> mailbox.mboxMessage:
    """Return an mboxMessage for *key* without crashing on non-ASCII From lines.

    ``mbox.get()`` calls ``set_from(...decode('ASCII'))`` on the envelope line,
    which raises ``UnicodeDecodeError`` for non-ASCII mail.  This helper reads
    the raw bytes via ``_toc`` / ``_file`` and decodes the From line with
    ``errors='replace'`` so all mail is readable.
    """
    try:
        toc: dict[int, tuple[int, int]] = mbox._toc  # type: ignore[attr-defined]
        start, stop = toc[key]
    except KeyError:
        raise KeyError(f"message not found: {storage_key}") from None
    mbox._file.seek(start)  # type: ignore[attr-defined]
    raw = mbox._file.read(stop - start)  # type: ignore[attr-defined]
    from_line, sep, body = raw.partition(b"\n")
    parsed = BytesParser(policy=policy.compat32).parsebytes(body if sep else raw)
    msg = mailbox.mboxMessage(parsed)
    if from_line.startswith(b"From "):
        msg.set_from(from_line[5:].decode("ascii", errors="replace"))
    return msg


# ---------------------------------------------------------------------------
# Flag helpers
# ---------------------------------------------------------------------------


def _maildir_flags(flags: frozenset[MessageFlag]) -> str:
    mapped: list[str] = []
    mapping = {
        MessageFlag.DRAFT: "D",
        MessageFlag.FLAGGED: "F",
        MessageFlag.ANSWERED: "R",
        MessageFlag.SEEN: "S",
        MessageFlag.DELETED: "T",
    }
    for flag in sorted(flags, key=lambda item: item.value):
        code = mapping.get(flag)
        if code is not None:
            mapped.append(code)
    return "".join(mapped)


def _set_mbox_flags(
    message: mailbox.mboxMessage, *, flags: frozenset[MessageFlag]
) -> None:
    """Write Status and X-Status headers onto an mbox message in-place."""
    status = "RO" if MessageFlag.SEEN in flags else "O"
    x_status_codes: list[str] = []
    mapping = {
        MessageFlag.ANSWERED: "A",
        MessageFlag.FLAGGED: "F",
        MessageFlag.DELETED: "D",
        MessageFlag.DRAFT: "T",
    }
    for flag in sorted(flags, key=lambda item: item.value):
        code = mapping.get(flag)
        if code is not None:
            x_status_codes.append(code)

    if "Status" in message:
        del message["Status"]
    if "X-Status" in message:
        del message["X-Status"]

    message["Status"] = status
    if x_status_codes:
        message["X-Status"] = "".join(x_status_codes)


_UNSAFE_PATH_RE = re.compile(r'[/\\<>:"|?*\x00-\x1f]')

_GLOB_META_RE = re.compile(r"([\[\]?*])")


def _glob_escape(name: str) -> str:
    """Escape glob meta-characters so *name* matches literally in Path.glob."""
    return _GLOB_META_RE.sub(r"[\1]", name)


def _sanitize_for_path(name: str) -> str:
    """Replace characters unsafe for filesystem paths with dots.

    Handles ``/``, ``\\``, and any other character that could cause
    path traversal or creation failures on Windows or Unix.
    """
    return _UNSAFE_PATH_RE.sub(".", name)


def _encode_folder_name(folder_name: str) -> str:
    return _sanitize_for_path(folder_name)


def _decode_folder_name(encoded: str) -> str:
    # Decoding is best-effort: some information is lost when multiple
    # different characters collapse to '.'.  For mbox this is used only
    # in list_folders which reconstructs folder names from filenames.
    return encoded
