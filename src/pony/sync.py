"""IMAP synchronization engine.

Implements the reconciliation model described in ``SYNCHRONIZATION.md``:
state comparison rather than intent-log replay, with Message-ID as the
stable cross-folder identity.

Sync is a two-pass process:

1. **Plan** (``ImapSyncService.plan``) — connect, fetch lightweight metadata
   (UIDs, flags), compute a ``SyncPlan`` of typed operations.  No writes to
   the local mirror or index.

2. **Execute** (``ImapSyncService.execute``) — reconnect, apply each planned
   operation, update the local mirror, index, and server-state snapshot.

``ImapSyncService.sync`` is a convenience that calls both in sequence.
"""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import hashlib
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
    """Structured progress update emitted during sync.

    ``message`` is a human-readable status line.  ``current`` and
    ``total`` allow callers to render a progress bar or percentage.
    When ``total`` is 0, the operation count is unknown (e.g. during
    initial connection) and callers should display only the message.
    """

    message: str
    current: int = 0
    total: int = 0


ProgressCallback = collections.abc.Callable[[ProgressInfo], None]


# ---------------------------------------------------------------------------
# Result types  (returned by execute / sync)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FolderSyncResult:
    """Summary of what changed during one folder sync pass."""

    folder_name: str
    fetched: int
    flag_updates_from_server: int
    flag_pushes_to_server: int
    flag_conflicts_merged: int
    deleted_on_server: int
    moved_on_server: int
    moved_to_server: int = 0   # PushMoveOp: local moves pushed to server
    appended_to_server: int = 0  # PushAppendOp: local-only messages APPENDed
    linked_local: int = 0      # LinkLocalOp: pending rows adopted a server UID
    scan_ms: int = 0    # IMAP metadata scan (fetch_uid_to_message_id)
    fetch_ms: int = 0   # IMAP body downloads (fetch_messages_batch)
    ingest_ms: int = 0  # MIME parse + mirror write + index write


@dataclass(frozen=True, slots=True)
class AccountSyncResult:
    """Summary of one full account sync pass."""

    account_name: str
    folders: tuple[FolderSyncResult, ...]
    skipped_folders: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Top-level result returned by ImapSyncService.execute / sync."""

    accounts: tuple[AccountSyncResult, ...]


# ---------------------------------------------------------------------------
# Plan operation types  (returned by plan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchNewOp:
    """Download a new message from the server and store it locally."""

    uid: int
    message_id: str  # empty string → synthetic ID derived during execution
    server_flags: frozenset[MessageFlag]
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ServerMoveOp:
    """Message was moved to another folder on the server."""

    uid: int
    message_id: str
    new_folder: str


@dataclass(frozen=True, slots=True)
class ServerDeleteOp:
    """Message was deleted on the server — move to local trash."""

    uid: int
    message_id: str


@dataclass(frozen=True, slots=True)
class PullFlagsOp:
    """Server flags changed; pull them to the local index."""

    uid: int
    message_ref: MessageRef
    new_flags: frozenset[MessageFlag]  # the server's current flags
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PushFlagsOp:
    """Local flags changed; push them to the server."""

    uid: int
    message_ref: MessageRef
    new_flags: frozenset[MessageFlag]  # the local flags to push
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class MergeFlagsOp:
    """Both sides changed flags — three-way merge."""

    uid: int
    message_ref: MessageRef
    merged_flags: frozenset[MessageFlag]
    push_to_server: bool  # False for read-only folders
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PushDeleteOp:
    """Locally-deleted message; expunge from server."""

    server_uid: int | None  # None if the message is no longer on server
    message_ref: MessageRef
    storage_key: str


@dataclass(frozen=True, slots=True)
class PushMoveOp:
    """Local move: push a server-side move to reflect the local state.

    The message exists on the server in the current folder (``uid``) and
    a local row with ``uid=NULL`` exists in ``target_folder`` — evidence
    that the user moved the message locally.  The executor runs ``UID
    MOVE`` from the current folder to ``target_folder`` on the server.
    The resulting UID in the target folder is picked up on the next sync.
    """

    uid: int
    message_id: str
    target_folder: str


@dataclass(frozen=True, slots=True)
class PushAppendOp:
    """Upload a local row with ``uid=NULL`` via IMAP ``APPEND``.

    Emitted when a local row exists with no UID and the message is not
    present on the server anywhere — the local state is the source of
    truth and needs to be pushed.
    """

    message_ref: MessageRef


@dataclass(frozen=True, slots=True)
class LinkLocalOp:
    """Adopt a new server UID into an existing ``uid=NULL`` local row.

    Emitted when a remote UID appears for a Message-ID that already has
    a local row in the same folder with no UID (e.g. the row that a
    previous PushMoveOp created in the target folder).  No bytes are
    fetched; the row is updated with the UID and the server's flag
    baseline.
    """

    uid: int
    message_ref: MessageRef
    server_flags: frozenset[MessageFlag]
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ReUploadOp:
    """C-1: message deleted on server but locally modified — re-upload."""

    uid: int
    message_ref: MessageRef
    local_flags: frozenset[MessageFlag]
    extra_imap_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RestoreOp:
    """Locally-trashed message in a read-only folder; server still has it — restore."""

    message_ref: MessageRef


type SyncOp = (
    FetchNewOp
    | ServerMoveOp
    | ServerDeleteOp
    | PullFlagsOp
    | PushFlagsOp
    | MergeFlagsOp
    | PushDeleteOp
    | PushMoveOp
    | PushAppendOp
    | LinkLocalOp
    | ReUploadOp
    | RestoreOp
)


# ---------------------------------------------------------------------------
# Plan types  (returned by plan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FolderSyncPlan:
    """All planned operations for one folder.

    ``is_new`` marks a folder that exists only in the local mirror at
    plan time — the server will receive a ``CREATE`` for it in this
    sync's pre-phase.  No IMAP scan happened, so ``uid_validity`` /
    ``highest_uid`` carry placeholder zeros and the post-execute
    watermark recording is skipped; the next sync does the real scan.

    ``highest_modseq`` is the CONDSTORE (RFC 7162) watermark observed
    at STATUS time.  Zero when CONDSTORE is unavailable or when the
    folder is brand new.  Persisted post-execute to gate the next
    sync's CONDSTORE-aware fast/medium-path decision.
    """

    folder_name: str
    uid_validity: int
    highest_uid: int  # max surviving UID; used for progress reporting only
    ops: tuple[SyncOp, ...]
    needs_confirmation: bool = False  # C-6: mass-deletion threshold exceeded
    is_new: bool = False
    scan_ms: int = 0  # wall time for the IMAP metadata scan (fetch_uid_to_message_id)
    highest_modseq: int = 0
    # Server-observed UIDNEXT at STATUS time; this is the *authoritative*
    # gate watermark for the next sync (``highest_uid + 1`` is wrong
    # whenever UIDs have been burned — delivered then expunged).
    uidnext: int = 0


@dataclass(frozen=True, slots=True)
class AccountSyncPlan:
    """All planned operations for one account.

    ``creates`` lists folders that exist in the local mirror but not on
    the server yet.  They are executed with ``CREATE`` before any
    per-folder op runs, so a ``PushMoveOp`` that targets a newly-created
    folder has a live destination on the server.
    """

    account_name: str
    folders: tuple[FolderSyncPlan, ...]
    skipped_folders: tuple[str, ...] = ()
    creates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """Top-level plan returned by ``ImapSyncService.plan``."""

    accounts: tuple[AccountSyncPlan, ...]

    def is_empty(self) -> bool:
        """True when no operations are planned across all accounts."""
        for a in self.accounts:
            if a.creates:
                return False
            if any(f.ops for f in a.folders):
                return False
        return True

    def count_ops(self, op_type: type) -> int:
        """Count planned operations of a given type across all accounts."""
        return sum(
            1
            for a in self.accounts
            for f in a.folders
            for op in f.ops
            if isinstance(op, op_type)
        )


# ---------------------------------------------------------------------------
# Synthetic Message-ID derivation (C-8: missing/malformed header)
# ---------------------------------------------------------------------------


def _synthetic_message_id(
    *,
    account_name: str,
    folder_name: str,
    uid: int,
    raw_message: bytes,
) -> str:
    """Derive a stable synthetic Message-ID when the header is absent."""
    digest = hashlib.sha256(
        f"{account_name}\x00{folder_name}\x00{uid}\x00".encode() + raw_message[:512]
    ).hexdigest()[:32]
    return f"<synthetic-{digest}@pony.local>"


# ---------------------------------------------------------------------------
# Three-way flag merge
# ---------------------------------------------------------------------------


def _merge_flags(
    *,
    local: frozenset[MessageFlag],
    base: frozenset[MessageFlag],  # noqa: ARG001  # reserved for future 3-way merge
    remote: frozenset[MessageFlag],
) -> frozenset[MessageFlag]:
    """Return the union of local and remote flag sets, minus ``\\Deleted``.

    ``\\Deleted`` is stripped before merging — it requires explicit user
    confirmation (SYNCHRONIZATION.md C-1/C-2) and must never be
    propagated by automatic union.

    ``base`` is accepted for parity with a proper three-way merge so that
    a future refinement can distinguish "both sides set SEEN independently"
    (convergent) from "both sides set incompatible flags" (a real
    conflict) without changing the signature.
    """
    deleted = frozenset({MessageFlag.DELETED})
    return (local - deleted) | (remote - deleted)


# ---------------------------------------------------------------------------
# Sync engine
# ---------------------------------------------------------------------------


class ImapSyncService:
    """Reconcile local mirror and index against an IMAP server.

    Implements :class:`pony.protocols.SyncService`.

    Parameters
    ----------
    config:
        Full application configuration.
    mirror_factory:
        Callable that returns a :class:`MirrorRepository` for a given account.
    index:
        Shared index repository (all accounts write to the same SQLite DB).
    credentials:
        Provider that returns the password for a given account name.
    session_factory:
        Callable ``(account_config, password) → ImapClientSession``.
        Defaults to creating a real :class:`~pony.imap_client.ImapSession`.
    """

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
        """Connect to each server, fetch metadata, and return a plan of operations.

        Does not write to the local mirror or index (except clearing stale
        UID columns when UIDVALIDITY has reset).

        If *progress* is provided, it is called with status messages
        during the planning phase.
        """
        accounts = self._select_accounts(account_name)
        account_plans: list[AccountSyncPlan] = []
        errors: list[str] = []
        for account in accounts:
            if progress is not None:
                progress(ProgressInfo(f"Connecting to {account.name}…"))
            try:
                account_plans.append(
                    self._plan_account(
                        account=account, progress=progress,
                    )
                )
            except Exception as exc:
                logger.exception("Planning failed for account %r", account.name)
                errors.append(f"{account.name}: {exc}")
        plan = SyncPlan(accounts=tuple(account_plans))
        if errors and plan.is_empty():
            raise RuntimeError(
                "Sync planning failed:\n" + "\n".join(errors)
            )
        return plan

    def execute(
        self,
        plan: SyncPlan,
        *,
        confirmed_folders: frozenset[str] = frozenset(),
        progress: ProgressCallback | None = None,
    ) -> SyncResult:
        """Execute a previously computed plan and return what changed.

        Folders with ``needs_confirmation=True`` (C-6: mass deletion) are
        skipped unless their name appears in *confirmed_folders*.

        If *progress* is provided, it is called with a status message
        after each folder completes (e.g. ``"QTEP/INBOX: 149 fetched"``).
        """
        # Periodic cleanup: stale accounts, expired trash.
        self._run_cleanup()

        results: list[AccountSyncResult] = []
        for account_plan in plan.accounts:
            account = self._find_account(account_plan.account_name)
            if account is None:
                logger.warning(
                    "Account %r in plan not found in config — skipping",
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
        """Plan and execute in one step — no confirmation gate.

        All folders are implicitly confirmed (mass-delete threshold
        does not block in headless mode).
        """
        plan = self.plan(account_name=account_name)
        all_folders = frozenset(
            f.folder_name for a in plan.accounts for f in a.folders
        )
        return self.execute(plan, confirmed_folders=all_folders)

    # ------------------------------------------------------------------
    # Planning pass
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
            result = self._plan_folders(
                account=account, session=session,
                progress=progress, reconnect=_reconnect,
            )
        finally:
            with contextlib.suppress(Exception):
                session.logout()
        return result

    def _plan_folders(
        self,
        *,
        account: AccountConfig,
        session: ImapClientSession,
        progress: ProgressCallback | None = None,
        reconnect: collections.abc.Callable[[], ImapClientSession] | None = None,
    ) -> AccountSyncPlan:
        folder_policy = account.folders
        all_server_folders = session.list_folders()
        server_folders = [
            f for f in all_server_folders if folder_policy.should_sync(f)
        ]

        # Folders that exist in the local mirror but not on the server
        # yet (and pass the sync policy) get a CREATE op at the head of
        # the execution pass.  This is the signal for any locally-created
        # folder — whether from the TUI's "new folder" action or from
        # archiving into a fresh folder — to be propagated upstream.
        mirror = self._mirror_factory(account)
        try:
            mirror_folder_names = {
                f.folder_name
                for f in mirror.list_folders(account_name=account.name)
            }
        except Exception:
            logger.exception(
                "Failed to list mirror folders for %r", account.name,
            )
            mirror_folder_names = set()
        server_folder_set = set(all_server_folders)
        creates = tuple(
            sorted(
                name for name in mirror_folder_names - server_folder_set
                if folder_policy.should_sync(name)
            )
        )

        # Share one DB connection for all planning-phase index reads.
        with self._index.connection():
            return self._plan_folders_inner(
                account=account, session=session,
                folder_policy=folder_policy,
                all_server_folders=all_server_folders,
                server_folders=server_folders,
                creates=creates,
                progress=progress, reconnect=reconnect,
            )

    def _plan_folders_inner(
        self,
        *,
        account: AccountConfig,
        session: ImapClientSession,
        folder_policy: FolderConfig,
        all_server_folders: collections.abc.Sequence[str],
        server_folders: list[str],
        creates: tuple[str, ...] = (),
        progress: ProgressCallback | None = None,
        reconnect: collections.abc.Callable[
            [], ImapClientSession
        ] | None = None,
    ) -> AccountSyncPlan:
        # Warn about Gmail aggregate folders that should typically be
        # excluded to avoid duplicate Message-ID issues (see 7b).
        gmail_aggregate = {"[Gmail]/All Mail", "[Gmail]/Important"}
        synced_aggregate = gmail_aggregate & set(server_folders)
        if synced_aggregate:
            logger.warning(
                "Account %r syncs Gmail aggregate folder(s) %s — consider "
                "adding them to folders.exclude to avoid duplicates",
                account.name, ", ".join(sorted(synced_aggregate)),
            )

        # Clean up sync/server state for folders no longer on the server.
        stale = self._index.purge_stale_folders(
            account_name=account.name,
            active_folders=frozenset(all_server_folders),
        )
        if stale:
            logger.info(
                "Cleanup: purged state for %d stale folder(s) in %r: %s",
                len(stale), account.name, ", ".join(stale),
            )

        # Build local-pending map up-front: Message-ID → folder name for
        # index rows that have no UID yet and are still ACTIVE.  These
        # rows represent local moves or appends that sync will reconcile
        # on this pass.  Built before the folder scan loop so the merge
        # logic can short-circuit the cross-folder mid map on fully
        # quiescent accounts (no pending rows, stable server state).
        #
        # Two invariants:
        #
        #   (a) The target folder must be syncable and writable — a pending
        #       row pointing at a read-only or excluded folder cannot be
        #       pushed to the server, so skip it rather than emitting a
        #       doomed PushMoveOp.
        #   (b) A Message-ID must not map to more than one folder.  If two
        #       pending rows share the same mid (pathological), we can't
        #       pick one deterministically — keep the first, warn on the
        #       rest.  The losers stay uid=NULL until the winner completes
        #       and the user resolves the duplicate.
        local_pending_mid_folder: dict[str, str] = {}
        for row in self._index.list_pending_rows(account_name=account.name):
            mid = row.message_ref.rfc5322_id
            if not mid:
                continue
            folder = row.message_ref.folder_name
            if folder_policy.is_read_only(folder):
                logger.warning(
                    "Pending row %r in read-only folder %s/%s — cannot "
                    "push to server; skipping",
                    mid, account.name, folder,
                )
                continue
            if not folder_policy.should_sync(folder):
                logger.warning(
                    "Pending row %r in excluded folder %s/%s — cannot "
                    "push to server; skipping",
                    mid, account.name, folder,
                )
                continue
            if mid in local_pending_mid_folder:
                logger.warning(
                    "Duplicate pending Message-ID %r in %s/%s "
                    "(already mapped to %s); keeping the first mapping",
                    mid, account.name, folder,
                    local_pending_mid_folder[mid],
                )
                continue
            local_pending_mid_folder[mid] = folder

        # Build a global Message-ID → (folder, uid) map across all synced
        # folders for cross-folder move detection (SYNCHRONIZATION.md step 1).
        # Folders that fail here (e.g. protected, SELECT denied) are skipped.
        remote_mid_map: dict[str, tuple[str, int]] = {}
        _ambiguous_mids: set[str] = set()
        folder_uid_maps: dict[str, dict[int, str]] = {}
        folder_flags_maps: dict[str, dict[int, FlagSet]] = {}
        folder_scan_ms: dict[str, int] = {}
        # STATUS-time UIDNEXT per folder; zero when STATUS failed.
        # Recorded into folder_sync_state after successful execute so
        # the next sync's fast-path gate can compare exactly.  Must be
        # the server's UIDNEXT — not ``max_surviving_uid + 1`` — to
        # handle folders where UIDs have been burned.
        folder_observed_uidnext: dict[str, int] = {}
        # STATUS-time HIGHESTMODSEQ per folder; zero when the server
        # does not advertise CONDSTORE or STATUS failed.  Recorded into
        # folder_sync_state after successful execute so the next sync's
        # CONDSTORE gate has a watermark to compare against.
        folder_observed_modseq: dict[str, int] = {}
        fast_path_folders: set[str] = set()
        fast_path_state: dict[str, FolderSyncState] = {}
        skipped: list[str] = []

        def _merge_mid_map(
            folder: str, uid_to_mid: dict[int, str], *, always: bool,
        ) -> None:
            # Fast/medium folders skip the merge when no pending rows
            # exist account-wide: ``remote_mid_map`` is consulted only
            # by pending-row suppression and by slow-path Step 1, and
            # a fast/medium folder's UIDs cannot participate in a
            # server-side move detectable by any other folder's Step 1
            # (a move would have changed this folder's MESSAGES count,
            # forcing it onto the slow path too).
            if not always and not local_pending_mid_folder:
                return
            for uid, mid in uid_to_mid.items():
                if not mid or mid in _ambiguous_mids:
                    continue
                if mid in remote_mid_map:
                    existing_folder, _ = remote_mid_map[mid]
                    if existing_folder != folder:
                        logger.debug(
                            "Message-ID %r in multiple folders (%s, %s)"
                            " — disabling move detection for this ID",
                            mid, existing_folder, folder,
                        )
                        _ambiguous_mids.add(mid)
                        del remote_mid_map[mid]
                else:
                    remote_mid_map[mid] = (folder, uid)

        for i, folder_name in enumerate(server_folders, 1):
            if progress is not None:
                progress(ProgressInfo(
                    f"{account.name}: scanning {folder_name}"
                    f" ({i}/{len(server_folders)})",
                    current=i,
                    total=len(server_folders),
                )
                )

            # One cheap STATUS roundtrip per folder, always.  The result
            # drives fast/medium/slow path selection AND captures the
            # server's current HIGHESTMODSEQ — we need the modseq on
            # the very first sync too, so subsequent passes have a
            # watermark to compare against.
            stored = self._index.get_folder_sync_state(
                account_name=account.name, folder_name=folder_name,
            )
            quick: FolderQuickStatus | None = None
            try:
                _t0 = time.perf_counter()
                quick = session.folder_quick_status(folder_name)
                folder_scan_ms[folder_name] = int(
                    (time.perf_counter() - _t0) * 1000
                )
            except (OSError, EOFError) as exc:
                if reconnect is not None:
                    logger.info(
                        "Connection lost on STATUS %s/%s: %s"
                        " — reconnecting",
                        account.name, folder_name, exc,
                    )
                    try:
                        session = reconnect()
                        _t0 = time.perf_counter()
                        quick = session.folder_quick_status(folder_name)
                        folder_scan_ms[folder_name] = int(
                            (time.perf_counter() - _t0) * 1000
                        )
                    except (OSError, EOFError):
                        quick = None
                else:
                    quick = None

            # STATUS-gated path selection (RFC 7162 CONDSTORE-aware).
            #
            # Base conditions: UIDVALIDITY, UIDNEXT, MESSAGES all match
            # the local snapshot — the server's UID set is stable.
            #
            # CONDSTORE layer: when the server advertises HIGHESTMODSEQ,
            # we additionally split by whether the modseq advanced:
            #   - base conditions + modseq match  -> fast path
            #     (no server I/O; correctness-preserving).
            #   - base conditions + modseq differs -> medium path
            #     (FETCH ... (FLAGS) CHANGEDSINCE stored_modseq —
            #     returns only messages whose flags changed).
            # Without CONDSTORE, ``stored_modseq == 0`` and
            # ``quick.highest_modseq is None``; we take the fast path
            # as before and accept the Phase 1 limitation.
            uid_set_stable = (
                quick is not None
                and stored is not None
                and quick.uid_validity == stored.uid_validity
                and stored.uidnext > 0
                and quick.uidnext == stored.uidnext
                and quick.messages
                == self._index.count_uids_for_folder(
                    account_name=account.name,
                    folder_name=folder_name,
                )
            )
            if quick is not None:
                folder_observed_uidnext[folder_name] = quick.uidnext
                if quick.highest_modseq is not None:
                    folder_observed_modseq[folder_name] = quick.highest_modseq

            if uid_set_stable and stored is not None and quick is not None:
                stored_modseq = stored.highest_modseq
                remote_modseq = quick.highest_modseq
                modseq_matches = (
                    remote_modseq is None
                    or stored_modseq == 0
                    or remote_modseq == stored_modseq
                )
                uid_to_mid_local = self._index.list_folder_uid_to_mid(
                    account_name=account.name,
                    folder_name=folder_name,
                )
                if modseq_matches:
                    # Fast path: nothing changed server-side.  Push
                    # whatever the user changed locally with no FETCH.
                    folder_uid_maps[folder_name] = uid_to_mid_local
                    folder_flags_maps[folder_name] = {}
                    fast_path_folders.add(folder_name)
                    fast_path_state[folder_name] = stored
                    _merge_mid_map(
                        folder_name, uid_to_mid_local, always=False,
                    )
                    logger.debug(
                        "Fast-path %s/%s: %d msg(s), stable server state",
                        account.name, folder_name, quick.messages,
                    )
                    continue

                # Medium path: UID set stable, flags changed server-side.
                try:
                    _t0 = time.perf_counter()
                    changed_flags = session.fetch_flags_changed_since(
                        folder_name, stored_modseq,
                    )
                    folder_scan_ms[folder_name] = folder_scan_ms.get(
                        folder_name, 0,
                    ) + int((time.perf_counter() - _t0) * 1000)
                except (OSError, EOFError) as exc:
                    logger.info(
                        "Medium-path CHANGEDSINCE failed on %s/%s: %s"
                        " — falling through to slow path",
                        account.name, folder_name, exc,
                    )
                else:
                    # Synthesize the planner's inputs: start with the
                    # local uid→mid map (UID set is stable, so it is
                    # complete); fill in every UID's flags from either
                    # the CHANGEDSINCE response or the local index
                    # baseline (unchanged messages need a value for the
                    # planner's Step 3 comparison — ``base_flags`` is
                    # the right value because "no modseq advance" means
                    # the server still has that baseline).
                    base_by_uid: dict[int, FlagSet] = (
                        self._index.list_folder_base_flags(
                            account_name=account.name,
                            folder_name=folder_name,
                        )
                    )
                    uid_to_flags_medium: dict[int, FlagSet] = {}
                    for uid in uid_to_mid_local:
                        if uid in changed_flags:
                            uid_to_flags_medium[uid] = changed_flags[uid]
                        else:
                            uid_to_flags_medium[uid] = base_by_uid.get(
                                uid, (frozenset(), frozenset()),
                            )
                    folder_uid_maps[folder_name] = uid_to_mid_local
                    folder_flags_maps[folder_name] = uid_to_flags_medium
                    _merge_mid_map(
                        folder_name, uid_to_mid_local, always=False,
                    )
                    logger.debug(
                        "Medium-path %s/%s: CHANGEDSINCE %d returned %d row(s)",
                        account.name, folder_name, stored_modseq,
                        len(changed_flags),
                    )
                    continue

            # Slow path: full FETCH of UIDs + Message-IDs + flags.
            try:
                _t0 = time.perf_counter()
                uid_metadata = session.fetch_uid_to_message_id(folder_name)
                folder_scan_ms[folder_name] = int((time.perf_counter() - _t0) * 1000)
            except (OSError, EOFError) as exc:
                # Connection may have dropped (SSL EOF, timeout).
                # Try reconnecting once before skipping the folder.
                if reconnect is not None:
                    logger.info(
                        "Connection lost scanning %s/%s: %s"
                        " — reconnecting",
                        account.name, folder_name, exc,
                    )
                    try:
                        session = reconnect()
                        _t0 = time.perf_counter()
                        uid_metadata = session.fetch_uid_to_message_id(
                            folder_name,
                        )
                        folder_scan_ms[folder_name] = int(
                            (time.perf_counter() - _t0) * 1000
                        )
                    except (OSError, EOFError) as retry_exc:
                        logger.debug(
                            "Retry failed for %s/%s: %s",
                            account.name, folder_name,
                            retry_exc,
                        )
                        skipped.append(folder_name)
                        continue
                else:
                    logger.debug(
                        "Skipping folder %s/%s: %s",
                        account.name, folder_name, exc,
                    )
                    skipped.append(folder_name)
                    continue

            uid_to_mid: dict[int, str] = {
                uid: v[0] for uid, v in uid_metadata.items()
            }
            uid_to_flags: dict[int, FlagSet] = {
                uid: v[1] for uid, v in uid_metadata.items()
            }

            # C-7: deduplicate Message-IDs within the folder.  The second
            # occurrence gets an empty mid → synthetic ID during fetch.
            seen_in_folder: dict[str, int] = {}
            for uid in sorted(uid_to_mid):
                mid = uid_to_mid[uid]
                if not mid:
                    continue
                if mid in seen_in_folder:
                    logger.warning(
                        "Duplicate Message-ID %r in %s/%s (UIDs %d, %d)"
                        " — move detection disabled for UID %d",
                        mid, account.name, folder_name,
                        seen_in_folder[mid], uid, uid,
                    )
                    uid_to_mid[uid] = ""
                else:
                    seen_in_folder[mid] = uid

            folder_uid_maps[folder_name] = uid_to_mid
            folder_flags_maps[folder_name] = uid_to_flags
            _merge_mid_map(folder_name, uid_to_mid, always=True)

        # Lazy mid→folders loader — the map is only consulted by the
        # slow-path planner's Step 1 (distinguishing a server-side move
        # from a delete).  Fast and medium paths never touch it, so
        # most syncs never pay the account-wide SELECT.  Cached after
        # first call so multiple slow-path folders in one sync only
        # rebuild once.
        _mid_folders_cache: dict[str, set[str]] | None = None

        def _local_mid_folders() -> dict[str, set[str]]:
            nonlocal _mid_folders_cache
            if _mid_folders_cache is None:
                _mid_folders_cache = (
                    self._index.list_mid_folders_for_account(
                        account_name=account.name,
                    )
                )
            return _mid_folders_cache

        syncable = [f for f in server_folders if f in folder_uid_maps]
        logger.info(
            "Account %r: planning %d folder(s): %s",
            account.name,
            len(syncable),
            ", ".join(syncable),
        )

        folder_plans: list[FolderSyncPlan] = []
        for folder_name in syncable:
            try:
                if folder_name in fast_path_folders:
                    folder_plan = self._plan_fast_path_folder(
                        account=account,
                        folder_name=folder_name,
                        stored=fast_path_state[folder_name],
                        remote_mid_map=remote_mid_map,
                        read_only=folder_policy.is_read_only(folder_name),
                    )
                else:
                    folder_plan = self._plan_folder(
                        account=account,
                        folder_name=folder_name,
                        session=session,
                        uid_to_mid=folder_uid_maps[folder_name],
                        uid_to_flags=folder_flags_maps[folder_name],
                        remote_mid_map=remote_mid_map,
                        local_mid_folders=_local_mid_folders,
                        local_pending_mid_folder=local_pending_mid_folder,
                        read_only=folder_policy.is_read_only(folder_name),
                        has_aggregate=bool(synced_aggregate),
                    )
                folder_plan = dataclasses.replace(
                    folder_plan,
                    scan_ms=folder_scan_ms.get(folder_name, 0),
                    uidnext=folder_observed_uidnext.get(folder_name, 0),
                    highest_modseq=folder_observed_modseq.get(
                        folder_name, 0,
                    ),
                )
            except OSError as exc:
                logger.warning(
                    "Failed to plan folder %s/%s: %s",
                    account.name, folder_name, exc,
                )
                skipped.append(folder_name)
                continue
            folder_plans.append(folder_plan)

        # Brand-new folders (local mirror only, about to be CREATEd on
        # the server this pass) still need a plan so their pending
        # uid=NULL rows get APPENDed in the same sync — the CREATE ops
        # run first in _execute_account_plan, so the destination exists
        # by the time APPEND executes.
        for folder_name in creates:
            folder_plans.append(
                self._plan_new_folder(
                    account=account,
                    folder_name=folder_name,
                    remote_mid_map=remote_mid_map,
                )
            )

        return AccountSyncPlan(
            account_name=account.name,
            folders=tuple(folder_plans),
            skipped_folders=tuple(skipped),
            creates=creates,
        )

    def _plan_fast_path_folder(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        stored: FolderSyncState,
        remote_mid_map: dict[str, tuple[str, int]],
        read_only: bool = False,
    ) -> FolderSyncPlan:
        """Plan ops when STATUS confirmed the server UID set is stable.

        ``STATUS`` already matched UIDVALIDITY, UIDNEXT, and MESSAGES
        against the stored watermark — no server-side adds, deletions,
        or (Phase-1 assumption) flag changes.  The planner only pushes
        whatever changed locally; no ``FETCH`` is issued.

        - Flag drift on active rows with a server uid ⇒ ``PushFlagsOp``.
        - Trashed rows ⇒ ``PushDeleteOp`` (writable folders).
        - Pending ``uid=NULL`` ACTIVE rows ⇒ ``PushAppendOp`` (unless
          the Message-ID is on the server in another folder, in which
          case that folder's plan emits ``PushMoveOp``).
        """
        candidates = self._index.list_folder_push_candidates(
            account_name=account.name, folder_name=folder_name,
        )
        ops: list[SyncOp] = []
        for row in candidates:
            if row.local_status == MessageStatus.TRASHED:
                if read_only:
                    ops.append(RestoreOp(message_ref=row.message_ref))
                else:
                    ops.append(
                        PushDeleteOp(
                            server_uid=row.uid,
                            message_ref=row.message_ref,
                            storage_key=row.storage_key,
                        )
                    )
                continue
            if read_only:
                # Flag drift / pending appends cannot be pushed to a
                # read-only folder — the SQL returned them, but there
                # is no op for them.  Drop silently.
                continue
            if row.uid is None:
                # Pending uid=NULL ACTIVE row — PushAppendOp unless the
                # Message-ID is already on the server in another folder
                # (that folder's plan will emit PushMoveOp here).
                mid = row.message_ref.rfc5322_id
                if not mid or mid in remote_mid_map:
                    continue
                ops.append(PushAppendOp(message_ref=row.message_ref))
            else:
                # Flag drift on an ACTIVE row with a server UID.
                ops.append(
                    PushFlagsOp(
                        uid=row.uid,
                        message_ref=row.message_ref,
                        new_flags=row.local_flags,
                        extra_imap_flags=row.extra_imap_flags,
                    )
                )
        return FolderSyncPlan(
            folder_name=folder_name,
            uid_validity=stored.uid_validity,
            highest_uid=stored.highest_uid,
            ops=tuple(ops),
        )

    def _plan_new_folder(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        remote_mid_map: dict[str, tuple[str, int]],
    ) -> FolderSyncPlan:
        """Plan ops for a folder that lives only in the local mirror.

        No IMAP scan is possible yet (the folder doesn't exist on the
        server).  Emit one :class:`PushAppendOp` for every pending
        ``uid=NULL`` ACTIVE row whose Message-ID is not also on the
        server in another folder — messages that *are* on the server get
        a :class:`PushMoveOp` in the source folder's plan instead.
        """
        candidates = self._index.list_folder_push_candidates(
            account_name=account.name, folder_name=folder_name,
        )
        ops: list[SyncOp] = []
        for row in candidates:
            if row.local_status != MessageStatus.ACTIVE:
                continue
            if row.uid is not None:
                continue
            mid = row.message_ref.rfc5322_id
            if not mid or mid in remote_mid_map:
                continue
            ops.append(PushAppendOp(message_ref=row.message_ref))
        return FolderSyncPlan(
            folder_name=folder_name,
            uid_validity=0,  # no scan yet — folder about to be CREATEd
            highest_uid=0,
            ops=tuple(ops),
            is_new=True,
        )

    def _plan_folder(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        session: ImapClientSession,
        uid_to_mid: dict[int, str],
        uid_to_flags: dict[int, FlagSet],
        remote_mid_map: dict[str, tuple[str, int]],
        local_mid_folders: collections.abc.Callable[
            [], dict[str, set[str]]
        ],
        local_pending_mid_folder: dict[str, str],
        read_only: bool = False,
        has_aggregate: bool = False,
    ) -> FolderSyncPlan:
        # Always-fresh SELECT; see ImapClientSession.get_uid_validity.
        uid_validity = session.get_uid_validity(folder_name)

        if uid_validity <= 0:
            logger.warning(
                "Server reported UIDVALIDITY %d for %s/%s"
                " — UID stability is not guaranteed",
                uid_validity, account.name, folder_name,
            )

        stored_sync_state = self._index.get_folder_sync_state(
            account_name=account.name, folder_name=folder_name
        )
        if (
            stored_sync_state is not None
            and stored_sync_state.uid_validity != uid_validity
        ):
            # C-4: UIDVALIDITY reset — clear stale UIDs on messages.
            logger.warning(
                "UIDVALIDITY changed for %s/%s (was %d, now %d) — full resync",
                account.name,
                folder_name,
                stored_sync_state.uid_validity,
                uid_validity,
            )
            self._index.clear_uids_for_folder(
                account_name=account.name, folder_name=folder_name
            )
            stored_sync_state = None

        remote_flags: dict[int, frozenset[MessageFlag]] = {
            uid: f[0] for uid, f in uid_to_flags.items()
        }
        remote_extra: dict[int, frozenset[str]] = {
            uid: f[1] for uid, f in uid_to_flags.items()
        }

        # Load the narrow slow-path projection of this folder's rows from
        # the unified index.  ``SlowPathRow`` carries only the seven
        # columns Steps 1/3/4/5 actually read; hydrating
        # ``IndexedMessage`` here pays datetime parsing and flag-set
        # construction on columns the planner never touches.
        index_rows = self._index.list_folder_slow_path_rows(
            account_name=account.name, folder_name=folder_name,
        )
        index_by_mid: dict[str, SlowPathRow] = {
            row.message_ref.rfc5322_id: row for row in index_rows
        }

        # Build known-by-uid from messages that have a UID (previously synced).
        known_by_uid: dict[int, SlowPathRow] = {
            row.uid: row for row in index_rows if row.uid is not None
        }

        remote_uids = set(uid_to_mid.keys())
        local_uids = set(known_by_uid.keys())

        ops: list[SyncOp] = []

        # Step 1: messages gone from this folder since last sync.
        for uid in local_uids - remote_uids:
            known = known_by_uid[uid]
            known_mid = known.message_ref.rfc5322_id
            if known_mid and known_mid in remote_mid_map:
                new_folder, _ = remote_mid_map[known_mid]
                # Only treat as a move if the destination did NOT already
                # have this message in the previous sync.  If it did, the
                # message existed in both folders and this is a delete.
                # The dup-guard is only meaningful when an aggregate
                # folder (Gmail-style [Gmail]/All Mail) is synced — that
                # is the only way a single message legitimately appears
                # in two folders' previous-sync snapshots.  Without one,
                # the account-wide ``local_mid_folders`` query is wasted
                # I/O.
                if has_aggregate:
                    prev_folders = local_mid_folders().get(known_mid, set())
                else:
                    prev_folders = set()
                is_move = (
                    new_folder != folder_name
                    and new_folder not in prev_folders
                )
                if is_move:
                    ops.append(
                        ServerMoveOp(
                            uid=uid,
                            message_id=known_mid,
                            new_folder=new_folder,
                        )
                    )
                else:
                    ops.append(
                        ServerDeleteOp(uid=uid, message_id=known_mid)
                    )
            else:
                # C-1: if the message was locally modified, re-upload it
                # to the server instead of trashing it locally.
                if (
                    known.local_flags != known.base_flags
                    and not read_only
                ):
                    ops.append(
                        ReUploadOp(
                            uid=uid,
                            message_ref=known.message_ref,
                            local_flags=known.local_flags,
                            extra_imap_flags=known.extra_imap_flags,
                        )
                    )
                else:
                    ops.append(
                        ServerDeleteOp(uid=uid, message_id=known_mid)
                    )

        # Track pending Message-IDs we handled via Link/PushMove so step 5
        # doesn't try to APPEND them.
        _handled_pending_mids: set[str] = set()

        # Step 2: new UIDs on the server in this folder.
        #
        # Extended to recognise local-pending rows (uid=NULL):
        #   - Same folder  → LinkLocalOp (adopt the UID, no refetch).
        #   - Other folder → PushMoveOp (move server-side to match the
        #                    local state).  Skipped for read-only sources.
        #   - Otherwise    → FetchNewOp (genuinely new server message, or
        #                    a second-folder copy that will dedup by msgid).
        for uid in remote_uids - local_uids:
            mid = uid_to_mid.get(uid, "")
            pending_folder = (
                local_pending_mid_folder.get(mid) if mid else None
            )
            if pending_folder == folder_name:
                ops.append(
                    LinkLocalOp(
                        uid=uid,
                        message_ref=MessageRef(
                            account_name=account.name,
                            folder_name=folder_name,
                            rfc5322_id=mid,
                        ),
                        server_flags=remote_flags.get(uid, frozenset()),
                        extra_imap_flags=remote_extra.get(uid, frozenset()),
                    )
                )
                _handled_pending_mids.add(mid)
            elif pending_folder is not None and not read_only:
                ops.append(
                    PushMoveOp(
                        uid=uid,
                        message_id=mid,
                        target_folder=pending_folder,
                    )
                )
                _handled_pending_mids.add(mid)
            else:
                ops.append(
                    FetchNewOp(
                        uid=uid,
                        message_id=mid,
                        server_flags=remote_flags.get(uid, frozenset()),
                        extra_imap_flags=remote_extra.get(uid, frozenset()),
                    )
                )

        # Track messages restored by C-2 so Step 4 skips them.
        _restored_mids: set[str] = set()

        # Step 3: flag reconciliation for surviving messages.
        for uid in remote_uids & local_uids:
            known = known_by_uid[uid]
            current_remote = remote_flags.get(uid, frozenset())
            current_extra = remote_extra.get(uid, frozenset())
            local_row = index_by_mid.get(known.message_ref.rfc5322_id)

            if local_row is None:
                # 1c: the server may have changed the Message-ID header
                # (re-import, migration).  Try the fresh mid from the
                # current fetch; if it matches a different index row,
                # use that — the server state will be updated on commit.
                fresh_mid = uid_to_mid.get(uid, "")
                if fresh_mid and fresh_mid != known.message_ref.rfc5322_id:
                    local_row = index_by_mid.get(fresh_mid)
                    if local_row is not None:
                        logger.warning(
                            "Message-ID changed for UID %d in %s/%s: "
                            "%r → %r",
                            uid, account.name, folder_name,
                            known.message_ref.rfc5322_id, fresh_mid,
                        )
                if local_row is None:
                    continue

            if local_row.local_status == MessageStatus.TRASHED:
                base = local_row.base_flags
                if read_only:
                    # Read-only folder: can't push deletion, restore.
                    ops.append(RestoreOp(message_ref=local_row.message_ref))
                    _restored_mids.add(local_row.message_ref.rfc5322_id)
                elif current_remote != base:
                    # C-2: server changed flags on a locally-trashed message
                    # in a writable folder.  Safe path: cancel the deletion,
                    # restore to active, and pull the server's new flags.
                    ops.append(RestoreOp(message_ref=local_row.message_ref))
                    ops.append(
                        PullFlagsOp(
                            uid=uid,
                            message_ref=local_row.message_ref,
                            new_flags=current_remote,
                            extra_imap_flags=current_extra,
                        )
                    )
                    _restored_mids.add(local_row.message_ref.rfc5322_id)
                # else: no server change → Step 4 handles PushDeleteOp
                continue

            base = local_row.base_flags
            server_changed = current_remote != base
            local_changed = local_row.local_flags != base

            if server_changed and not local_changed:
                ops.append(
                    PullFlagsOp(
                        uid=uid,
                        message_ref=local_row.message_ref,
                        new_flags=current_remote,
                        extra_imap_flags=current_extra,
                    )
                )
            elif local_changed and not server_changed:
                if not read_only:
                    ops.append(
                        PushFlagsOp(
                            uid=uid,
                            message_ref=local_row.message_ref,
                            new_flags=local_row.local_flags,
                            extra_imap_flags=current_extra,
                        )
                    )
            elif server_changed and local_changed:
                merged = _merge_flags(
                    local=local_row.local_flags,
                    base=base,
                    remote=current_remote,
                )
                ops.append(
                    MergeFlagsOp(
                        uid=uid,
                        message_ref=local_row.message_ref,
                        merged_flags=merged,
                        push_to_server=not read_only,
                        extra_imap_flags=current_extra,
                    )
                )

        # Step 4: push local deletions (skipped for read-only folders).
        # Messages restored by C-2 in Step 3 are excluded.
        if not read_only:
            for row in index_rows:
                if row.local_status != MessageStatus.TRASHED:
                    continue
                if row.message_ref.rfc5322_id in _restored_mids:
                    continue
                server_uid = row.uid
                ops.append(
                    PushDeleteOp(
                        server_uid=server_uid,
                        message_ref=row.message_ref,
                        storage_key=row.storage_key,
                    )
                )

        # Step 5: push local-pending rows (uid=NULL, ACTIVE) that weren't
        # matched by a server UID.  If the Message-ID is already on the
        # server in some other folder, the PushMoveOp emitted during that
        # folder's plan will handle it.  Otherwise we APPEND the mirror
        # bytes to this folder so the message reaches the server.
        if not read_only:
            for row in index_rows:
                if row.local_status != MessageStatus.ACTIVE:
                    continue
                if row.uid is not None:
                    continue
                mid = row.message_ref.rfc5322_id
                if not mid or mid in _handled_pending_mids:
                    continue
                if mid in remote_mid_map:
                    # Another folder's plan will move it to us.
                    continue
                ops.append(PushAppendOp(message_ref=row.message_ref))

        # C-6: mass-deletion safety halt.  If >20% of previously-known
        # UIDs disappeared in one pass, flag the folder for confirmation.
        delete_count = sum(1 for op in ops if isinstance(op, ServerDeleteOp))
        total_known = len(local_uids)
        needs_confirmation = (
            total_known >= 5
            and delete_count > total_known * 0.2
        )
        if needs_confirmation:
            logger.warning(
                "Mass deletion detected in %s/%s: %d of %d messages gone"
                " (%.0f%%) — folder needs confirmation",
                account.name, folder_name,
                delete_count, total_known,
                delete_count / total_known * 100,
            )

        return FolderSyncPlan(
            folder_name=folder_name,
            uid_validity=uid_validity,
            highest_uid=max(remote_uids) if remote_uids else 0,
            ops=tuple(ops),
            needs_confirmation=needs_confirmation,
        )

    # ------------------------------------------------------------------
    # Execution pass
    # ------------------------------------------------------------------

    def _execute_account_plan(
        self,
        *,
        account: AccountConfig,
        plan: AccountSyncPlan,
        confirmed_folders: frozenset[str] = frozenset(),
        progress: ProgressCallback | None = None,
    ) -> AccountSyncResult:
        logger.info("Executing sync plan for account %r", account.name)
        password = self._credentials.get_password(account_name=account.name)
        session = self._session_factory(account, password)
        mirror = self._mirror_factory(account)

        folder_results: list[FolderSyncResult] = []
        skipped = list(plan.skipped_folders)
        try:
            # Propagate local folder creations to the server first so any
            # PushMoveOp that targets a new folder has a live destination.
            for folder_name in plan.creates:
                try:
                    session.create_folder(folder_name)
                    logger.info(
                        "Created server folder %s/%s",
                        account.name, folder_name,
                    )
                    if progress is not None:
                        progress(ProgressInfo(
                            f"{account.name}: created {folder_name}",
                        ))
                except Exception:
                    logger.exception(
                        "Failed to create folder %s/%s",
                        account.name, folder_name,
                    )

            for folder_plan in plan.folders:
                if (
                    folder_plan.needs_confirmation
                    and folder_plan.folder_name not in confirmed_folders
                ):
                    logger.warning(
                        "Skipping %s/%s — mass deletion detected, "
                        "needs user confirmation",
                        account.name, folder_plan.folder_name,
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
                    # Try reconnecting once before skipping.
                    logger.info(
                        "Connection lost executing %s/%s: %s"
                        " — reconnecting",
                        account.name, folder_plan.folder_name,
                        exc,
                    )
                    try:
                        with contextlib.suppress(Exception):
                            session.logout()
                        session = self._session_factory(
                            account, password,
                        )
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
                msg = (
                    f"{account.name}/{folder_plan.folder_name}: "
                    f"{result.fetched} fetched, "
                    f"{result.flag_updates_from_server} flag updates"
                )
                logger.info("%s", msg)
                if progress is not None:
                    progress(ProgressInfo(msg))
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
        progress: ProgressCallback | None = None,
    ) -> FolderSyncResult:
        folder_name = plan.folder_name
        folder_ref = FolderRef(account_name=account.name, folder_name=folder_name)

        counters = {
            "fetched": 0,
            "flag_updates_from_server": 0,
            "flag_pushes_to_server": 0,
            "flag_conflicts_merged": 0,
            "deleted_on_server": 0,
            "moved_on_server": 0,
            "moved_to_server": 0,
            "appended_to_server": 0,
            "linked_local": 0,
        }

        # Two-phase execution per folder:
        #
        # Phase 1 (overlap): a background producer thread batches
        # FetchNewOps and calls ``fetch_messages_batch`` on the IMAP
        # session; the main thread consumes the queue and executes ops
        # that only touch the local index/mirror (FetchNewOp plus the
        # other session-free ops below).  During Phase 1, the producer
        # is the *only* thread that touches ``session`` — the consumer
        # never does.  That is the invariant that makes the overlap
        # safe; violating it (as earlier versions did) races two
        # threads on one ``imaplib`` socket and deadlocks.
        #
        # Phase 2 (serial): ops that issue IMAP commands themselves
        # (PushFlagsOp, PushDeleteOp, PushMoveOp, PushAppendOp,
        # ReUploadOp) run on the main thread in plan order after the
        # producer has finished.  No concurrency, no races.
        phase1_ops: list[SyncOp] = []
        phase2_ops: list[SyncOp] = []
        for op in plan.ops:
            if isinstance(
                op,
                (
                    PushFlagsOp,
                    PushDeleteOp,
                    PushMoveOp,
                    PushAppendOp,
                    ReUploadOp,
                ),
            ):
                phase2_ops.append(op)
            else:
                phase1_ops.append(op)

        q: SimpleQueue[tuple[SyncOp, bytes | None] | None] = SimpleQueue()
        fetch_ns: list[int] = []   # accumulated by producer thread
        ingest_ns: list[int] = []  # accumulated by main thread

        def _producer() -> None:
            batch: list[FetchNewOp] = []
            for op in phase1_ops:
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
                    folder_name, [op.uid for op in batch],
                )
                fetch_ns.append(int((time.perf_counter() - _t) * 1_000_000_000))
            except Exception:
                logger.exception("Batch fetch failed for %s", folder_name)
                raw_map = {}
            for op in batch:
                q.put((op, raw_map.get(op.uid)))
            batch.clear()

        producer: threading.Thread | None = None
        if phase1_ops:
            producer = threading.Thread(target=_producer, daemon=True)
            producer.start()

        # All index writes for one folder share a single transaction.
        total_ops = len(plan.ops)
        completed = 0
        with self._index.connection():
            # Phase 1: drain the queue; the producer owns `session`.
            if phase1_ops:
                while (item := q.get()) is not None:
                    op, raw = item
                    try:
                        _t = time.perf_counter()
                        self._execute_one(
                            op, raw, account=account,
                            folder_name=folder_name, folder_ref=folder_ref,
                            session=session, mirror=mirror,
                            counters=counters,
                        )
                        if isinstance(op, FetchNewOp) and raw:
                            ingest_ns.append(
                                int((time.perf_counter() - _t) * 1_000_000_000)
                            )
                    except Exception:
                        logger.exception(
                            "%s failed for %s/%s — skipping",
                            type(op).__name__, account.name, folder_name,
                        )
                    completed += 1
                    if progress is not None:
                        progress(ProgressInfo(
                            f"{account.name}/{folder_name}: "
                            f"{completed}/{total_ops}",
                            current=completed,
                            total=total_ops,
                        ))
            if producer is not None:
                producer.join()

            # Phase 2: IMAP-command ops, serial on the main thread.
            for op in phase2_ops:
                try:
                    self._execute_one(
                        op, None, account=account,
                        folder_name=folder_name, folder_ref=folder_ref,
                        session=session, mirror=mirror,
                        counters=counters,
                    )
                except Exception:
                    logger.exception(
                        "%s failed for %s/%s — skipping",
                        type(op).__name__, account.name, folder_name,
                    )
                completed += 1
                if progress is not None:
                    progress(ProgressInfo(
                        f"{account.name}/{folder_name}: "
                        f"{completed}/{total_ops}",
                        current=completed,
                        total=total_ops,
                    ))

            # Update the watermark after all ops for this folder.  Brand-
            # new folders (is_new=True) haven't been scanned on the server
            # yet — their uid_validity/highest_uid are placeholders, so
            # skip recording.  The next sync does the real scan.
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

        # Wait for any async mirror writes to finish before moving on.
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
            moved_on_server=counters["moved_on_server"],
            moved_to_server=counters["moved_to_server"],
            appended_to_server=counters["appended_to_server"],
            linked_local=counters["linked_local"],
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
        """Execute one sync operation.

        *raw* contains pre-fetched message bytes for ``FetchNewOp``;
        ``None`` for all other op types.
        """
        now = datetime.now(tz=UTC)

        match op:
            case FetchNewOp() if raw:
                self._ingest_raw(
                    account=account, folder_ref=folder_ref,
                    mirror=mirror, uid=op.uid,
                    message_id=op.message_id,
                    server_flags=op.server_flags,
                    extra_imap_flags=op.extra_imap_flags, raw=raw,
                )
                counters["fetched"] += 1

            case ServerDeleteOp():
                self._handle_server_deletion(
                    account=account,
                    folder_name=folder_name,
                    message_id=op.message_id,
                )
                counters["deleted_on_server"] += 1

            case ServerMoveOp():
                self._handle_server_move(
                    account=account,
                    folder_name=folder_name,
                    message_id=op.message_id,
                    new_folder=op.new_folder,
                )
                counters["moved_on_server"] += 1

            case PullFlagsOp():
                row = self._index.get_message(message_ref=op.message_ref)
                if row is not None:
                    self._index.upsert_message(
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
                    folder_name, op.uid,
                    op.new_flags, op.extra_imap_flags,
                )
                row = self._index.get_message(message_ref=op.message_ref)
                if row is not None:
                    self._index.upsert_message(
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
                        folder_name, op.uid,
                        op.merged_flags, op.extra_imap_flags,
                    )
                row = self._index.get_message(message_ref=op.message_ref)
                if row is not None:
                    self._index.upsert_message(
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
                    op, account=account, folder_name=folder_name,
                    session=session,
                )

            case PushDeleteOp():
                if op.server_uid is not None:
                    session.mark_deleted(folder_name, op.server_uid)
                    session.expunge(folder_name)
                else:
                    logger.info(
                        "Message %r already gone — purging local copy",
                        op.message_ref.rfc5322_id,
                    )
                self._index.delete_message(message_ref=op.message_ref)
                mirror.delete_message(
                    folder=FolderRef(
                        account_name=account.name, folder_name=folder_name,
                    ),
                    storage_key=op.storage_key,
                )

            case PushMoveOp():
                session.move_message(
                    folder_name, op.uid, op.target_folder,
                )
                counters["moved_to_server"] += 1
                logger.info(
                    "Pushed local move: %s/%s UID %d -> %s",
                    account.name, folder_name, op.uid, op.target_folder,
                )

            case PushAppendOp():
                self._execute_push_append(
                    op, account=account, folder_name=folder_name,
                    session=session, mirror=mirror,
                )
                counters["appended_to_server"] += 1

            case LinkLocalOp():
                row = self._index.get_message(message_ref=op.message_ref)
                if row is not None:
                    self._index.upsert_message(
                        message=dataclasses.replace(
                            row,
                            uid=op.uid,
                            server_flags=op.server_flags,
                            base_flags=op.server_flags,
                            extra_imap_flags=op.extra_imap_flags,
                            synced_at=now,
                        )
                    )
                counters["linked_local"] += 1

            case RestoreOp():
                local_row = self._index.get_message(
                    message_ref=op.message_ref,
                )
                if local_row is not None:
                    self._index.upsert_message(
                        message=dataclasses.replace(
                            local_row, local_status=MessageStatus.ACTIVE,
                        )
                    )
                    logger.info(
                        "Restored message %r in %s/%s",
                        op.message_ref.rfc5322_id,
                        account.name, folder_name,
                    )

    def _execute_reupload(
        self,
        op: ReUploadOp,
        *,
        account: AccountConfig,
        folder_name: str,
        session: ImapClientSession,
    ) -> None:
        """C-1: re-upload a locally-modified message deleted on server."""
        idx_row = self._index.get_message(message_ref=op.message_ref)
        if idx_row is not None:
            mirror_repo = self._mirror_factory(account)
            try:
                raw = mirror_repo.get_message_bytes(
                    folder=FolderRef(
                        account_name=account.name, folder_name=folder_name,
                    ),
                    storage_key=idx_row.storage_key,
                )
            except Exception:
                logger.exception(
                    "Cannot re-upload %r — mirror read failed",
                    op.message_ref.rfc5322_id,
                )
            else:
                session.append_message(
                    folder_name, raw,
                    op.local_flags, op.extra_imap_flags,
                )
                logger.info(
                    "Re-uploaded message %r to %s/%s",
                    op.message_ref.rfc5322_id,
                    account.name, folder_name,
                )
        # Clear the UID — the old UID is gone; the APPEND created a new one
        # which will be picked up on the next sync.
        if idx_row is not None:
            self._index.upsert_message(
                message=dataclasses.replace(
                    idx_row, uid=None, server_flags=frozenset(),
                    extra_imap_flags=frozenset(), synced_at=None,
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
    ) -> None:
        """APPEND a local-only message's mirror bytes to the server.

        Leaves ``uid=NULL`` — the next sync picks up the server's assigned
        UID via :class:`LinkLocalOp`.
        """
        row = self._index.get_message(message_ref=op.message_ref)
        if row is None:
            return
        try:
            raw = mirror.get_message_bytes(
                folder=FolderRef(
                    account_name=account.name, folder_name=folder_name,
                ),
                storage_key=row.storage_key,
            )
        except Exception:
            logger.exception(
                "Cannot APPEND %r — mirror read failed",
                op.message_ref.rfc5322_id,
            )
            return
        session.append_message(
            folder_name, raw, row.local_flags, row.extra_imap_flags,
        )
        logger.info(
            "APPENDed local message %r to %s/%s",
            op.message_ref.rfc5322_id, account.name, folder_name,
        )

    def _run_cleanup(self) -> None:
        """Periodic DB cleanup: stale accounts, expired trash."""
        configured = {a.name for a in self._config.accounts}

        with self._index.connection():
            # Purge all data for accounts no longer in config.
            for name in self._index.list_indexed_accounts():
                if name not in configured:
                    self._index.purge_account(account_name=name)
                    logger.info(
                        "Cleanup: purged stale account %r", name,
                    )

            # Purge expired trash per configured account.
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
                                folder=folder_ref, storage_key=storage_key,
                            )
                        except Exception:
                            logger.debug(
                                "Mirror cleanup failed for %r", storage_key,
                            )
                    logger.info(
                        "Cleanup: purged %d expired trashed"
                        " message(s) for %r",
                        len(purged), account.name,
                    )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _ingest_raw(
        self,
        *,
        account: AccountConfig,
        folder_ref: FolderRef,
        mirror: MirrorRepository,
        uid: int,
        message_id: str,
        server_flags: frozenset[MessageFlag],
        extra_imap_flags: frozenset[str] = frozenset(),
        raw: bytes,
    ) -> None:
        """Ingest pre-fetched raw bytes into mirror and index."""
        if not raw:
            logger.warning(
                "Empty message body for UID %d in %s/%s — skipping",
                uid, account.name, folder_ref.folder_name,
            )
            return

        if not message_id:
            message_id = _synthetic_message_id(
                account_name=account.name,
                folder_name=folder_ref.folder_name,
                uid=uid,
                raw_message=raw,
            )

        try:
            # Use async write if available — the actual write_bytes runs
            # in a thread pool and overlaps with projection + index work.
            store = getattr(mirror, "store_message_async", None)
            if store is not None:
                storage_key = store(folder=folder_ref, raw_message=raw)
            else:
                storage_key = mirror.store_message(
                    folder=folder_ref, raw_message=raw,
                )
        except Exception:
            logger.exception("Failed to store message UID %d to mirror", uid)
            return

        projected = project_rfc822_message(
            message_ref=MessageRef(
                account_name=account.name,
                folder_name=folder_ref.folder_name,
                rfc5322_id=message_id,
            ),
            raw_message=raw,
            storage_key=storage_key,
        )
        # Stamp with server flags and UID (first sync for this message).
        indexed = dataclasses.replace(
            projected,
            uid=uid,
            local_flags=server_flags,
            base_flags=server_flags,
            server_flags=server_flags,
            extra_imap_flags=extra_imap_flags,
            local_status=MessageStatus.ACTIVE,
            synced_at=datetime.now(tz=UTC),
        )
        self._index.upsert_message(message=indexed)

    def _handle_server_deletion(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        message_id: str,
    ) -> None:
        """Move a server-deleted message to local trash (C-1: defer, don't destroy)."""
        ref = MessageRef(
            account_name=account.name,
            folder_name=folder_name,
            rfc5322_id=message_id,
        )
        local_row = self._index.get_message(message_ref=ref)
        if local_row is not None:
            self._index.upsert_message(
                message=dataclasses.replace(
                    local_row,
                    local_status=MessageStatus.TRASHED,
                    trashed_at=datetime.now(tz=UTC),
                    uid=None,
                    server_flags=frozenset(),
                    extra_imap_flags=frozenset(),
                    synced_at=None,
                )
            )
        logger.info(
            "Message %r deleted on server — moved to local trash",
            message_id,
        )

    def _handle_server_move(
        self,
        *,
        account: AccountConfig,
        folder_name: str,
        message_id: str,
        new_folder: str,
    ) -> None:
        """Update the local folder assignment when a message was moved on the server."""
        ref = MessageRef(
            account_name=account.name,
            folder_name=folder_name,
            rfc5322_id=message_id,
        )
        local_row = self._index.get_message(message_ref=ref)
        if local_row is not None:
            new_ref = MessageRef(
                account_name=account.name,
                folder_name=new_folder,
                rfc5322_id=message_id,
            )
            moved = dataclasses.replace(
                local_row,
                message_ref=new_ref,
                uid=None,  # UID is per-folder; new folder will assign one on next sync
                server_flags=frozenset(),
                extra_imap_flags=frozenset(),
                synced_at=None,
            )
            self._index.delete_message(message_ref=local_row.message_ref)
            self._index.upsert_message(message=moved)
        logger.info(
            "Message %r moved on server: %s → %s",
            message_id,
            folder_name,
            new_folder,
        )

    def _select_accounts(
        self, account_name: str | None
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
