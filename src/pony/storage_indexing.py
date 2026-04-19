"""Project mirror-stored messages into the SQLite index."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .domain import FolderRef, MessageRef
from .message_projection import project_rfc822_message
from .protocols import IndexRepository, MirrorRepository

RescanProgress = Callable[[str, int, int], None]
"""``(message, current, total)`` callback.  ``total == 0`` means unknown."""

# Pull the RFC 5322 Message-ID header out of the raw bytes so the index's
# MessageRef carries the same identity the IMAP sync path produces.  Falls
# back to the storage_key when the header is missing (some legacy or
# malformed messages).
_MESSAGE_ID_RE = re.compile(
    rb"^Message-ID:\s*(.*(?:\r?\n[ \t]+.*)*)",
    re.MULTILINE | re.IGNORECASE,
)


def _extract_message_id(raw: bytes) -> str:
    match = _MESSAGE_ID_RE.search(raw)
    if match is None:
        return ""
    value = match.group(1).decode("ascii", errors="replace")
    return " ".join(value.split())


def ingest_account_from_mirror(
    *,
    mirror_repository: MirrorRepository,
    index_repository: IndexRepository,
    account_name: str,
) -> int:
    """Read mirror messages and upsert projected metadata into index."""
    upserted = 0
    folders = mirror_repository.list_folders(account_name=account_name)
    for folder in folders:
        upserted += ingest_folder_from_mirror(
            mirror_repository=mirror_repository,
            index_repository=index_repository,
            folder=folder,
        )
    return upserted


def ingest_folder_from_mirror(
    *,
    mirror_repository: MirrorRepository,
    index_repository: IndexRepository,
    folder: FolderRef,
) -> int:
    """Read one folder from mirror storage and update index rows."""
    upserted = 0
    for storage_key in mirror_repository.list_messages(folder=folder):
        _ingest_one(
            mirror_repository=mirror_repository,
            index_repository=index_repository,
            folder=folder,
            storage_key=storage_key,
        )
        upserted += 1
    return upserted


def _ingest_one(
    *,
    mirror_repository: MirrorRepository,
    index_repository: IndexRepository,
    folder: FolderRef,
    storage_key: str,
) -> None:
    raw_message = mirror_repository.get_message_bytes(
        folder=folder, storage_key=storage_key,
    )
    rfc5322_id = _extract_message_id(raw_message) or storage_key
    message_ref = MessageRef(
        account_name=folder.account_name,
        folder_name=folder.folder_name,
        rfc5322_id=rfc5322_id,
    )
    projected = project_rfc822_message(
        message_ref=message_ref,
        raw_message=raw_message,
        storage_key=storage_key,
    )
    index_repository.upsert_message(message=projected)


@dataclass(frozen=True, slots=True)
class RescanResult:
    """Summary returned by :func:`rescan_local_account`."""

    added: int
    removed: int


@dataclass(frozen=True, slots=True)
class _FolderPlan:
    folder: FolderRef
    new_keys: tuple[str, ...]
    gone: tuple[tuple[str, MessageRef], ...]


def rescan_local_account(
    *,
    mirror_repository: MirrorRepository,
    index_repository: IndexRepository,
    account_name: str,
    on_plan: Callable[[RescanResult], None] | None = None,
    progress: RescanProgress | None = None,
) -> RescanResult:
    """Delta-scan a local account's mirror against the index.

    Compares the set of ``storage_key``s present on disk (per folder) with
    the set already recorded in the index and reconciles the difference:

    - New files on disk are projected and upserted.
    - Rows whose ``storage_key`` is no longer on disk are deleted.

    Rows with an empty ``storage_key`` are skipped by the prune step:
    those are pending-append rows produced by local compose / archive and
    must be preserved for the sync engine to push upstream.

    The plan is computed upfront (cheap — metadata only).  When the delta
    is non-empty, ``on_plan`` is fired once with the totals so callers can
    tell the user what is about to happen before the expensive work starts.
    ``progress`` is fired per item during execution with account-wide
    ``(current, total)`` counts.
    """
    plans: list[_FolderPlan] = []
    for folder in mirror_repository.list_folders(account_name=account_name):
        disk_keys = set(mirror_repository.list_messages(folder=folder))
        indexed = {
            m.storage_key: m
            for m in index_repository.list_folder_messages(folder=folder)
            if m.storage_key
        }
        new_keys = tuple(sorted(disk_keys - set(indexed)))
        gone = tuple(
            (key, indexed[key].message_ref)
            for key in sorted(set(indexed) - disk_keys)
        )
        if new_keys or gone:
            plans.append(_FolderPlan(folder=folder, new_keys=new_keys, gone=gone))

    planned = RescanResult(
        added=sum(len(p.new_keys) for p in plans),
        removed=sum(len(p.gone) for p in plans),
    )
    if planned.added == 0 and planned.removed == 0:
        return planned

    if on_plan is not None:
        on_plan(planned)

    total = planned.added + planned.removed
    done = 0
    with index_repository.connection():
        for plan in plans:
            for key in plan.new_keys:
                _ingest_one(
                    mirror_repository=mirror_repository,
                    index_repository=index_repository,
                    folder=plan.folder,
                    storage_key=key,
                )
                done += 1
                if progress is not None:
                    progress(plan.folder.folder_name, done, total)
            for _key, message_ref in plan.gone:
                index_repository.delete_message(message_ref=message_ref)
                done += 1
                if progress is not None:
                    progress(plan.folder.folder_name, done, total)

    return planned
