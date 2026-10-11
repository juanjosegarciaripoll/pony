"""Project mirror-stored messages into the SQLite index."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass

from chronos.outcome import ProgressGate, progress_stride

from .domain import FolderRef, FolderSyncState, IndexedMessage, MessageRef
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
    uid: int | None = None,
) -> None:
    raw_message = mirror_repository.get_message_bytes(
        folder=folder,
        storage_key=storage_key,
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
    if uid is not None:
        projected = dataclasses.replace(projected, uid=uid)
    index_repository.insert_message(message=projected)


@dataclass(frozen=True, slots=True)
class RescanResult:
    """Summary returned by :func:`rescan_local_account`.

    ``added`` and ``removed`` count index rows reconciled against disk.
    ``reprojected_scanned`` counts overlap rows whose projection was
    recomputed when ``reproject_existing=True``; ``reprojected`` counts
    those whose projection actually differed and were upserted.  Both
    stay at 0 in the default delta-only mode.
    """

    added: int
    removed: int
    reprojected_scanned: int = 0
    reprojected: int = 0


@dataclass(frozen=True, slots=True)
class _FolderPlan:
    folder: FolderRef
    new_keys: tuple[str, ...]
    gone: tuple[tuple[str, MessageRef], ...]
    existing: tuple[IndexedMessage, ...]
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
    reproject_existing: bool = False,
    force_reproject: bool = False,
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

    When ``reproject_existing`` is true, rows whose ``storage_key`` is
    present in both the index and the mirror are also re-projected from
    their on-disk bytes; the merged row (preserving sync state — uid,
    flags, status) is upserted when its projection fields differ from
    the stored row, or unconditionally when ``force_reproject`` is also
    true.  This makes ``rescan_local_account`` usable as the engine for
    ``pony rescan`` after a projection-logic change.

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
        if reproject_existing:
            stored_messages = index_repository.list_folder_messages(folder=folder)
            by_key: dict[str, IndexedMessage] = {
                m.storage_key: m for m in stored_messages if m.storage_key
            }
            indexed_keys = set(by_key)
            new_keys = tuple(sorted(disk_keys - indexed_keys))
            gone = tuple(
                (k, by_key[k].message_ref) for k in sorted(indexed_keys - disk_keys)
            )
            existing = tuple(by_key[k] for k in sorted(disk_keys & indexed_keys))
        else:
            indexed = index_repository.list_folder_storage_keys(folder=folder)
            new_keys = tuple(sorted(disk_keys - set(indexed)))
            gone = tuple(
                (key, indexed[key]) for key in sorted(set(indexed) - disk_keys)
            )
            existing = ()
        plans.append(
            _FolderPlan(
                folder=folder,
                new_keys=new_keys,
                gone=gone,
                existing=existing,
                current_mtime_ns=current_mtime,
            )
        )

    planned = RescanResult(
        added=sum(len(p.new_keys) for p in plans),
        removed=sum(len(p.gone) for p in plans),
        reprojected_scanned=sum(len(p.existing) for p in plans),
    )

    # Even when no changes were found, folders we *did* walk need their
    # mtime stamped so the next run can skip them via the fast path.
    if scan_state is not None:
        for plan in plans:
            if plan.current_mtime_ns > 0:
                scan_state[plan.folder.folder_name] = plan.current_mtime_ns

    if planned.added == 0 and planned.removed == 0 and planned.reprojected_scanned == 0:
        return planned

    if on_plan is not None:
        on_plan(planned)

    total = planned.added + planned.removed + planned.reprojected_scanned
    done = 0
    reprojected = 0
    gate = ProgressGate(stride=progress_stride(total))
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
                if progress is not None and gate.due(done, final=done == total):
                    progress(plan.folder.folder_name, done, total)
            for _key, message_ref in plan.gone:
                index_repository.delete_message(message_ref=message_ref)
                done += 1
                if progress is not None and gate.due(done, final=done == total):
                    progress(plan.folder.folder_name, done, total)
            for stored in plan.existing:
                raw = mirror_repository.get_message_bytes(
                    folder=plan.folder,
                    storage_key=stored.storage_key,
                )
                fresh = project_rfc822_message(
                    message_ref=stored.message_ref,
                    raw_message=raw,
                    storage_key=stored.storage_key,
                )
                if force_reproject or not _projection_matches(stored, fresh):
                    merged = dataclasses.replace(
                        stored,
                        sender=fresh.sender,
                        recipients=fresh.recipients,
                        cc=fresh.cc,
                        subject=fresh.subject,
                        body_preview=fresh.body_preview,
                        has_attachments=fresh.has_attachments,
                        received_at=fresh.received_at,
                    )
                    index_repository.update_message(message=merged)
                    reprojected += 1
                done += 1
                if progress is not None and gate.due(done, final=done == total):
                    progress(plan.folder.folder_name, done, total)

    return dataclasses.replace(planned, reprojected=reprojected)


@dataclass(frozen=True, slots=True)
class ReindexResult:
    """What a rebuild from the mirror recovered."""

    folders: int
    added: int
    with_uid: int
    local_only: int
    states: int


def reindex_account_from_mirror(
    *,
    mirror_repository: MirrorRepository,
    index_repository: IndexRepository,
    account_name: str,
    progress: RescanProgress | None = None,
) -> ReindexResult:
    """Rebuild an account's index rows from what the mirror already holds.

    The mirror is self-describing: a Maildir filename carries the server
    handle it was fetched with (``,V=<uidvalidity>,U=<uid>`` — see
    :func:`pony.storage.uid_of`), so an index can be rebuilt without
    asking the server for a single byte.  That is the point of putting
    the handle in the name: losing the index costs a local pass over the
    files, not a re-download of the mailbox.

    Rows already present are left alone, so this is safe to repeat.  A
    file with no handle in its name is indexed with ``uid = NULL`` —
    which is the correct state for a message that exists only locally
    and that the next sync should upload.

    Where a folder's files agree on a UIDVALIDITY, that epoch and the
    highest UID in it are written back as the folder's sync watermark,
    so the next sync reconciles rather than refetches.  Files from an
    older epoch are indexed without a handle: their UIDs mean nothing
    on the current one.

    Nothing here touches the network.
    """
    from .storage import uid_of

    folders = mirror_repository.list_folders(account_name=account_name)
    added = with_uid = local_only = states = 0
    with index_repository.connection():
        for folder in folders:
            keys = list(mirror_repository.list_messages(folder=folder))
            gate = ProgressGate(stride=progress_stride(len(keys)))
            # The lean projection: this needs the key set and nothing
            # else, and `list_folder_messages` would pull 23 columns
            # (body preview included) and parse flags and three
            # datetimes for every row in the folder to get it.
            known = set(index_repository.list_folder_storage_keys(folder=folder))
            epoch = max(
                (handle[0] for key in keys if (handle := uid_of(key)) is not None),
                default=0,
            )
            highest = 0
            total = len(keys)
            for done, key in enumerate(keys, start=1):
                handle = uid_of(key)
                # A handle from a superseded epoch is not a handle.
                uid = handle[1] if handle is not None and handle[0] == epoch else None
                if uid is not None:
                    highest = max(highest, uid)
                if key not in known:
                    _ingest_one(
                        mirror_repository=mirror_repository,
                        index_repository=index_repository,
                        folder=folder,
                        storage_key=key,
                        uid=uid,
                    )
                    added += 1
                    if uid is None:
                        local_only += 1
                    else:
                        with_uid += 1
                if progress is not None and gate.due(done, final=done == total):
                    progress(folder.folder_name, done, total)
            if epoch:
                index_repository.record_folder_sync_state(
                    state=FolderSyncState(
                        account_name=account_name,
                        folder_name=folder.folder_name,
                        uid_validity=epoch,
                        highest_uid=highest,
                        # UIDNEXT is the server's to report; leaving it
                        # unknown keeps the next sync on the full
                        # reconcile path rather than its fast path.
                        uidnext=0,
                    ),
                )
                states += 1
    return ReindexResult(
        folders=len(folders),
        added=added,
        with_uid=with_uid,
        local_only=local_only,
        states=states,
    )


def _projection_matches(stored: IndexedMessage, fresh: IndexedMessage) -> bool:
    return (
        stored.sender == fresh.sender
        and stored.recipients == fresh.recipients
        and stored.cc == fresh.cc
        and stored.subject == fresh.subject
        and stored.body_preview == fresh.body_preview
        and stored.has_attachments == fresh.has_attachments
        and stored.received_at == fresh.received_at
    )
