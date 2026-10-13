from __future__ import annotations

import hashlib
import os
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

from chronos.atomic import TEMP_PREFIX, write_atomically
from chronos.domain import ResourceRef

_MAX_ENCODED_UID_LEN = 180

# Windows refuses these as a filename stem, with or without an extension.
_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


def safe_component(name: str) -> str:
    """Encode an untrusted name as exactly one path component.

    Account and calendar names come from the server: a CalDAV
    ``<D:displayname>`` of ``../../../etc/cron.d`` is a directory
    traversal, ``C:\\Windows`` discards the mirror root when joined on
    Windows, and ``\\\\host\\share`` makes the mkdir authenticate
    outwards.  Percent-encoding removes every separator, drive letter
    and reserved character, and is injective -- two different names
    cannot collapse onto one directory.  ``decode_component`` inverts it.
    """
    encoded = urllib.parse.quote(name, safe="")
    # quote() leaves '.' alone, so "." and ".." would survive intact.
    if encoded.strip(".") == "" or encoded.endswith("."):
        encoded = encoded.replace(".", "%2E")
    if encoded.split(".", 1)[0].upper() in _RESERVED_STEMS:
        encoded = f"%{ord(encoded[0]):02X}{encoded[1:]}"
    if not encoded:
        raise MirrorError("empty name cannot be stored")
    return encoded


def decode_component(component: str) -> str:
    """Invert :func:`safe_component`."""
    return urllib.parse.unquote(component)


class MirrorError(OSError):
    pass


class ResourceNotFoundError(MirrorError):
    pass


class VdirMirrorRepository:
    """Vdir-style `.ics` mirror rooted at a single path.

    Layout: `<root>/<account>/<calendar>/<encoded-uid>.ics`.

    An account may sit somewhere else entirely: `account_roots` maps an
    account name to the directory that holds *its* calendars, which is
    what the per-account `mirror_path` in `config.toml` asks for. The
    default for that setting is `<root>/<account>`, the same place this
    layout would have put it, so an account that does not set one keeps
    the files exactly where they already are.

    Writes are crash-safe: bytes go into a temp file in the target
    directory and are promoted via `os.replace` (atomic on a single
    filesystem).
    """

    def __init__(
        self, root: Path, *, account_roots: Mapping[str, Path] | None = None
    ) -> None:
        self._root = root
        self._account_roots = dict(account_roots or {})

    @property
    def root(self) -> Path:
        return self._root

    def _account_dir(self, account_name: str) -> Path:
        explicit = self._account_roots.get(account_name)
        if explicit is not None:
            return explicit
        return self._root / safe_component(account_name)

    def list_calendars(self, account_name: str) -> tuple[str, ...]:
        account_dir = self._account_dir(account_name)
        if not account_dir.is_dir():
            return ()
        return tuple(
            sorted(
                decode_component(p.name) for p in account_dir.iterdir() if p.is_dir()
            )
        )

    def list_resources(
        self, account_name: str, calendar_name: str
    ) -> tuple[ResourceRef, ...]:
        calendar_dir = self._account_dir(account_name) / safe_component(calendar_name)
        if not calendar_dir.is_dir():
            return ()
        refs: list[ResourceRef] = []
        for path in sorted(calendar_dir.iterdir()):
            if not path.is_file() or path.suffix != ".ics":
                continue
            if path.name.startswith(TEMP_PREFIX):
                continue
            uid = _filename_to_uid(path.name)
            refs.append(
                ResourceRef(
                    account_name=account_name,
                    calendar_name=calendar_name,
                    uid=uid,
                )
            )
        return tuple(refs)

    def read(self, ref: ResourceRef) -> bytes:
        path = self._path_for(ref)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ResourceNotFoundError(str(path)) from exc

    def write(self, ref: ResourceRef, data: bytes) -> None:
        write_atomically(self._path_for(ref), data, suffix=".ics")

    def delete(self, ref: ResourceRef) -> None:
        path = self._path_for(ref)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ResourceNotFoundError(str(path)) from exc

    def move(self, source: ResourceRef, target: ResourceRef) -> None:
        src_path = self._path_for(source)
        dst_path = self._path_for(target)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        if not src_path.is_file():
            raise ResourceNotFoundError(str(src_path))
        os.replace(src_path, dst_path)

    def exists(self, ref: ResourceRef) -> bool:
        return self._path_for(ref).is_file()

    def _path_for(self, ref: ResourceRef) -> Path:
        return (
            self._account_dir(ref.account_name)
            / safe_component(ref.calendar_name)
            / _uid_to_filename(ref.uid)
        )


def _uid_to_filename(uid: str) -> str:
    """One ``.ics`` filename for a UID, inverted by `_filename_to_uid`.

    The encoding is `safe_component`'s, not a second one eight lines
    away: a UID is as untrusted as a calendar name -- it comes off the
    server -- so the reserved-stem and empty-name guards apply to it
    too.  A UID of ``NUL`` wrote to the null device on Windows.

    A UID too long to spell out keeps a readable prefix and a digest of
    the whole thing.  The prefix is built character by character, so it
    ends between characters: cutting the *encoding* at 180 bytes split
    ``%E2%82%AC`` into ``...%E2%8``, which is not an escape at all, and
    what `_filename_to_uid` read back off disk was a UID no index row
    had -- so the next sync saw the event as one only the server knew
    about, every time.
    """
    encoded = safe_component(uid)
    if len(encoded) <= _MAX_ENCODED_UID_LEN:
        return f"{encoded}.ics"
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
    return f"{_encoded_prefix(uid, _MAX_ENCODED_UID_LEN)}-{digest}.ics"


def _encoded_prefix(uid: str, limit: int) -> str:
    """As much of *uid*'s encoding as fits in *limit* characters.

    Encoding each character on its own is what keeps the cut on a
    character boundary: one character can become nine encoded ones, and
    neither half of ``%E2%82%AC`` means anything by itself.
    """
    out: list[str] = []
    used = 0
    for char in uid:
        piece = urllib.parse.quote(char, safe="")
        if used + len(piece) > limit:
            break
        out.append(piece)
        used += len(piece)
    return "".join(out)


def _filename_to_uid(filename: str) -> str:
    return decode_component(filename.removesuffix(".ics"))
