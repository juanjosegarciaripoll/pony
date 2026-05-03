"""IMAP synchronization engine.

Per-row identity, per-folder UID-set diff.  See ``ai/SYNCHRONIZATION.md``.

Sync is a two-pass process:

1. **Plan** (``ImapSyncService.plan``) — connect, run cheap STATUS or
   full FETCH metadata, compute a :class:`SyncPlan` of typed ops.  No
   writes to the local mirror or index (except clearing stale UIDs on
   UIDVALIDITY reset).

2. **Execute** (``ImapSyncService.execute``) — apply each op,
   updating the local mirror, index, and per-folder watermarks.

``ImapSyncService.sync`` runs both in one shot.

Identity model: every row has an autoincrement ``id`` (``MessageRef.id``).
The ``Message-ID:`` header is display-only — multiple rows in one folder
may share it.  Sync compares the local UID set against the server's UID
set per folder and never tracks Message-IDs across folders.
"""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from queue import SimpleQueue

from .domain import (
    AccountConfig,
    AppConfig,
    FlagSet,
    FolderConfig,
    FolderQuickStatus,
    FolderRef,
    FolderSyncState,
    MessageFlag,
    MessageRef,
    MessageStatus,
    SlowPathRow,
)
from .message_projection import project_rfc822_message
from .protocols import (
    CredentialsProvider,
    ImapClientSession,
    ImapSessionFactory,
    IndexRepository,
    MirrorRepository,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProgressInfo:
    """Structured progress update emitted during sync."""

    message: str
    current: int = 0
    total: int = 0


ProgressCallback = collections.abc.Callable[[ProgressInfo], None]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FolderSyncResult:
    """Summary of what changed during one folder sync pass."""

    folder_name: str
    fetched: int = 0
    flag_updates_from_server: int = 0
    flag_pushes_to_server: int = 0
    flag_conflicts_merged: int = 0
    deleted_on_server: int = 0
    moved_to_server: int = 0
    appended_to_server: int = 0
    scan_ms: int = 0
    fetch_ms: int = 0
    ingest_ms: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(
            self.fetched
            or self.flag_updates_from_server
            or self.flag_pushes_to_server
            or self.flag_conflicts_merged
            or self.deleted_on_server
            or self.moved_to_server
            or self.appended_to_server
        )


@dataclass(frozen=True, slots=True)
class AccountSyncResult:
    account_name: str
    folders: tuple[FolderSyncResult, ...]
    skipped_folders: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncResult:
    accounts: tuple[AccountSyncResult, ...]


# ---------------------------------------------------------------------------
# Op types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchNewOp:
    """Download a new message from the server and insert a fresh row."""

    uid: int
    message_id: str
    server_flags: frozenset[MessageFlag]
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ServerDeleteOp:
    """A UID disappeared from the server's folder — mark the row trashed."""

    uid: int
    message_ref: MessageRef


@dataclass(frozen=True, slots=True)
class PullFlagsOp:
    """Server flags changed; pull them onto the local row."""

    uid: int
    message_ref: MessageRef
    new_flags: frozenset[MessageFlag]
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PushFlagsOp:
    """Local flags changed; push to the server."""

    uid: int
    message_ref: MessageRef
    new_flags: frozenset[MessageFlag]
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class MergeFlagsOp:
    """Both sides changed flags — three-way merge."""

    uid: int
    message_ref: MessageRef
    merged_flags: frozenset[MessageFlag]
    push_to_server: bool
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PushDeleteOp:
    """Locally-trashed row with a server UID — expunge and drop."""

    server_uid: int
    message_ref: MessageRef
    storage_key: str


@dataclass(frozen=True, slots=True)
class PushMoveOp:
    """Server-side move recorded by a PENDING_MOVE row.

    The row currently lives in ``message_ref.folder_name``
    (``= target_folder``) with ``uid IS NULL``; the server still has
    the message at ``(source_folder, source_uid)``.
    """

    message_ref: MessageRef
    source_folder: str
    source_uid: int
    target_folder: str


@dataclass(frozen=True, slots=True)
class PushAppendOp:
    """Upload a local-only row via IMAP APPEND."""

    message_ref: MessageRef


@dataclass(frozen=True, slots=True)
class PurgeLocalOp:
    """Drop a local-only row that has no server presence (compose draft)."""

    message_ref: MessageRef
    storage_key: str


@dataclass(frozen=True, slots=True)
class ReUploadOp:
    """C-1: server lost the message but the local row was modified — APPEND."""

    message_ref: MessageRef
    local_flags: frozenset[MessageFlag]
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RestoreOp:
    """C-2: server still has a row we trashed locally — undo the trash."""

    message_ref: MessageRef


type SyncOp = (
    FetchNewOp
    | ServerDeleteOp
    | PullFlagsOp
    | PushFlagsOp
    | MergeFlagsOp
    | PushDeleteOp
    | PushMoveOp
    | PushAppendOp
    | PurgeLocalOp
    | ReUploadOp
    | RestoreOp
)


# ---------------------------------------------------------------------------
# Plan types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FolderSyncPlan:
    """All planned operations for one folder."""

    folder_name: str
    uid_validity: int
    highest_uid: int
    ops: tuple[SyncOp, ...]
    needs_confirmation: bool = False
    is_new: bool = False
    scan_ms: int = 0
    highest_modseq: int = 0
    uidnext: int = 0


@dataclass(frozen=True, slots=True)
class AccountSyncPlan:
    account_name: str
    folders: tuple[FolderSyncPlan, ...]
    skipped_folders: tuple[str, ...] = ()
    creates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncPlan:
    accounts: tuple[AccountSyncPlan, ...]

    def is_empty(self) -> bool:
        for a in self.accounts:
            if a.creates:
                return False
            if any(f.ops for f in a.folders):
                return False
        return True

    def count_ops(self, op_type: type) -> int:
        return sum(
            1
            for a in self.accounts
            for f in a.folders
            for op in f.ops
            if isinstance(op, op_type)
        )


# ---------------------------------------------------------------------------
# Plan formatting
# ---------------------------------------------------------------------------

_OP_LABELS: tuple[tuple[str, str], ...] = (
    ("download", "{n} new message(s) to download"),
    ("server_delete", "{n} deleted on server (move to trash)"),
    ("push_move", "{n} moved locally (push to server)"),
    ("push_append", "{n} new local message(s) to upload"),
    ("push_delete", "{n} local deletion(s) to expunge"),
    ("pull_flags", "{n} flag update(s) from server"),
    ("push_flags", "{n} flag update(s) to push"),
    ("merge_flags", "{n} flag conflict(s) to merge"),
    ("restore", "{n} locally-trashed message(s) to restore"),
    ("reupload", "{n} message(s) to re-upload"),
    ("purge", "{n} local-only row(s) to drop"),
)


def _categorize_ops(ops: tuple[SyncOp, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for op in ops:
        if isinstance(op, FetchNewOp):
            key = "download"
        elif isinstance(op, ServerDeleteOp):
            key = "server_delete"
        elif isinstance(op, PushMoveOp):
            key = "push_move"
        elif isinstance(op, PushAppendOp):
            key = "push_append"
        elif isinstance(op, PushDeleteOp):
            key = "push_delete"
        elif isinstance(op, PullFlagsOp):
            key = "pull_flags"
        elif isinstance(op, PushFlagsOp):
            key = "push_flags"
        elif isinstance(op, MergeFlagsOp):
            key = "merge_flags"
        elif isinstance(op, RestoreOp):
            key = "restore"
        elif isinstance(op, ReUploadOp):
            key = "reupload"
        elif isinstance(op, PurgeLocalOp):
            key = "purge"
        else:
            key = "other"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _format_op_counts(counts: dict[str, int]) -> list[str]:
    parts = [
        template.format(n=counts[key])
        for key, template in _OP_LABELS
        if counts.get(key)
    ]
    if counts.get("other"):
        parts.append(f"{counts['other']} other operation(s)")
    return parts


def format_plan_summary(plan: SyncPlan) -> str:
    """One-line summary of total operations across all folders."""
    totals: dict[str, int] = {}
    for acct in plan.accounts:
        for folder in acct.folders:
            for key, n in _categorize_ops(folder.ops).items():
                totals[key] = totals.get(key, 0) + n
    return ", ".join(_format_op_counts(totals))


def format_plan_detail(plan: SyncPlan) -> str:
    """Multi-line detail: per-account, per-folder human-readable counts."""
    lines: list[str] = []
    for acct in plan.accounts:
        header = f"  {acct.account_name}"
        if acct.skipped_folders:
            header += f"  (skipped: {', '.join(acct.skipped_folders)})"
        lines.append(header)
        if acct.creates:
            lines.append(f"    create folder(s): {', '.join(acct.creates)}")
        for folder in acct.folders:
            counts = _categorize_ops(folder.ops)
            parts = _format_op_counts(counts)
            confirm = " [needs confirmation]" if folder.needs_confirmation else ""
            if parts:
                lines.append(f"    {folder.folder_name}: " + ", ".join(parts) + confirm)
            elif folder.is_new:
                lines.append(f"    {folder.folder_name}: new folder")
    return "\n".join(lines) if lines else "  (nothing to do)"


# ---------------------------------------------------------------------------
# Three-way flag merge
# ---------------------------------------------------------------------------


def _merge_flags(
    *,
    local: frozenset[MessageFlag],
    base: frozenset[MessageFlag],  # noqa: ARG001  # reserved for true 3-way
    remote: frozenset[MessageFlag],
) -> frozenset[MessageFlag]:
    """Union local and remote, stripping ``\\Deleted`` (handled separately)."""
    deleted = frozenset({MessageFlag.DELETED})
    return (local - deleted) | (remote - deleted)


# C-6 mass-deletion safety threshold: if more than this fraction of
# locally-known UIDs disappear server-side in one sync, the folder
# requires explicit confirmation before the deletes are applied.
_MASS_DELETE_THRESHOLD = 0.20
_MASS_DELETE_MIN = 5


# ---------------------------------------------------------------------------
# Sync engine
# ---------------------------------------------------------------------------


class ImapSyncService:
    """Reconcile local mirror and index against an IMAP server."""

    def __init__(
        self,
        *,
        config: AppConfig,
        mirror_factory: collections.abc.Callable[[AccountConfig], MirrorRepository],
        index: IndexRepository,
        credentials: CredentialsProvider,
        session_factory: ImapSessionFactory | None = None,
    ) -> None:
        self._config = config
        self._mirror_factory = mirror_factory
        self._index = index
        self._credentials = credentials
        if session_factory is None:
            from .imap_client import ImapSession

            def _default_factory(
                account: AccountConfig, password: str
            ) -> ImapClientSession:
                return ImapSession(
                    host=account.imap_host,
                    port=account.imap_port,
                    ssl=account.imap_ssl,
                    username=account.username,
                    password=password,
                )

            self._session_factory: ImapSessionFactory = _default_factory
        else:
            self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(
        self,
        *,
        account_name: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> SyncPlan:
        accounts = self._select_accounts(account_name)
        account_plans: list[AccountSyncPlan] = []
        errors: list[str] = []
        for account in accounts:
            if progress is not None:
                progress(ProgressInfo(f"Connecting to {account.name}…"))
            try:
                account_plans.append(
                    self._plan_account(account=account, progress=progress)
                )
            except Exception as exc:
                logger.exception("Planning failed for account %r", account.name)
                errors.append(f"{account.name}: {exc}")
        plan = SyncPlan(accounts=tuple(account_plans))
        if errors and plan.is_empty():
            raise RuntimeError("Sync planning failed:\n" + "\n".join(errors))
        return plan

    def execute(
        self,
        plan: SyncPlan,
        *,
        confirmed_folders: frozenset[str] = frozenset(),
        progress: ProgressCallback | None = None,
    ) -> SyncResult:
        self._run_cleanup()
        results: list[AccountSyncResult] = []
        for account_plan in plan.accounts:
            account = self._find_account(account_plan.account_name)
            if account is None:
                logger.warning(
                    "Account %r in plan not found — skipping",
                    account_plan.account_name,
                )
                continue
            try:
                results.append(
                    self._execute_account_plan(
                        account=account,
                        plan=account_plan,
                        confirmed_folders=confirmed_folders,
                        progress=progress,
                    )
                )
            except Exception:
                logger.exception(
                    "Execution failed for account %r", account_plan.account_name
                )
        return SyncResult(accounts=tuple(results))

    def sync(self, *, account_name: str | None = None) -> SyncResult:
        plan = self.plan(account_name=account_name)
        all_folders = frozenset(f.folder_name for a in plan.accounts for f in a.folders)
        return self.execute(plan, confirmed_folders=all_folders)

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def _plan_account(
        self,
        *,
        account: AccountConfig,
        progress: ProgressCallback | None = None,
    ) -> AccountSyncPlan:
        logger.info("Planning sync for account %r", account.name)
        password = self._credentials.get_password(account_name=account.name)
        session = self._session_factory(account, password)

        def _reconnect() -> ImapClientSession:
            nonlocal session
            logger.info("Reconnecting to %s", account.imap_host)
            with contextlib.suppress(Exception):
                session.logout()
            session = self._session_factory(account, password)
            return session

        try:
            return self._plan_folders(
                account=account,
                session=session,
                progress=progress,
                reconnect=_reconnect,
            )
        finally:
            with contextlib.suppress(Exception):
                session.logout()

    def _plan_folders(
        self,
        *,
        account: AccountConfig,
        session: ImapClientSession,
        progress: ProgressCallback | None = None,
        reconnect: collections.abc.Callable[[], ImapClientSession] | None = None,
    ) -> AccountSyncPlan:
        folder_policy = account.folders
        all_server_folders = list(session.list_folders())
        server_folders = [f for f in all_server_folders if folder_policy.should_sync(f)]

        # Folders that exist in the local mirror but not on the server
        # get a CREATE op at the head of execution so any PushAppendOp
        # / PushMoveOp targeting them has a live destination.
        mirror = self._mirror_factory(account)
        try:
            mirror_folder_names = {
                f.folder_name for f in mirror.list_folders(account_name=account.name)
            }
        except Exception:
            logger.exception(
                "Failed to list mirror folders for %r",
                account.name,
            )
            mirror_folder_names = set()
        server_folder_set = set(all_server_folders)
        creates = tuple(
            sorted(
                name
                for name in mirror_folder_names - server_folder_set
                if folder_policy.should_sync(name)
            )
        )

        with self._index.connection():
            return self._plan_folders_inner(
                account=account,
                session=session,
                folder_policy=folder_policy,
                all_server_folders=all_server_folders,
                server_folders=server_folders,
                creates=creates,
                progress=progress,
                reconnect=reconnect,
            )

    def _plan_folders_inner(
        self,
        *,
        account: AccountConfig,
        session: ImapClientSession,
        folder_policy: FolderConfig,
        all_server_folders: collections.abc.Sequence[str],
        server_folders: list[str],
        creates: tuple[str, ...],
        progress: ProgressCallback | None,
        reconnect: collections.abc.Callable[[], ImapClientSession] | None,
    ) -> AccountSyncPlan:
        # Warn about Gmail aggregate folders — duplicate Message-ID
        # rows now live as separate index rows, but the user still
        # usually wants these excluded.
        gmail_aggregate = {"[Gmail]/All Mail", "[Gmail]/Important"}
        synced_aggregate = gmail_aggregate & set(server_folders)
        if synced_aggregate:
            logger.warning(
                "Account %r syncs Gmail aggregate folder(s) %s — consider "
                "excluding them via folders.exclude",
                account.name,
                ", ".join(sorted(synced_aggregate)),
            )

        stale = self._index.purge_stale_folders(
            account_name=account.name,
            active_folders=frozenset(all_server_folders),
        )
        if stale:
            logger.info(
                "Cleanup: purged state for %d stale folder(s) in %r: %s",
                len(stale),
                account.name,
                ", ".join(stale),
            )

        # Per-folder STATUS gate.  Outputs:
        #   - fast_path[folder] = stored FolderSyncState  (no FETCH at all)
        #   - medium_path[folder] = (stored, changed_flags)  (CHANGEDSINCE)
        #   - slow_path[folder] = (uid_to_mid, uid_to_flags)  (full scan)
        # plus per-folder observed UIDNEXT/HIGHESTMODSEQ for the watermark.
        fast_path: dict[str, FolderSyncState] = {}
        medium_path: dict[str, tuple[FolderSyncState, dict[int, FlagSet]]] = {}
        slow_path: dict[str, tuple[dict[int, str], dict[int, FlagSet]]] = {}
        observed_uidnext: dict[str, int] = {}
        observed_modseq: dict[str, int] = {}
        scan_ms: dict[str, int] = {}
        skipped: list[str] = []

        for i, folder_name in enumerate(server_folders, 1):
            if progress is not None:
                progress(
                    ProgressInfo(
                        f"{account.name}: scanning {folder_name}"
                        f" ({i}/{len(server_folders)})",
                        current=i,
                        total=len(server_folders),
                    )
                )

            stored = self._index.get_folder_sync_state(
                account_name=account.name,
                folder_name=folder_name,
            )
            quick = self._safe_status(
                session,
                folder_name,
                reconnect=reconnect,
                scan_ms=scan_ms,
            )
            if quick is None:
                skipped.append(folder_name)
                continue

            observed_uidnext[folder_name] = quick.uidnext
            if quick.highest_modseq is not None:
                observed_modseq[folder_name] = quick.highest_modseq

            uid_set_stable = (
                stored is not None
                and quick.uid_validity == stored.uid_validity
                and stored.uidnext > 0
                and quick.uidnext == stored.uidnext
                and quick.messages
                == self._index.count_uids_for_folder(
                    account_name=account.name,
                    folder_name=folder_name,
                )
            )

            if uid_set_stable and stored is not None:
                modseq_matches = (
                    quick.highest_modseq is None
                    or stored.highest_modseq == 0
                    or quick.highest_modseq == stored.highest_modseq
                )
                if modseq_matches:
                    fast_path[folder_name] = stored
                    logger.debug(
                        "Fast-path %s/%s: %d msg(s)",
                        account.name,
                        folder_name,
                        quick.messages,
                    )
                    continue

                # Medium path: UID set stable, flags drifted server-side.
                try:
                    _t = time.perf_counter()
                    changed = session.fetch_flags_changed_since(
                        folder_name,
                        stored.highest_modseq,
                    )
                    scan_ms[folder_name] = scan_ms.get(folder_name, 0) + int(
                        (time.perf_counter() - _t) * 1000
                    )
                except (OSError, EOFError) as exc:
                    logger.info(
                        "Medium-path CHANGEDSINCE failed on %s/%s: %s"
                        " — falling through to slow path",
                        account.name,
                        folder_name,
                        exc,
                    )
                else:
                    medium_path[folder_name] = (stored, changed)
                    continue

            # Slow path: full FETCH of UIDs, mid, flags.
            try:
                _t = time.perf_counter()
                uid_metadata = session.fetch_uid_to_message_id(folder_name)
                scan_ms[folder_name] = int((time.perf_counter() - _t) * 1000)
            except (OSError, EOFError) as exc:
                if reconnect is not None:
                    logger.info(
                        "Connection lost scanning %s/%s: %s — reconnecting",
                        account.name,
                        folder_name,
                        exc,
                    )
                    try:
                        session = reconnect()
                        _t = time.perf_counter()
                        uid_metadata = session.fetch_uid_to_message_id(
                            folder_name,
                        )
                        scan_ms[folder_name] = int((time.perf_counter() - _t) * 1000)
                    except (OSError, EOFError):
                        skipped.append(folder_name)
                        continue
                else:
                    skipped.append(folder_name)
                    continue

            uid_to_mid = {uid: v[0] for uid, v in uid_metadata.items()}
            uid_to_flags = {uid: v[1] for uid, v in uid_metadata.items()}
            slow_path[folder_name] = (uid_to_mid, uid_to_flags)

        # Build a cross-folder UID view from the per-folder STATUS /
        # FETCH outputs.  Fast and medium paths confirmed the server's
        # UID set matches the local set; slow paths have an explicit
        # uid_to_mid map.  PENDING_MOVE rows whose ``source_uid`` is
        # absent from the source folder's view fall back to
        # ``PushAppendOp`` — the move was interrupted (or the source
        # was purged by another client) so APPEND is the only way to
        # land the local copy on the server.
        remote_uids_by_folder: dict[str, set[int]] = {}
        for fn in fast_path:
            remote_uids_by_folder[fn] = self._index.list_folder_uids(
                account_name=account.name,
                folder_name=fn,
            )
        for fn in medium_path:
            remote_uids_by_folder[fn] = self._index.list_folder_uids(
                account_name=account.name,
                folder_name=fn,
            )
        for fn, (uid_to_mid, _) in slow_path.items():
            remote_uids_by_folder[fn] = set(uid_to_mid.keys())

        # Plan each folder.
        folder_plans: list[FolderSyncPlan] = []
        for folder_name in server_folders:
            if folder_name in skipped:
                continue
            try:
                if folder_name in fast_path:
                    plan = self._plan_fast_path(
                        account=account,
                        folder_name=folder_name,
                        stored=fast_path[folder_name],
                        read_only=folder_policy.is_read_only(folder_name),
                        remote_uids_by_folder=remote_uids_by_folder,
                    )
                elif folder_name in medium_path:
                    stored, changed = medium_path[folder_name]
                    plan = self._plan_medium_path(
                        account=account,
                        folder_name=folder_name,
                        stored=stored,
                        changed_flags=changed,
                        read_only=folder_policy.is_read_only(folder_name),
                        remote_uids_by_folder=remote_uids_by_folder,
                    )
                else:
                    uid_to_mid, uid_to_flags = slow_path[folder_name]
                    plan = self._plan_slow_path(
                        account=account,
                        folder_name=folder_name,
                        session=session,
                        uid_to_mid=uid_to_mid,
                        uid_to_flags=uid_to_flags,
                        read_only=folder_policy.is_read_only(folder_name),
                        remote_uids_by_folder=remote_uids_by_folder,
                    )
                plan = dataclasses.replace(
                    plan,
                    scan_ms=scan_ms.get(folder_name, 0),
                    uidnext=observed_uidnext.get(folder_name, 0),
                    highest_modseq=observed_modseq.get(folder_name, 0),
                )
            except OSError as exc:
                logger.warning(
                    "Failed to plan folder %s/%s: %s",
                    account.name,
                    folder_name,
                    exc,
                )
                skipped.append(folder_name)
                continue
            folder_plans.append(plan)

        # New mirror-only folders: no scan possible yet, but their
        # PENDING_MOVE / pending-append rows still need PushMoveOp /
        # PushAppendOp emitted so they ride out on this sync after
        # the CREATE.
        for folder_name in creates:
            folder_plans.append(
                self._plan_new_folder(
                    account=account,
                    folder_name=folder_name,
                    remote_uids_by_folder=remote_uids_by_folder,
                )
            )

        return AccountSyncPlan(
            account_name=account.name,
            folders=tuple(folder_plans),
            skipped_folders=tuple(skipped),
            creates=creates,
        )

    def _safe_status(
        self,
        session: ImapClientSession,
        folder_name: str,
        *,
        reconnect: collections.abc.Callable[[], ImapClientSession] | None,
        scan_ms: dict[str, int],
    ) -> FolderQuickStatus | None:
        """Run STATUS, retrying once via *reconnect* on connection loss."""
        try:
            _t = time.perf_counter()
            quick = session.folder_quick_status(folder_name)
            scan_ms[folder_name] = int((time.perf_counter() - _t) * 1000)
            return quick
        except (OSError, EOFError) as exc:
            if reconnect is None:
                logger.debug("STATUS skipped on %s: %s", folder_name, exc)
                return None
            try:
                session = reconnect()
                _t = time.perf_counter()
                quick = session.folder_quick_status(folder_name)
                scan_ms[folder_name] = int((time.perf_counter() - _t) * 1000)
                return quick
            except (OSError, EOFError):
                return None

    # ------------------------------------------------------------------
    # Per-folder planners
    # ------------------------------------------------------------------

    def _plan_fast_path(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        stored: FolderSyncState,
        read_only: bool,
        remote_uids_by_folder: dict[str, set[int]],
    ) -> FolderSyncPlan:
        """Push-only plan: no FETCH, no fresh UID/flag info from server."""
        ops = self._pending_push_ops(
            account=account,
            folder_name=folder_name,
            read_only=read_only,
            remote_uids_by_folder=remote_uids_by_folder,
        )
        return FolderSyncPlan(
            folder_name=folder_name,
            uid_validity=stored.uid_validity,
            highest_uid=stored.highest_uid,
            ops=tuple(ops),
        )

    def _plan_medium_path(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        stored: FolderSyncState,
        changed_flags: dict[int, FlagSet],
        read_only: bool,
        remote_uids_by_folder: dict[str, set[int]],
    ) -> FolderSyncPlan:
        """UID set stable; reconcile only flags whose modseq advanced."""
        ops = self._pending_push_ops(
            account=account,
            folder_name=folder_name,
            read_only=read_only,
            remote_uids_by_folder=remote_uids_by_folder,
        )
        if changed_flags:
            base_by_uid = self._index.list_folder_base_flags(
                account_name=account.name,
                folder_name=folder_name,
            )
            uid_to_row = {
                row.uid: row
                for row in self._index.list_folder_slow_path_rows(
                    account_name=account.name,
                    folder_name=folder_name,
                )
                if row.uid is not None
            }
            for uid, (remote_flags, extra) in changed_flags.items():
                row = uid_to_row.get(uid)
                if row is None:
                    continue
                base, _ = base_by_uid.get(uid, (frozenset(), frozenset()))
                self._reconcile_flags(
                    ops=ops,
                    row=row,
                    uid=uid,
                    remote=remote_flags,
                    base=base,
                    extra=extra,
                    read_only=read_only,
                )
        return FolderSyncPlan(
            folder_name=folder_name,
            uid_validity=stored.uid_validity,
            highest_uid=stored.highest_uid,
            ops=tuple(ops),
        )

    def _plan_slow_path(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        session: ImapClientSession,
        uid_to_mid: dict[int, str],
        uid_to_flags: dict[int, FlagSet],
        read_only: bool,
        remote_uids_by_folder: dict[str, set[int]],
    ) -> FolderSyncPlan:
        """Full per-folder UID-set diff (§6 of SYNCHRONIZATION.md)."""
        uid_validity = session.get_uid_validity(folder_name)
        if uid_validity <= 0:
            logger.warning(
                "Server reported UIDVALIDITY %d for %s/%s — UID stability"
                " is not guaranteed",
                uid_validity,
                account.name,
                folder_name,
            )

        stored = self._index.get_folder_sync_state(
            account_name=account.name,
            folder_name=folder_name,
        )
        if stored is not None and stored.uid_validity != uid_validity:
            # C-4: UIDVALIDITY reset.
            logger.warning(
                "UIDVALIDITY changed for %s/%s (was %d, now %d) — full resync",
                account.name,
                folder_name,
                stored.uid_validity,
                uid_validity,
            )
            self._index.clear_uids_for_folder(
                account_name=account.name,
                folder_name=folder_name,
            )
            stored = None

        rows = self._index.list_folder_slow_path_rows(
            account_name=account.name,
            folder_name=folder_name,
        )
        rows_by_uid: dict[int, SlowPathRow] = {
            row.uid: row for row in rows if row.uid is not None
        }

        remote_uids = set(uid_to_mid.keys())
        local_uids = set(rows_by_uid.keys())
        new_uids = remote_uids - local_uids
        gone_uids = local_uids - remote_uids
        common_uids = remote_uids & local_uids

        ops: list[SyncOp] = []

        # C-2 detection: a locally-TRASHED row whose server flags
        # changed since base means the user's delete intent collides
        # with a real server-side update.  Restore the row instead of
        # pushing the deletion (the user can re-trash on their next
        # pass).  Compute the row ids that get the C-2 treatment so
        # ``_pending_push_ops`` can skip emitting PushDeleteOp for them.
        c2_row_ids: set[int] = set()
        for uid in remote_uids & local_uids:
            row = rows_by_uid[uid]
            if row.local_status != MessageStatus.TRASHED:
                continue
            remote_flags, _ = uid_to_flags.get(
                uid,
                (frozenset(), frozenset()),
            )
            if remote_flags != row.base_flags and not read_only:
                c2_row_ids.add(row.message_ref.id)

        # Pending-push rows for this folder (uid IS NULL or PENDING_MOVE
        # or TRASHED or flag drift).  Emitted regardless of UID diff.
        ops.extend(
            self._pending_push_ops(
                account=account,
                folder_name=folder_name,
                read_only=read_only,
                suppress_delete_ids=c2_row_ids,
                remote_uids_by_folder=remote_uids_by_folder,
            )
        )

        # Step: new UIDs on the server.
        for uid in new_uids:
            mid = uid_to_mid.get(uid, "")
            ops.append(
                FetchNewOp(
                    uid=uid,
                    message_id=mid,
                    server_flags=uid_to_flags.get(
                        uid,
                        (frozenset(), frozenset()),
                    )[0],
                    extra_imap_flags=uid_to_flags.get(
                        uid,
                        (frozenset(), frozenset()),
                    )[1],
                )
            )

        # Step: UIDs gone server-side.
        for uid in gone_uids:
            row = rows_by_uid[uid]
            if row.local_status == MessageStatus.TRASHED:
                # We already wanted it gone; planner already emitted
                # the PushDelete via _pending_push_ops.  Just mark
                # local row state cleanly here.
                continue
            local_changed = row.local_flags != row.base_flags
            if local_changed and not read_only:
                # C-1: server lost it but we have local mutations —
                # re-upload the body.  After ReUploadOp, the row's
                # uid is cleared and the next sync picks up the new
                # UID via FetchNewOp/APPENDUID.
                ops.append(
                    ReUploadOp(
                        message_ref=row.message_ref,
                        local_flags=row.local_flags,
                        extra_imap_flags=row.extra_imap_flags,
                    )
                )
            else:
                ops.append(ServerDeleteOp(uid=uid, message_ref=row.message_ref))

        # Step: flag reconciliation for surviving UIDs.
        for uid in common_uids:
            row = rows_by_uid[uid]
            remote_flags, extra = uid_to_flags.get(
                uid,
                (frozenset(), frozenset()),
            )
            if row.local_status == MessageStatus.TRASHED:
                # C-2: server still has a row we trashed.  In a
                # read-only folder we always restore (no way to push
                # the delete); in a writable folder, restore + pull
                # only when the server's flags actually changed —
                # that's the signal that the deletion conflicts with
                # a real server-side update.  Otherwise the delete
                # already queued by ``_pending_push_ops`` runs.
                if read_only or row.message_ref.id in c2_row_ids:
                    ops.append(RestoreOp(message_ref=row.message_ref))
                    ops.append(
                        PullFlagsOp(
                            uid=uid,
                            message_ref=row.message_ref,
                            new_flags=remote_flags,
                            extra_imap_flags=extra,
                        )
                    )
                continue
            self._reconcile_flags(
                ops=ops,
                row=row,
                uid=uid,
                remote=remote_flags,
                base=row.base_flags,
                extra=extra,
                read_only=read_only,
            )

        # C-6: mass-deletion safety halt.
        delete_count = sum(1 for op in ops if isinstance(op, ServerDeleteOp))
        total_known = len(local_uids)
        needs_confirmation = (
            total_known >= _MASS_DELETE_MIN
            and delete_count > total_known * _MASS_DELETE_THRESHOLD
        )
        if needs_confirmation:
            logger.warning(
                "Mass deletion in %s/%s: %d of %d (%.0f%%) — needs confirmation",
                account.name,
                folder_name,
                delete_count,
                total_known,
                delete_count / total_known * 100,
            )

        return FolderSyncPlan(
            folder_name=folder_name,
            uid_validity=uid_validity,
            highest_uid=max(remote_uids) if remote_uids else 0,
            ops=tuple(ops),
            needs_confirmation=needs_confirmation,
        )

    def _plan_new_folder(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        remote_uids_by_folder: dict[str, set[int]],
    ) -> FolderSyncPlan:
        """Plan a folder that lives only locally (about to be CREATEd)."""
        ops = self._pending_push_ops(
            account=account,
            folder_name=folder_name,
            read_only=False,
            remote_uids_by_folder=remote_uids_by_folder,
        )
        return FolderSyncPlan(
            folder_name=folder_name,
            uid_validity=0,
            highest_uid=0,
            ops=tuple(ops),
            is_new=True,
        )

    def _pending_push_ops(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        read_only: bool,
        suppress_delete_ids: set[int] = frozenset(),  # type: ignore[assignment]
        remote_uids_by_folder: dict[str, set[int]] | None = None,
    ) -> list[SyncOp]:
        """Emit ops driven by local mutations recorded in the index."""
        ops: list[SyncOp] = []
        candidates = self._index.list_folder_push_candidates(
            account_name=account.name,
            folder_name=folder_name,
        )
        for row in candidates:
            if row.local_status == MessageStatus.TRASHED:
                if row.message_ref.id in suppress_delete_ids:
                    # C-2: server changed flags on a locally-trashed
                    # row.  The slow-path planner will emit RestoreOp
                    # and PullFlagsOp for this row in its flag
                    # reconciliation step.
                    continue
                if read_only:
                    ops.append(RestoreOp(message_ref=row.message_ref))
                elif row.uid is not None:
                    ops.append(
                        PushDeleteOp(
                            server_uid=row.uid,
                            message_ref=row.message_ref,
                            storage_key=row.storage_key,
                        )
                    )
                else:
                    # No server UID — just drop the local row.
                    ops.append(
                        PurgeLocalOp(
                            message_ref=row.message_ref,
                            storage_key=row.storage_key,
                        )
                    )
                continue

            if row.local_status == MessageStatus.PENDING_MOVE:
                if read_only or row.source_folder is None or row.source_uid is None:
                    # Cannot push the move: missing source handle or
                    # the destination is read-only.  Leave the row
                    # untouched until the user resolves it.
                    continue
                # Interrupted-move recovery: if the source folder was
                # scanned and ``source_uid`` is no longer there, the
                # message is gone server-side.  APPEND to the target
                # instead — the user's archive intent stands.
                source_view = (
                    remote_uids_by_folder.get(row.source_folder)
                    if remote_uids_by_folder is not None
                    else None
                )
                if source_view is not None and row.source_uid not in source_view:
                    ops.append(PushAppendOp(message_ref=row.message_ref))
                    continue
                ops.append(
                    PushMoveOp(
                        message_ref=row.message_ref,
                        source_folder=row.source_folder,
                        source_uid=row.source_uid,
                        target_folder=folder_name,
                    )
                )
                continue

            if row.uid is None:
                # ACTIVE compose draft — APPEND.
                if read_only:
                    continue
                ops.append(PushAppendOp(message_ref=row.message_ref))
                continue

            # ACTIVE row with flag drift.
            if read_only:
                continue
            ops.append(
                PushFlagsOp(
                    uid=row.uid,
                    message_ref=row.message_ref,
                    new_flags=row.local_flags,
                    extra_imap_flags=row.extra_imap_flags,
                )
            )
        return ops

    def _reconcile_flags(
        self,
        *,
        ops: list[SyncOp],
        row: SlowPathRow,
        uid: int,
        remote: frozenset[MessageFlag],
        base: frozenset[MessageFlag],
        extra: frozenset[str],
        read_only: bool,
    ) -> None:
        """Append the right flag-reconciliation op to *ops*."""
        server_changed = remote != base
        local_changed = row.local_flags != base
        if server_changed and not local_changed:
            ops.append(
                PullFlagsOp(
                    uid=uid,
                    message_ref=row.message_ref,
                    new_flags=remote,
                    extra_imap_flags=extra,
                )
            )
        elif local_changed and not server_changed and not read_only:
            ops.append(
                PushFlagsOp(
                    uid=uid,
                    message_ref=row.message_ref,
                    new_flags=row.local_flags,
                    extra_imap_flags=extra,
                )
            )
        elif server_changed and local_changed:
            merged = _merge_flags(
                local=row.local_flags,
                base=base,
                remote=remote,
            )
            ops.append(
                MergeFlagsOp(
                    uid=uid,
                    message_ref=row.message_ref,
                    merged_flags=merged,
                    push_to_server=not read_only,
                    extra_imap_flags=extra,
                )
            )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute_account_plan(
        self,
        *,
        account: AccountConfig,
        plan: AccountSyncPlan,
        confirmed_folders: frozenset[str],
        progress: ProgressCallback | None,
    ) -> AccountSyncResult:
        logger.info("Executing sync plan for account %r", account.name)
        password = self._credentials.get_password(account_name=account.name)
        session = self._session_factory(account, password)
        mirror = self._mirror_factory(account)

        folder_results: list[FolderSyncResult] = []
        skipped = list(plan.skipped_folders)
        try:
            for folder_name in plan.creates:
                try:
                    session.create_folder(folder_name)
                    logger.info(
                        "Created server folder %s/%s",
                        account.name,
                        folder_name,
                    )
                    if progress is not None:
                        progress(
                            ProgressInfo(
                                f"{account.name}: created {folder_name}",
                            )
                        )
                except Exception:
                    logger.exception(
                        "Failed to create folder %s/%s",
                        account.name,
                        folder_name,
                    )

            for folder_plan in plan.folders:
                if (
                    folder_plan.needs_confirmation
                    and folder_plan.folder_name not in confirmed_folders
                ):
                    logger.warning(
                        "Skipping %s/%s — needs confirmation",
                        account.name,
                        folder_plan.folder_name,
                    )
                    skipped.append(folder_plan.folder_name)
                    continue
                logger.info(
                    "Executing %d op(s) for %s/%s",
                    len(folder_plan.ops),
                    account.name,
                    folder_plan.folder_name,
                )
                try:
                    result = self._execute_folder_plan(
                        account=account,
                        plan=folder_plan,
                        session=session,
                        mirror=mirror,
                        progress=progress,
                    )
                except (OSError, EOFError) as exc:
                    logger.info(
                        "Connection lost executing %s/%s: %s — reconnecting",
                        account.name,
                        folder_plan.folder_name,
                        exc,
                    )
                    try:
                        with contextlib.suppress(Exception):
                            session.logout()
                        session = self._session_factory(account, password)
                        result = self._execute_folder_plan(
                            account=account,
                            plan=folder_plan,
                            session=session,
                            mirror=mirror,
                            progress=progress,
                        )
                    except (OSError, EOFError) as retry_exc:
                        logger.warning(
                            "Retry failed for %s/%s: %s",
                            account.name,
                            folder_plan.folder_name,
                            retry_exc,
                        )
                        skipped.append(folder_plan.folder_name)
                        continue
                logger.info(
                    "%s/%s: %d fetched, %d flag updates",
                    account.name,
                    folder_plan.folder_name,
                    result.fetched,
                    result.flag_updates_from_server,
                )
                if progress is not None and result.has_changes:
                    progress(
                        ProgressInfo(
                            f"{account.name}/{folder_plan.folder_name}: "
                            f"{result.fetched} fetched, "
                            f"{result.flag_updates_from_server} flag updates"
                        )
                    )
                folder_results.append(result)
        finally:
            session.logout()

        logger.info(
            "Finished execution for account %r: %d folder(s)",
            account.name,
            len(folder_results),
        )
        return AccountSyncResult(
            account_name=account.name,
            folders=tuple(folder_results),
            skipped_folders=tuple(skipped),
        )

    def _execute_folder_plan(
        self,
        *,
        account: AccountConfig,
        plan: FolderSyncPlan,
        session: ImapClientSession,
        mirror: MirrorRepository,
        progress: ProgressCallback | None,
    ) -> FolderSyncResult:
        folder_name = plan.folder_name
        folder_ref = FolderRef(account_name=account.name, folder_name=folder_name)
        counters: dict[str, int] = {
            "fetched": 0,
            "flag_updates_from_server": 0,
            "flag_pushes_to_server": 0,
            "flag_conflicts_merged": 0,
            "deleted_on_server": 0,
            "moved_to_server": 0,
            "appended_to_server": 0,
        }

        # Two phases: phase-1 (fetch-heavy ops with a producer thread
        # that owns the IMAP socket) and phase-2 (mutation ops that
        # issue IMAP commands themselves on the main thread).
        phase1: list[SyncOp] = []
        phase2: list[SyncOp] = []
        for op in plan.ops:
            if isinstance(
                op,
                (
                    PushFlagsOp,
                    PushDeleteOp,
                    PushMoveOp,
                    PushAppendOp,
                    ReUploadOp,
                    PurgeLocalOp,
                ),
            ):
                phase2.append(op)
            else:
                phase1.append(op)

        q: SimpleQueue[tuple[SyncOp, bytes | None] | None] = SimpleQueue()
        fetch_ns: list[int] = []
        ingest_ns: list[int] = []

        def _producer() -> None:
            batch: list[FetchNewOp] = []
            for op in phase1:
                if isinstance(op, FetchNewOp):
                    batch.append(op)
                    if len(batch) >= 25:
                        _flush(batch)
                else:
                    _flush(batch)
                    q.put((op, None))
            _flush(batch)
            q.put(None)

        def _flush(batch: list[FetchNewOp]) -> None:
            if not batch:
                return
            try:
                _t = time.perf_counter()
                raw_map = session.fetch_messages_batch(
                    folder_name,
                    [op.uid for op in batch],
                )
                fetch_ns.append(int((time.perf_counter() - _t) * 1_000_000_000))
            except Exception:
                logger.exception("Batch fetch failed for %s", folder_name)
                raw_map = {}
            for op in batch:
                q.put((op, raw_map.get(op.uid)))
            batch.clear()

        producer: threading.Thread | None = None
        if phase1:
            producer = threading.Thread(target=_producer, daemon=True)
            producer.start()

        total_ops = len(plan.ops)
        completed = 0
        with self._index.connection():
            if phase1:
                while (item := q.get()) is not None:
                    op, raw = item
                    try:
                        _t = time.perf_counter()
                        self._execute_one(
                            op,
                            raw,
                            account=account,
                            folder_name=folder_name,
                            folder_ref=folder_ref,
                            session=session,
                            mirror=mirror,
                            counters=counters,
                        )
                        if isinstance(op, FetchNewOp) and raw:
                            ingest_ns.append(
                                int((time.perf_counter() - _t) * 1_000_000_000)
                            )
                    except Exception:
                        logger.exception(
                            "%s failed for %s/%s — skipping",
                            type(op).__name__,
                            account.name,
                            folder_name,
                        )
                    completed += 1
                    if progress is not None:
                        progress(
                            ProgressInfo(
                                f"{account.name}/{folder_name}: "
                                f"{completed}/{total_ops}",
                                current=completed,
                                total=total_ops,
                            )
                        )
            if producer is not None:
                producer.join()

            for op in phase2:
                try:
                    self._execute_one(
                        op,
                        None,
                        account=account,
                        folder_name=folder_name,
                        folder_ref=folder_ref,
                        session=session,
                        mirror=mirror,
                        counters=counters,
                    )
                except Exception:
                    logger.exception(
                        "%s failed for %s/%s — skipping",
                        type(op).__name__,
                        account.name,
                        folder_name,
                    )
                completed += 1
                if progress is not None:
                    progress(
                        ProgressInfo(
                            f"{account.name}/{folder_name}: {completed}/{total_ops}",
                            current=completed,
                            total=total_ops,
                        )
                    )

            if not plan.is_new:
                self._index.record_folder_sync_state(
                    state=FolderSyncState(
                        account_name=account.name,
                        folder_name=folder_name,
                        uid_validity=plan.uid_validity,
                        highest_uid=plan.highest_uid,
                        uidnext=plan.uidnext,
                        highest_modseq=plan.highest_modseq,
                    )
                )

        flush = getattr(mirror, "flush_writes", None)
        if flush is not None:
            flush()

        return FolderSyncResult(
            folder_name=folder_name,
            fetched=counters["fetched"],
            flag_updates_from_server=counters["flag_updates_from_server"],
            flag_pushes_to_server=counters["flag_pushes_to_server"],
            flag_conflicts_merged=counters["flag_conflicts_merged"],
            deleted_on_server=counters["deleted_on_server"],
            moved_to_server=counters["moved_to_server"],
            appended_to_server=counters["appended_to_server"],
            scan_ms=plan.scan_ms,
            fetch_ms=sum(fetch_ns) // 1_000_000,
            ingest_ms=sum(ingest_ns) // 1_000_000,
        )

    def _execute_one(
        self,
        op: SyncOp,
        raw: bytes | None,
        *,
        account: AccountConfig,
        folder_name: str,
        folder_ref: FolderRef,
        session: ImapClientSession,
        mirror: MirrorRepository,
        counters: dict[str, int],
    ) -> None:
        now = datetime.now(tz=UTC)

        match op:
            case FetchNewOp() if raw:
                self._ingest_raw(
                    account=account,
                    folder_ref=folder_ref,
                    mirror=mirror,
                    uid=op.uid,
                    message_id=op.message_id,
                    server_flags=op.server_flags,
                    extra_imap_flags=op.extra_imap_flags,
                    raw=raw,
                )
                counters["fetched"] += 1

            case ServerDeleteOp():
                row = self._index.get_message(message_ref=op.message_ref)
                if row is not None:
                    self._index.update_message(
                        message=dataclasses.replace(
                            row,
                            local_status=MessageStatus.TRASHED,
                            trashed_at=now,
                            uid=None,
                            server_flags=frozenset(),
                            extra_imap_flags=frozenset(),
                            synced_at=None,
                        )
                    )
                counters["deleted_on_server"] += 1

            case PullFlagsOp():
                row = self._index.get_message(message_ref=op.message_ref)
                if row is not None:
                    self._index.update_message(
                        message=dataclasses.replace(
                            row,
                            local_flags=op.new_flags,
                            base_flags=op.new_flags,
                            server_flags=op.new_flags,
                            extra_imap_flags=op.extra_imap_flags,
                            uid=op.uid,
                            synced_at=now,
                        )
                    )
                counters["flag_updates_from_server"] += 1

            case PushFlagsOp():
                session.store_flags(
                    folder_name,
                    op.uid,
                    op.new_flags,
                    op.extra_imap_flags,
                )
                row = self._index.get_message(message_ref=op.message_ref)
                if row is not None:
                    self._index.update_message(
                        message=dataclasses.replace(
                            row,
                            base_flags=op.new_flags,
                            server_flags=op.new_flags,
                            extra_imap_flags=op.extra_imap_flags,
                            uid=op.uid,
                            synced_at=now,
                        )
                    )
                counters["flag_pushes_to_server"] += 1

            case MergeFlagsOp():
                if op.push_to_server:
                    session.store_flags(
                        folder_name,
                        op.uid,
                        op.merged_flags,
                        op.extra_imap_flags,
                    )
                row = self._index.get_message(message_ref=op.message_ref)
                if row is not None:
                    self._index.update_message(
                        message=dataclasses.replace(
                            row,
                            local_flags=op.merged_flags,
                            base_flags=op.merged_flags,
                            server_flags=op.merged_flags,
                            extra_imap_flags=op.extra_imap_flags,
                            uid=op.uid,
                            synced_at=now,
                        )
                    )
                counters["flag_conflicts_merged"] += 1

            case ReUploadOp():
                self._execute_reupload(
                    op,
                    account=account,
                    folder_name=folder_name,
                    session=session,
                    now=now,
                )

            case PushDeleteOp():
                session.mark_deleted(folder_name, op.server_uid)
                session.expunge(folder_name)
                self._index.delete_message(message_ref=op.message_ref)
                mirror.delete_message(folder=folder_ref, storage_key=op.storage_key)

            case PurgeLocalOp():
                self._index.delete_message(message_ref=op.message_ref)
                if op.storage_key:
                    with contextlib.suppress(Exception):
                        mirror.delete_message(
                            folder=folder_ref,
                            storage_key=op.storage_key,
                        )

            case PushMoveOp():
                self._execute_push_move(
                    op,
                    account=account,
                    session=session,
                    mirror=mirror,
                    now=now,
                )

            case PushAppendOp():
                self._execute_push_append(
                    op,
                    account=account,
                    folder_name=folder_name,
                    session=session,
                    mirror=mirror,
                    now=now,
                )

            case RestoreOp():
                row = self._index.get_message(message_ref=op.message_ref)
                if row is not None:
                    self._index.update_message(
                        message=dataclasses.replace(
                            row,
                            local_status=MessageStatus.ACTIVE,
                        )
                    )
                    logger.info(
                        "Restored message id=%d in %s/%s",
                        op.message_ref.id,
                        account.name,
                        folder_name,
                    )

    def _execute_reupload(
        self,
        op: ReUploadOp,
        *,
        account: AccountConfig,
        folder_name: str,
        session: ImapClientSession,
        now: datetime,
    ) -> None:
        row = self._index.get_message(message_ref=op.message_ref)
        if row is None:
            return
        mirror = self._mirror_factory(account)
        try:
            raw = mirror.get_message_bytes(
                folder=FolderRef(
                    account_name=account.name,
                    folder_name=folder_name,
                ),
                storage_key=row.storage_key,
            )
        except Exception:
            logger.exception(
                "Cannot re-upload id=%d — mirror read failed",
                op.message_ref.id,
            )
            return
        new_uid = session.append_message(
            folder_name,
            raw,
            op.local_flags,
            op.extra_imap_flags,
        )
        logger.info(
            "Re-uploaded id=%d to %s/%s (new uid=%s)",
            op.message_ref.id,
            account.name,
            folder_name,
            new_uid,
        )
        self._index.update_message(
            message=dataclasses.replace(
                row,
                uid=new_uid,
                base_flags=op.local_flags if new_uid is not None else row.base_flags,
                server_flags=op.local_flags if new_uid is not None else frozenset(),
                extra_imap_flags=op.extra_imap_flags,
                synced_at=now if new_uid is not None else None,
            )
        )

    def _execute_push_append(
        self,
        op: PushAppendOp,
        *,
        account: AccountConfig,
        folder_name: str,
        session: ImapClientSession,
        mirror: MirrorRepository,
        now: datetime,
    ) -> None:
        row = self._index.get_message(message_ref=op.message_ref)
        if row is None:
            return
        try:
            raw = mirror.get_message_bytes(
                folder=FolderRef(
                    account_name=account.name,
                    folder_name=folder_name,
                ),
                storage_key=row.storage_key,
            )
        except Exception:
            logger.exception(
                "Cannot APPEND id=%d — mirror read failed",
                op.message_ref.id,
            )
            return
        new_uid = session.append_message(
            folder_name,
            raw,
            row.local_flags,
            row.extra_imap_flags,
        )
        logger.info(
            "APPENDed id=%d to %s/%s (new uid=%s)",
            op.message_ref.id,
            account.name,
            folder_name,
            new_uid,
        )
        if new_uid is not None:
            self._index.update_message(
                message=dataclasses.replace(
                    row,
                    uid=new_uid,
                    base_flags=row.local_flags,
                    server_flags=row.local_flags,
                    synced_at=now,
                )
            )
        # Without APPENDUID, leave uid=NULL; next sync's per-folder
        # diff will see the new server UID and emit a FetchNewOp.  To
        # avoid duplicate ingestion, the next sync would create a
        # second row — accepted trade-off for non-UIDPLUS servers.

    def _execute_push_move(
        self,
        op: PushMoveOp,
        *,
        account: AccountConfig,  # noqa: ARG002
        session: ImapClientSession,
        mirror: MirrorRepository,  # noqa: ARG002
        now: datetime,
    ) -> None:
        row = self._index.get_message(message_ref=op.message_ref)
        if row is None:
            return
        try:
            new_uid = session.move_message(
                op.source_folder,
                op.source_uid,
                op.target_folder,
            )
        except Exception:
            logger.exception(
                "MOVE failed for id=%d (%s -> %s, src uid=%d)",
                op.message_ref.id,
                op.source_folder,
                op.target_folder,
                op.source_uid,
            )
            return
        logger.info(
            "Pushed local move: id=%d %s -> %s (src uid=%d, new uid=%s)",
            op.message_ref.id,
            op.source_folder,
            op.target_folder,
            op.source_uid,
            new_uid,
        )
        if new_uid is not None:
            self._index.update_message(
                message=dataclasses.replace(
                    row,
                    uid=new_uid,
                    local_status=MessageStatus.ACTIVE,
                    source_folder=None,
                    source_uid=None,
                    base_flags=row.local_flags,
                    server_flags=row.local_flags,
                    synced_at=now,
                )
            )
        else:
            # Server omitted COPYUID; we know the move executed but
            # not the new UID.  Mark ACTIVE so the row is visible;
            # next sync's per-folder diff will re-link uid via
            # FetchNewOp's ingestion path (creating a duplicate row,
            # which the user can clean up).  Pragmatic on UIDPLUS-less
            # servers; UIDPLUS is universal on modern stacks.
            self._index.update_message(
                message=dataclasses.replace(
                    row,
                    uid=None,
                    local_status=MessageStatus.ACTIVE,
                    source_folder=None,
                    source_uid=None,
                    synced_at=None,
                )
            )

    def _ingest_raw(
        self,
        *,
        account: AccountConfig,
        folder_ref: FolderRef,
        mirror: MirrorRepository,
        uid: int,
        message_id: str,
        server_flags: frozenset[MessageFlag],
        extra_imap_flags: frozenset[str],
        raw: bytes,
    ) -> None:
        if not raw:
            logger.warning(
                "Empty body for UID %d in %s/%s — skipping",
                uid,
                account.name,
                folder_ref.folder_name,
            )
            return
        try:
            store = getattr(mirror, "store_message_async", None)
            if store is not None:
                storage_key = store(folder=folder_ref, raw_message=raw)
            else:
                storage_key = mirror.store_message(
                    folder=folder_ref,
                    raw_message=raw,
                )
        except Exception:
            logger.exception("Failed to store UID %d to mirror", uid)
            return

        # Project — the projection's MessageRef carries id=0, which
        # insert_message replaces with the autoincrement value.
        projected = project_rfc822_message(
            message_ref=MessageRef(
                account_name=account.name,
                folder_name=folder_ref.folder_name,
                id=0,
            ),
            raw_message=raw,
            storage_key=storage_key,
        )
        indexed = dataclasses.replace(
            projected,
            message_id=message_id,
            uid=uid,
            local_flags=server_flags,
            base_flags=server_flags,
            server_flags=server_flags,
            extra_imap_flags=extra_imap_flags,
            local_status=MessageStatus.ACTIVE,
            synced_at=datetime.now(tz=UTC),
        )
        self._index.insert_message(message=indexed)

    def _run_cleanup(self) -> None:
        configured = {a.name for a in self._config.accounts}
        with self._index.connection():
            for name in self._index.list_indexed_accounts():
                if name not in configured:
                    self._index.purge_account(account_name=name)
                    logger.info("Cleanup: purged stale account %r", name)
            for account in self._config.accounts:
                if not isinstance(account, AccountConfig):
                    continue
                retention = account.mirror.trash_retention_days
                if retention <= 0:
                    continue
                purged = self._index.purge_expired_trash(
                    account_name=account.name,
                    retention_days=retention,
                )
                if purged:
                    mirror = self._mirror_factory(account)
                    for folder_ref, storage_key in purged:
                        try:
                            mirror.delete_message(
                                folder=folder_ref,
                                storage_key=storage_key,
                            )
                        except Exception:
                            logger.debug(
                                "Mirror cleanup failed for %r",
                                storage_key,
                            )
                    logger.info(
                        "Cleanup: purged %d expired trashed message(s) for %r",
                        len(purged),
                        account.name,
                    )

    def _select_accounts(
        self,
        account_name: str | None,
    ) -> list[AccountConfig]:
        imap = [a for a in self._config.accounts if isinstance(a, AccountConfig)]
        if account_name is not None:
            return [a for a in imap if a.name == account_name]
        return imap

    def _find_account(self, name: str) -> AccountConfig | None:
        for a in self._config.accounts:
            if isinstance(a, AccountConfig) and a.name == name:
                return a
        return None
