"""Project mirror-stored messages into the SQLite index."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .domain import FolderRef, MessageRef
from .message_projection import project_rfc822_message
from .protocols import IndexRepository, MirrorRepository

RescanProgress = Callable[[str, int, int], None]
"""``(message, current, total)`` callback.  ``total == 0`` means unknown."""


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
    message_ref = MessageRef(
        account_name=folder.account_name,
        folder_name=folder.folder_name,
        id=0,
    )
    projected = project_rfc822_message(
        message_ref=message_ref,
        raw_message=raw_message,
        storage_key=storage_key,
    )
    index_repository.insert_message(message=projected)


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
    current_mtime_ns: int


# Map of ``folder_name -> mtime_ns`` recorded the last time a folder
# was fully scanned.  Opaque to the rescan engine beyond the equality
# check — callers persist and load it between runs.
ScanState = dict[str, int]


def rescan_local_account(
    *,
    mirror_repository: MirrorRepository,
    index_repository: IndexRepository,
    account_name: str,
    scan_state: ScanState | None = None,
    on_folder_scan: Callable[[str], None] | None = None,
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

    When ``scan_state`` is provided, folders whose mtime has not advanced
    since the last successful scan are skipped entirely — avoiding the
    expensive per-folder listing for cold archives.  The dict is mutated
    in place: entries for skipped folders stay untouched; entries for
    scanned folders are updated to the current mtime.  Callers pass an
    empty dict on first run and persist it between runs for the fast
    path to kick in.

    Callbacks:

    - ``on_folder_scan(name)`` fires once per folder during the plan phase
      *before* its disk listing starts — use it to show liveness while
      big mbox files are being walked.  Not fired for folders skipped by
      the mtime check.
    - ``on_plan(result)`` fires once after the plan is built, but only if
      the delta is non-empty, so callers can announce the planned work.
    - ``progress(folder, current, total)`` fires per item during the
      execute phase with account-wide totals.
    """
    plans: list[_FolderPlan] = []
    for folder in mirror_repository.list_folders(account_name=account_name):
        current_mtime = mirror_repository.folder_mtime_ns(folder=folder)
        if (
            scan_state is not None
            and current_mtime > 0
            and scan_state.get(folder.folder_name) == current_mtime
        ):
            # Folder untouched since last full scan — no re-listing
            # needed.  Empty plan so the folder is simply not touched
            # this pass; its scan-state entry stays as-is.
            continue
        if on_folder_scan is not None:
            on_folder_scan(folder.folder_name)
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
        plans.append(_FolderPlan(
            folder=folder, new_keys=new_keys, gone=gone,
            current_mtime_ns=current_mtime,
        ))

    planned = RescanResult(
        added=sum(len(p.new_keys) for p in plans),
        removed=sum(len(p.gone) for p in plans),
    )

    # Even when no changes were found, folders we *did* walk need their
    # mtime stamped so the next run can skip them via the fast path.
    if scan_state is not None:
        for plan in plans:
            if plan.current_mtime_ns > 0:
                scan_state[plan.folder.folder_name] = plan.current_mtime_ns

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
