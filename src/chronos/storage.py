from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

from chronos.domain import ResourceRef

_MAX_ENCODED_UID_LEN = 180


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
        return self._account_roots.get(account_name, self._root / account_name)

    def list_calendars(self, account_name: str) -> tuple[str, ...]:
        account_dir = self._account_dir(account_name)
        if not account_dir.is_dir():
            return ()
        return tuple(sorted(p.name for p in account_dir.iterdir() if p.is_dir()))

    def list_resources(
        self, account_name: str, calendar_name: str
    ) -> tuple[ResourceRef, ...]:
        calendar_dir = self._account_dir(account_name) / calendar_name
        if not calendar_dir.is_dir():
            return ()
        refs: list[ResourceRef] = []
        for path in sorted(calendar_dir.iterdir()):
            if not path.is_file() or path.suffix != ".ics":
                continue
            if path.name.startswith(".tmp-"):
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
        path = self._path_for(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp-", suffix=".ics", dir=path.parent
        )
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

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
            / ref.calendar_name
            / _uid_to_filename(ref.uid)
        )


def _uid_to_filename(uid: str) -> str:
    encoded = urllib.parse.quote(uid, safe="")
    if len(encoded) > _MAX_ENCODED_UID_LEN:
        digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
        return f"{encoded[:_MAX_ENCODED_UID_LEN]}-{digest}.ics"
    return f"{encoded}.ics"


def _filename_to_uid(filename: str) -> str:
    stem = filename.removesuffix(".ics")
    return urllib.parse.unquote(stem)
