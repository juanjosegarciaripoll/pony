"""The mail commands: `pony sync`, `folder`, `message`, `contacts`, ...

Each one works through the mail model and reports what it did; the
parser that reaches them, and the launchers for the two interactive
front ends, are in `pony.cli.program`.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from pony.atomic import write_atomically
from pony.outcome import Outcome

from ..config import ConfigError, load_config
from ..mail.accounts import (
    build_mirror,
    build_mirrors,
    find_account,
    find_imap_account,
)
from ..mail.contact_actions import ImportTally, import_contacts
from ..mail.credentials import build_credentials_provider, encrypt_password
from ..mail.domain import (
    CONFIG_VERSION,
    AccountConfig,
    AnyAccount,
    AppConfig,
    FolderRef,
    IndexedMessage,
    LocalAccountConfig,
    MessageRef,
    MessageStatus,
)
from ..mail.fixture_flow import run_fixture_ingest
from ..mail.imap_client import ImapAuthError, ImapSession
from ..mail.index_store import (
    SqliteIndexRepository,
)
from ..mail.message_renderer import AttachmentInfo  # noqa: TC002
from ..mail.protocols import (
    ImapClientSession,
    MirrorRepository,
)
from ..mail.search_parser import parse_query
from ..mail.storage_indexing import RescanResult, ScanState, rescan_local_account
from ..mail.sync import (
    ImapSyncService,
    MergeFlagsOp,
    ProgressInfo,
    PushDeleteOp,
    PushFlagsOp,
    RestoreOp,
    ServerDeleteOp,
    SyncPlan,
    describe_halt,
    format_plan_detail,
    format_plan_summary,
)
from ..paths import (
    AppPaths,
    sample_config_path,
)
from ..shell import pick_editor
from .program import (
    _prompt,
    _resolve_theme,
    _run_pager,
    _setup_tui_logging,
    require_config,
    try_load_config,
)


def run_sync(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str | None,
    yes: bool,
    dry_run: bool = False,
) -> int:
    """Synchronise one or all accounts with their IMAP servers."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    credentials = build_credentials_provider(config, index)

    def session_factory(acc: AccountConfig, password: str) -> ImapClientSession:
        return ImapSession(
            host=acc.imap_host,
            port=acc.imap_port,
            ssl=acc.imap_ssl,
            username=acc.username,
            password=password,
            connect_timeout=config.imap_connect_timeout_seconds,
        )

    service = ImapSyncService(
        config=config,
        mirror_factory=build_mirror,
        index=index,
        credentials=credentials,
        session_factory=session_factory,
    )

    if account and not any(a.name == account for a in config.accounts):
        raise SystemExit(f"No account named {account!r} in config.")

    def _cli_progress(info: ProgressInfo) -> None:
        # \r returns to column 0; \033[K clears from cursor to end of
        # line — without it, a shorter message leaves the tail of the
        # previous (longer) progress line visible, and the per-folder
        # completion line concatenates onto the running counter.
        erase = "\r\033[K"
        if info.total > 0:
            print(f"{erase}{info.message}", end="", flush=True)
        else:
            print(f"{erase}{info.message}")

    try:
        plan = service.plan(account_name=account, progress=_cli_progress)
    except ImapAuthError as exc:
        raise SystemExit(
            f"Authentication failed for {exc.username}@{exc.host}.\n"
            "Check your password or run "
            "`pony account set-password <name>`."
        ) from exc
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    # Close off the last \r-overwritten progress line and flush so
    # render_sync_plan doesn't land in the middle of the scan counter,
    # and so the terminal state is clean before input() reads from
    # stdin.  On some Windows terminals (MinTTY / git-bash) leaving the
    # cursor mid-line interacts badly with input() echo.
    print()
    sys.stdout.flush()

    if plan.is_empty():
        print("Nothing to sync — already up to date.")
        return 0

    summary = format_plan_summary(plan)
    header = f"Sync plan: {summary}" if summary else "Sync plan"
    print(f"{header}\n{format_plan_detail(plan)}")

    confirm_folders = plan.folders_needing_confirmation()
    if confirm_folders:
        print(
            f"\nWARNING: {describe_halt(plan)}"
            " Review the lines marked [CONFIRM: ...] above."
            ' Answering "y" applies them, those folders included.'
        )

    if dry_run:
        print("\nDry run: nothing was changed.")
        return 0

    if not yes:
        if not sys.stdin.isatty():
            # Some Windows shells (notably MinTTY / git-bash) present
            # Python with a pipe instead of a real console.  input()
            # won't echo keystrokes and Ctrl-C may not propagate, so
            # the prompt would hang indefinitely — refuse up front
            # with an actionable message.
            print(
                "\nCan't prompt for confirmation: stdin is not a TTY.\n"
                "Rerun with --yes to skip the confirmation, or invoke\n"
                "pony from a real console (cmd.exe, PowerShell, Windows\n"
                "Terminal, or MinTTY via `winpty`).",
            )
            return 1
        while True:
            sys.stdout.flush()
            try:
                answer = input("Proceed? [y/N/l] ").strip().lower()
            except EOFError:
                answer = ""
            except KeyboardInterrupt:
                print("\nAborted.")
                return 0
            if answer in ("y", "yes"):
                break
            if answer in ("l", "list"):
                _run_pager(_build_ops_detail_table(plan, index))
                print(f"{header}\n{format_plan_detail(plan)}")
                continue
            print("Aborted.")
            return 0

    # Pass 2: execute
    print()  # newline after any \r progress
    result = service.execute(
        plan, confirmed_folders=confirm_folders, progress=_cli_progress
    )
    print()  # newline after any \r progress

    for account_result in result.accounts:
        active = [f for f in account_result.folders if f.has_changes]
        skipped = account_result.skipped_folders
        if not active and not skipped:
            continue
        total_fetched = sum(f.fetched for f in active)
        total_flag_updates = sum(f.flag_updates_from_server for f in active)
        total_pushes = sum(f.flag_pushes_to_server for f in active)
        total_expunged = sum(f.expunged_on_server for f in active)
        total_moved = sum(f.moved_to_server for f in active)
        total_uploaded = sum(
            f.appended_to_server + f.reuploaded_to_server for f in active
        )
        total_purged = sum(f.purged_local for f in active)
        total_rekeyed = sum(f.rekeyed for f in active)
        skipped_suffix = (
            f", {len(skipped)} skipped ({', '.join(skipped)})" if skipped else ""
        )
        print(
            f"{account_result.account_name}: "
            f"{len(active)} folder(s) with changes{skipped_suffix}, "
            f"{total_fetched} new message(s), "
            f"{total_flag_updates} flag update(s) from server, "
            f"{total_pushes} flag push(es), "
            f"{total_uploaded} upload(s), "
            f"{total_moved} move(s), "
            f"{total_expunged} expunge(s), "
            f"{total_purged} local purge(s)"
            # Only after a UIDVALIDITY change, and then it is the
            # headline: a folder of mail kept without downloading any
            # of it would otherwise be reported as nothing happening.
            + (f", {total_rekeyed} re-keyed" if total_rekeyed else "")
        )
        for f in active:
            print(
                f"  {f.folder_name}: "
                f"scan {_fmt_ms(f.scan_ms)}"
                + (f", fetch {_fmt_ms(f.fetch_ms)}" if f.fetch_ms else "")
                + (f", ingest {_fmt_ms(f.ingest_ms)}" if f.ingest_ms else "")
            )
    expected = (
        1
        if account
        else sum(1 for a in config.accounts if isinstance(a, AccountConfig))
    )
    failed = expected - len(result.accounts)
    if failed:
        print(f"Warning: {failed} account(s) failed to sync — check logs for details.")

    return 0


def _fmt_ms(ms: int) -> str:
    """Format a millisecond duration as a human-readable string."""
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def _rescan_local_with_cli_progress(
    *,
    mirror: MirrorRepository,
    index: SqliteIndexRepository,
    account_name: str,
    scan_state: ScanState | None,
) -> None:
    """Rescan a local account with live feedback on stderr.

    Emits four kinds of lines so the TUI startup is never silent, even
    when the scan is slow (big mbox archives) or finds no changes:

    1. preamble: ``[acc] Scanning local mirror…``
    2. per-folder liveness during the plan phase (``\\r``-overwriting)
    3. if changes were found: an announcement + per-item progress bar
    4. if no changes: ``[acc] Local mirror up to date.``

    When ``scan_state`` is provided, folders whose mtime has not advanced
    since the last scan are skipped — this is the fast path that makes
    startup near-instant on a cold, unchanged archive.
    """
    # 1. preamble — flushed so it shows before the first folder listing.
    print(
        f"[{account_name}] Scanning local mirror…",
        flush=True,
        file=sys.stderr,
    )

    # \r returns to column 0; \033[K clears from cursor to end of line so
    # a shorter follow-up message doesn't leave tail characters behind.
    erase = "\r\033[K"

    walked: list[str] = []

    def _on_folder_scan(folder: str) -> None:
        # 2. liveness while walking each folder's disk listing.
        walked.append(folder)
        print(
            f"{erase}[{account_name}] scanning {folder}…",
            end="",
            flush=True,
            file=sys.stderr,
        )

    def _on_plan(planned: RescanResult) -> None:
        # 3a. announce once, right before the expensive projection loop.
        parts: list[str] = []
        if planned.added:
            parts.append(f"{planned.added} new")
        if planned.removed:
            parts.append(f"{planned.removed} removed")
        print(
            f"{erase}[{account_name}] Local mirror changed — indexing "
            f"{', '.join(parts)} message(s)…",
            file=sys.stderr,
        )

    def _progress(folder: str, current: int, total: int) -> None:
        # 3b. account-wide progress bar.
        end = "\n" if current == total else ""
        print(
            f"{erase}[{account_name}] {folder}: {current}/{total}",
            end=end,
            flush=True,
            file=sys.stderr,
        )

    result = rescan_local_account(
        mirror_repository=mirror,
        index_repository=index,
        account_name=account_name,
        scan_state=scan_state,
        on_folder_scan=_on_folder_scan,
        on_plan=_on_plan,
        progress=_progress,
    )

    if result.added == 0 and result.removed == 0:
        # 4. clear the \r-overwritten scan line and confirm we finished.
        if walked:
            print(
                f"{erase}[{account_name}] Local mirror up to date.",
                file=sys.stderr,
            )
        else:
            print(
                f"{erase}[{account_name}] Local mirror unchanged (all folders cached).",
                file=sys.stderr,
            )


def rescan_local_accounts(
    *,
    paths: AppPaths,
    config: AppConfig,
    index: SqliteIndexRepository,
    mirrors: dict[str, MirrorRepository],
) -> None:
    """Reconcile every local account's index against its mirror.

    A local account has no sync step, so its mirror is the only source
    of truth: a file added between sessions, a tree edited by another
    program, a folder restored from a backup -- all of it reaches the
    index here or not at all.  Both front ends run it before they open,
    because both read the index and neither has another way to notice.
    `pony web` did not, so a local-account user read a stale index for
    as long as the server was up.

    The folder-mtime cache is what keeps it cheap: a folder whose mtime
    has not moved since the last run is skipped, so a cold archive
    costs one `stat` per folder.
    """
    scan_state_path = paths.scan_state_file
    scan_state_by_account = _load_scan_state(scan_state_path)
    for account in config.accounts:
        if not isinstance(account, LocalAccountConfig):
            continue
        _rescan_local_with_cli_progress(
            mirror=mirrors[account.name],
            index=index,
            account_name=account.name,
            scan_state=scan_state_by_account.setdefault(account.name, {}),
        )
    _save_scan_state(scan_state_path, scan_state_by_account)


def _load_scan_state(path: Path) -> dict[str, ScanState]:
    """Read the per-account folder-mtime cache from disk.

    Returns ``{account_name: {folder_name: mtime_ns}}``.  A missing or
    corrupt file produces an empty dict — the rescan falls back to a
    full pass, which self-heals the cache on its next write.
    """
    import json

    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, ScanState] = {}
    for account_name, folders in parsed.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(account_name, str) or not isinstance(folders, dict):
            continue
        state: ScanState = {}
        for folder_name, mtime in folders.items():  # pyright: ignore[reportUnknownVariableType]
            if isinstance(folder_name, str) and isinstance(mtime, int):
                state[folder_name] = mtime
        out[account_name] = state
    return out


def _save_scan_state(path: Path, state: dict[str, ScanState]) -> None:
    """Persist the per-account folder-mtime cache atomically."""
    import json

    payload = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
    write_atomically(path, payload, suffix=".json")


def _build_ops_detail_table(plan: SyncPlan, index: SqliteIndexRepository) -> str:
    """Build a table of non-download operations for the pager detail view.

    Includes: server deletions → trash, server moves, local expunges,
    flag conflicts (merge), and flag pushes.  Downloads are excluded because
    there is nothing locally to show yet.
    """
    # Column widths — computed from data, with minimums.
    # columns: op, account, folder, from, subject
    rows: list[tuple[str, str, str, str, str]] = []

    # Only the rows the listed operations name, looked up by primary
    # key and remembered.  Reading every message in every planned folder
    # to index it by id meant a 100,000-message account was materialised
    # to label a handful of lines in a dry run.
    id_to_row: dict[int, IndexedMessage | None] = {}

    def row_for(ref: MessageRef) -> IndexedMessage | None:
        if ref.id not in id_to_row:
            id_to_row[ref.id] = index.get_message(message_ref=ref)
        return id_to_row[ref.id]

    for acc in plan.accounts:
        for folder_plan in acc.folders:
            for op in folder_plan.ops:
                if isinstance(op, ServerDeleteOp):
                    hit = row_for(op.message_ref)
                    rows.append(
                        (
                            "server→trash",
                            acc.account_name,
                            folder_plan.folder_name,
                            hit.sender if hit else "",
                            hit.subject if hit else f"id={op.message_ref.id}",
                        )
                    )
                elif isinstance(op, PushDeleteOp):
                    hit = row_for(op.message_ref)
                    rows.append(
                        (
                            "expunge",
                            op.message_ref.account_name,
                            op.message_ref.folder_name,
                            hit.sender if hit else "",
                            hit.subject if hit else f"id={op.message_ref.id}",
                        )
                    )
                elif isinstance(op, RestoreOp):
                    hit = row_for(op.message_ref)
                    rows.append(
                        (
                            "restore",
                            op.message_ref.account_name,
                            op.message_ref.folder_name,
                            hit.sender if hit else "",
                            hit.subject if hit else f"id={op.message_ref.id}",
                        )
                    )
                elif isinstance(op, MergeFlagsOp):
                    flag_str = ",".join(sorted(f.value for f in op.merged_flags)) or "—"
                    hit = row_for(op.message_ref)
                    rows.append(
                        (
                            f"merge({flag_str})",
                            op.message_ref.account_name,
                            op.message_ref.folder_name,
                            hit.sender if hit else "",
                            hit.subject if hit else f"id={op.message_ref.id}",
                        )
                    )
                elif isinstance(op, PushFlagsOp):
                    flag_str = ",".join(sorted(f.value for f in op.new_flags)) or "—"
                    hit = row_for(op.message_ref)
                    rows.append(
                        (
                            f"push({flag_str})",
                            op.message_ref.account_name,
                            op.message_ref.folder_name,
                            hit.sender if hit else "",
                            hit.subject if hit else f"id={op.message_ref.id}",
                        )
                    )

    if not rows:
        return "(no deletions, moves, flag conflicts, or expunges planned)"

    # Compute column widths.
    headers = ("Operation", "Account", "Folder", "From", "Subject")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Cap Subject and From so the table fits a typical terminal.
    widths[3] = min(widths[3], 30)  # From
    widths[4] = min(widths[4], 50)  # Subject

    def fmt_row(cells: tuple[str, str, str, str, str]) -> str:
        return "  ".join(
            cell[: widths[i]].ljust(widths[i]) for i, cell in enumerate(cells)
        )

    sep = "  ".join("-" * w for w in widths)
    lines = [fmt_row(headers), sep]
    for row in rows:
        lines.append(fmt_row(row))
    lines.append(f"\n{len(rows)} operation(s) listed.")
    return "\n".join(lines)


def run_fixture_ingest_command(*, paths: AppPaths, config_path: Path | None) -> int:
    """Ingest deterministic fixture rows into the SQLite index."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    created_count = run_fixture_ingest(config=config, paths=paths)
    print(
        "Fixture ingest complete. "
        f"Inserted or refreshed {created_count} fixture messages "
        f"in {paths.index_db_file}."
    )
    return 0


def run_rescan(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str | None,
    force: bool = False,
) -> int:
    """Re-project indexed messages from the local mirror.

    Routes through :func:`storage_indexing.rescan_local_account` so the
    folder-mtime fast path (cold archives skip re-walking entirely) and
    the disk/index reconciliation (orphan rows pruned, externally-added
    files indexed) come along for free.  Preserves sync state — uid,
    flags, status — by merging only projection fields onto the stored
    row.

    Operates on every configured account regardless of type — the
    typical caller is a ``LocalAccountConfig`` (mbox/Maildir archive)
    where projection drift can't be repaired by a normal sync.

    When ``force`` is true, the per-folder mtime cache is bypassed and
    every overlap row is upserted even when the projection is unchanged
    — useful after a projection-logic change that silently corrects
    past data.
    """
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    accounts = list(config.accounts)
    if account:
        accounts = [a for a in accounts if a.name == account]
        if not accounts:
            raise SystemExit(f"No account named {account!r} in config.")

    scan_state_path = paths.scan_state_file
    scan_state_by_account = _load_scan_state(scan_state_path)

    total_scanned = 0
    total_updated = 0
    total_added = 0
    total_removed = 0
    for acc in accounts:
        mirror = build_mirror(acc)
        print(f"Rescanning {acc.name}...")
        if force:
            scan_state_by_account[acc.name] = {}
        state = scan_state_by_account.setdefault(acc.name, {})

        result = rescan_local_account(
            mirror_repository=mirror,
            index_repository=index,
            account_name=acc.name,
            scan_state=state,
            reproject_existing=True,
            force_reproject=force,
            progress=_rescan_progress,
        )
        total_scanned += result.reprojected_scanned
        total_updated += result.reprojected
        total_added += result.added
        total_removed += result.removed

    _save_scan_state(scan_state_path, scan_state_by_account)

    summary_parts = [f"{total_updated}/{total_scanned} message(s) updated"]
    if total_added:
        summary_parts.append(f"{total_added} added")
    if total_removed:
        summary_parts.append(f"{total_removed} removed")
    print("Rescan complete: " + ", ".join(summary_parts) + ".")
    return 0


def run_reindex(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str | None,
) -> int:
    """Rebuild index rows from the mirrors, without asking a server.

    A Maildir filename carries the handle it was fetched with, so a lost
    or deleted index can be rebuilt from the files already on disk
    instead of re-downloading the mailbox.
    """
    from ..mail.storage_indexing import reindex_account_from_mirror

    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    accounts = list(config.accounts)
    if account:
        accounts = [a for a in accounts if a.name == account]
        if not accounts:
            raise SystemExit(f"No account named {account!r} in config.")

    added = with_uid = local_only = states = adopted = conflicts = 0
    for acc in accounts:
        print(f"Reindexing {acc.name} from {acc.mirror.path}...")
        mirror = build_mirror(acc)
        if not mirror.carries_handles:
            # `reindex --help` promises "recovering each message's server
            # handle from its filename", and an mbox key is a position
            # in a file with nowhere to keep one.  Say so: otherwise the
            # command reports a successful rebuild in which every single
            # message came back as local-only, i.e. as an upload.
            print(
                f"  {acc.name} uses an {acc.mirror.format} mirror, whose keys"
                " cannot carry a server handle, so there is none here to"
                " recover: every message is indexed as local-only. Run"
                " 'pony sync --dry-run' before syncing this account.",
            )
        result = reindex_account_from_mirror(
            mirror_repository=mirror,
            index_repository=index,
            account_name=acc.name,
            progress=_rescan_progress,
        )
        added += result.added
        with_uid += result.with_uid
        local_only += result.local_only
        states += result.states
        adopted += result.adopted
        conflicts += result.conflicts

    print(
        f"Reindex complete: {added} row(s) added "
        f"({with_uid} with a server handle, {local_only} local-only), "
        f"{adopted} row(s) given back their handle, "
        f"{states} folder watermark(s) recovered.",
    )
    if conflicts:
        print(
            f"{conflicts} file(s) carry a handle another message in the same"
            " folder already holds, so they were left unindexed rather than"
            " renumbered. They are still on disk; the log names them.",
        )
    if local_only:
        # Not `local_only and with_uid`: the case that most needs
        # saying is the one where *nothing* carried a handle, because
        # then every message in the mirror is an upload candidate --
        # and that is any mirror written before the handle was put in
        # the filename.  Gating on with_uid kept the warning for the
        # mixed case and silenced it for the wholesale one.
        print(
            "Messages indexed without a handle will be uploaded by the next"
            " sync, which is what a local-only message is for. If that is"
            " not what they are, run 'pony sync --dry-run' and look before"
            " proceeding.",
        )
        if not with_uid:
            print(
                "No message carried a handle, so the next sync sees this"
                " whole mirror as local mail. A mirror written before"
                " handles were recorded in the filename looks exactly like"
                " this; 'pony sync --dry-run' says what would happen.",
            )
        # Which command, and why it has to be that one: a folder of
        # local-only rows trips the mass-upload halt, and every
        # unattended path -- background sync, ctrl+g, the browser's
        # Sync button -- skips a halted folder rather than waiving it.
        # Only a `pony sync` that can put the question converges.
        print(
            "Answer the halt with 'pony sync' (or 'pony sync --yes'):"
            " background sync, ctrl+g and the browser's Sync button have"
            " nobody to ask, so they skip a held folder every time.",
        )
    return 0


def _rescan_progress(folder_name: str, done: int, total: int) -> None:
    """Overwrite-in-place progress ticker.

    How often this is called is the engine's business (see
    ``pony.outcome.ProgressGate``); it used to throttle itself here,
    which fixed the flooding for this front end only.
    """
    end = "\n" if done == total else ""
    print(
        f"\r  {folder_name}: {done}/{total}" + " " * 20,
        end=end,
        flush=True,
    )


def _require_imap_account(config: AppConfig, name: str) -> AccountConfig:
    """Return the IMAP account named ``name`` or exit with an error.

    For anything that only reads the local mirror, prefer
    :func:`_require_account` — a local (mbox/Maildir) account is a
    perfectly good source of mail to read.
    """
    acc = find_imap_account(config, name)
    if acc is None:
        raise SystemExit(f"No account named {name!r} in config.")
    return acc


def _require_account(config: AppConfig, name: str) -> AnyAccount:
    """Return the account named ``name`` or exit with an error.

    Accepts local accounts: reading a message needs a mirror, not a
    server.  Commands that genuinely need IMAP fields — credentials,
    sync, folder policy — use :func:`_require_imap_account` instead.
    """
    acc = find_account(config, name)
    if acc is None:
        raise SystemExit(f"No account named {name!r} in config.")
    return acc


def run_folder_list(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str | None,
) -> int:
    """List folders with indexed message counts and sync status."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    accounts: list[AnyAccount] = list(config.accounts)
    if account:
        accounts = [a for a in accounts if a.name == account]
        if not accounts:
            raise SystemExit(f"No account named {account!r} in config.")

    for acc in accounts:
        mirror = build_mirror(acc)
        sync_by_folder = {
            s.folder_name: s
            for s in index.list_folder_sync_states(account_name=acc.name)
        }
        folder_refs = sorted(
            mirror.list_folders(account_name=acc.name),
            key=lambda r: r.folder_name,
        )
        print(f"{acc.name}:")
        if not folder_refs:
            print("  (no folders)")
            continue
        name_width = max(len(r.folder_name) for r in folder_refs)
        for ref in folder_refs:
            count = index.count_folder_messages(folder=ref)
            state = sync_by_folder.get(ref.folder_name)
            if state is not None:
                synced = state.synced_at.strftime("%Y-%m-%d %H:%M")
                sync_suffix = f", last sync {synced}, uid {state.highest_uid}"
            else:
                sync_suffix = ", never synced"
            print(
                f"  {ref.folder_name:<{name_width}}  {count:>6} messages{sync_suffix}"
            )
    return 0


def run_dedup_folder(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str,
    folder: str,
    apply: bool,
) -> int:
    """Find and eliminate duplicate messages in one folder.

    Two messages are duplicates when they share the same ``Message-ID``
    header.  Among each duplicate group the copy with the most informative
    flag set is kept; the rest are marked TRASHED so the next
    ``pony sync`` expunges them from the server via ``PushDeleteOp``.

    Without ``--apply`` this is a dry-run: the plan is printed but
    nothing is written.
    """
    from datetime import UTC, datetime

    # Which rows are duplicates and which copy survives are
    # `mailbox_actions.plan_dedup`'s, as the trashing is
    # `trash_messages`': what is left here is printing the plan and
    # asking for `--apply`.
    from ..mail.mailbox_actions import Mailboxes, plan_dedup, trash_messages

    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    if not any(a.name == account for a in config.accounts):
        raise SystemExit(f"No account named {account!r} in config.")

    folder_ref = FolderRef(account_name=account, folder_name=folder)
    now = datetime.now(tz=UTC)
    with index.connection():
        # Read and write inside the same connection so the snapshot is
        # consistent: a concurrent sync cannot alter flags between the
        # read and the upsert.
        groups = plan_dedup(index.list_folder_messages(folder=folder_ref))
        if not groups:
            print(f"No duplicates found in {account}/{folder}.")
            return 0

        losers = [row for group in groups for row in group.trash]
        for group in groups:
            flags = (
                ",".join(sorted(f.value for f in group.keep.local_flags)) or "(none)"
            )
            trashing = ", ".join(str(row.uid) for row in group.trash)
            print(
                f"  {group.message_id}\n"
                f"    keep uid={group.keep.uid} flags=[{flags}]\n"
                f"    trash uid(s): {trashing}"
            )

        if not apply:
            print(f"\nWould trash {len(losers)} row(s) in {len(groups)} group(s).")
            print("Rerun with --apply to write changes.")
            return 0

        # Through the action, not a second spelling of it: the field set
        # a trashed row carries *is* the sync contract, and this copy
        # overwrote `trashed_at` -- so a row already in the trash had
        # its retention clock reset by a dedup pass, and survived a
        # retention it had already outlived.
        trash_messages(
            Mailboxes(config=config, index=index, mirrors={}),
            losers,
            now=now,
        )

    print(
        f"\nTrashed {len(losers)} row(s) in {len(groups)} group(s)."
        "  Run `pony sync` to propagate deletions to the server."
    )
    return 0


def run_mirror_folder(
    *,
    paths: AppPaths,
    config_path: Path | None,
    src_account: str,
    src_folder: str,
    dst_account: str,
    dst_folder: str,
) -> int:
    """Copy every ACTIVE message from one local folder to another.

    The destination folder is cleared before copying.  New rows are
    written with ``uid=NULL`` so the next ``pony sync`` uploads them to
    the destination account's IMAP server via ``PushAppendOp``.
    """

    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    all_accounts: dict[str, AnyAccount] = {a.name: a for a in config.accounts}
    if src_account not in all_accounts:
        raise SystemExit(f"No account named {src_account!r} in config.")
    if dst_account not in all_accounts:
        raise SystemExit(f"No account named {dst_account!r} in config.")

    src_mirror = build_mirror(all_accounts[src_account])
    dst_mirror = build_mirror(all_accounts[dst_account])

    src_ref = FolderRef(account_name=src_account, folder_name=src_folder)
    dst_ref = FolderRef(account_name=dst_account, folder_name=dst_folder)

    # Collect source messages that exist locally.
    src_messages = [
        m
        for m in index.list_folder_messages(folder=src_ref)
        if m.local_status == MessageStatus.ACTIVE and m.storage_key
    ]
    if not src_messages:
        raise SystemExit(
            f"No locally available messages in {src_account}/{src_folder}.\n"
            "Run `pony sync` first to download the source folder."
        )

    # Check destination for existing content and ask to confirm overwrite.
    dst_messages = [
        m
        for m in index.list_folder_messages(folder=dst_ref)
        if m.local_status != MessageStatus.TRASHED
    ]
    if dst_messages:
        print(
            f"Destination {dst_account}/{dst_folder} already contains "
            f"{len(dst_messages)} message(s)."
        )
        if not sys.stdin.isatty():
            raise SystemExit(
                "Aborted: destination is not empty (non-interactive mode)."
            )
        answer = input("Overwrite? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

        print(f"Clearing {len(dst_messages)} existing message(s)...")
        for msg in dst_messages:
            if msg.storage_key:
                try:
                    dst_mirror.delete_message(
                        folder=dst_ref, storage_key=msg.storage_key
                    )
                except Exception as exc:  # noqa: BLE001
                    logging.warning(
                        "Could not delete mirror file %s: %s", msg.storage_key, exc
                    )
            index.delete_message(message_ref=msg.message_ref)

    dst_mirror.create_folder(account_name=dst_account, folder_name=dst_folder)

    total = len(src_messages)
    print(
        f"Copying {total} message(s) "
        f"from {src_account}/{src_folder} to {dst_account}/{dst_folder}..."
    )

    from ..mail.mailbox_actions import Mailboxes, copy_messages

    def _progress(done: int, of: int) -> None:
        if done == of or done % 20 == 0:
            print(f"\r  {done}/{of}", end="\n" if done == of else "", flush=True)

    # Through the action: copying a message carries the source row's
    # flags forward, gives a same-account copy a synthetic Message-ID so
    # the sync planner cannot mistake it for a move, and flushes the
    # target mirror -- which this command did not, so an mbox
    # destination could be reported copied and left unwritten.
    outcome = copy_messages(
        Mailboxes(
            config=config,
            index=index,
            mirrors={src_account: src_mirror, dst_account: dst_mirror},
        ),
        src_messages,
        src_ref,
        dst_ref,
        progress=_progress,
    )
    for notice in outcome.notices:
        print(f"  {notice.text}")
    if outcome.changed:
        print(f"Run `pony sync` to upload the copied messages to {dst_account}.")
    return 0


def run_message_get(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str,
    folder: str,
    message_id: str,
) -> int:
    """Print metadata for a single indexed message.

    Attachments are listed individually (name, content-type, size) when
    the message body is available in the local mirror; otherwise a
    ``Attach.: yes/no`` summary line is printed.  Call
    ``pony message body`` to read the full text; call
    ``pony message attachment`` to extract one attachment to disk.
    """
    paths.ensure_runtime_dirs()
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    hits = _find_messages_or_fail(
        index,
        account_name=account,
        folder_name=folder,
        message_id=message_id,
    )
    if len(hits) > 1:
        print(
            f"Warning: {len(hits)} rows match Message-ID {message_id!r}; "
            "showing the most recent.  Use 'pony show-id <id>' to "
            "disambiguate by row id:"
        )
        for h in hits:
            print(f"  id={h.message_ref.id}  {h.received_at.isoformat()}  {h.subject}")
        print()
    msg = hits[0]
    flags = ", ".join(sorted(f.value for f in msg.local_flags)) or "(none)"
    print(f"Account:    {msg.message_ref.account_name}")
    print(f"Folder:     {msg.message_ref.folder_name}")
    print(f"Row id:     {msg.message_ref.id}")
    print(f"Message-ID: {msg.message_id}")
    print(f"From:       {msg.sender}")
    print(f"To:         {msg.recipients}")
    if msg.cc:
        print(f"Cc:         {msg.cc}")
    print(f"Subject:    {msg.subject}")
    print(f"Date:       {msg.received_at.isoformat()}")
    print(f"Flags:      {flags}")
    print(f"Status:     {msg.local_status.value}")
    print(f"UID:        {msg.uid if msg.uid is not None else '(unset)'}")
    print(f"Storage:    {msg.storage_key}")

    # Try to list individual attachments by reading the mirror bytes.
    # Fall back to the yes/no summary when the body isn't available
    # locally (e.g. a headers-only row, or an account whose mirror is
    # unreachable).
    from ..mail.message_renderer import fmt_size

    rendered_attachments = _try_render_attachments(
        config_path=config_path,
        account=account,
        folder=folder,
        storage_key=msg.storage_key,
    )
    if rendered_attachments is not None:
        if rendered_attachments:
            print(f"Attach.:    yes ({len(rendered_attachments)})")
            for att in rendered_attachments:
                print(
                    f"  {att.index}. {att.filename} "
                    f"({att.content_type}, {fmt_size(att.size_bytes)})"
                )
        else:
            print("Attach.:    no")
    else:
        print(f"Attach.:    {'yes' if msg.has_attachments else 'no'}")
    return 0


def _find_messages(
    index: SqliteIndexRepository,
    *,
    account_name: str,
    folder_name: str,
    message_id: str,
) -> list[IndexedMessage]:
    """Look up messages by Message-ID.

    Bracket normalisation lives in the index so every front-end gets it.
    """
    return list(
        index.find_messages_by_message_id(
            account_name=account_name,
            folder_name=folder_name,
            message_id=message_id,
        )
    )


def _find_messages_or_fail(
    index: SqliteIndexRepository,
    *,
    account_name: str,
    folder_name: str,
    message_id: str,
) -> list[IndexedMessage]:
    """Like ``_find_messages`` but exit with an error when nothing matches."""
    hits = _find_messages(
        index,
        account_name=account_name,
        folder_name=folder_name,
        message_id=message_id,
    )
    if not hits:
        raise SystemExit(
            f"Message not found in index: {account_name}/{folder_name}/{message_id}"
        )
    return hits


def _try_render_attachments(
    *,
    config_path: Path | None,
    account: str,
    folder: str,
    storage_key: str,
) -> tuple[AttachmentInfo, ...] | None:
    """Load the raw bytes from the mirror and return the attachment list.

    Returns ``None`` when config/mirror/bytes are unavailable — callers
    use that as a signal to fall back to the index's yes/no flag.
    Returns an empty tuple when the message has no attachments.
    """
    from ..mail.message_renderer import render_message

    config = try_load_config(config_path)
    if config is None:
        return None
    acc = find_account(config, account)
    if acc is None:
        return None
    mirror = build_mirror(acc)
    try:
        raw = mirror.get_message_bytes(
            folder=FolderRef(account_name=account, folder_name=folder),
            storage_key=storage_key,
        )
    except (KeyError, FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    return render_message(raw).attachments


def run_message_body(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str,
    folder: str,
    message_id: str,
) -> int:
    """Print the full decoded body of a message from the local mirror."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    from ..mail.message_renderer import fmt_size, render_message

    acc = _require_account(config, account)

    # Users pass an RFC 5322 Message-ID (from search results); the mirror
    # keys off the backend's own storage_key.  Resolve via the index;
    # when a Message-ID has multiple matches, the most recent wins.
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    indexed = _find_messages_or_fail(
        index,
        account_name=account,
        folder_name=folder,
        message_id=message_id,
    )[0]
    mirror = build_mirror(acc)
    try:
        raw = mirror.get_message_bytes(
            folder=FolderRef(account_name=account, folder_name=folder),
            storage_key=indexed.storage_key,
        )
    except (KeyError, FileNotFoundError, OSError) as exc:
        raise SystemExit(
            f"Message body not found in mirror: {account}/{folder}/{message_id}"
        ) from exc
    rendered = render_message(raw)
    print(f"From:    {rendered.from_}")
    print(f"To:      {rendered.to}")
    if rendered.cc:
        print(f"Cc:      {rendered.cc}")
    print(f"Subject: {rendered.subject}")
    print(f"Date:    {rendered.date}")
    print()
    print(rendered.body)
    if rendered.attachments:
        print()
        print("Attachments:")
        for a in rendered.attachments:
            print(
                f"  {a.index}. {a.filename} "
                f"({a.content_type}, {fmt_size(a.size_bytes)})"
            )
    return 0


def run_message_attachment(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str,
    folder: str,
    message_id: str,
    index: int,
    output: str | None,
    to_stdout: bool,
    force: bool,
) -> int:
    """Write one attachment's bytes to a file or to stdout."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    from ..mail.message_renderer import extract_attachment, safe_attachment_filename

    acc = _require_account(config, account)

    idx = SqliteIndexRepository(database_path=paths.index_db_file)
    idx.initialize()
    hits = _find_messages(
        idx,
        account_name=account,
        folder_name=folder,
        message_id=message_id,
    )
    if not hits:
        raise SystemExit(f"Message not found in index: {account}/{folder}/{message_id}")
    indexed = hits[0]
    mirror = build_mirror(acc)
    try:
        raw = mirror.get_message_bytes(
            folder=FolderRef(account_name=account, folder_name=folder),
            storage_key=indexed.storage_key,
        )
    except (KeyError, FileNotFoundError, OSError) as exc:
        raise SystemExit(
            f"Message body not found in mirror: {account}/{folder}/{message_id}"
        ) from exc

    payload = extract_attachment(raw, index)
    if payload is None:
        raise SystemExit(
            f"Attachment {index} not found (run 'pony message get' to list "
            "available attachments)."
        )

    if to_stdout:
        # Write raw bytes through the binary buffer — bypasses text mode
        # encoding that would mangle anything non-ASCII.
        sys.stdout.buffer.write(payload.data)
        return 0

    if output:
        # An explicit -o path is the user's own choice; honour it as given.
        dest = Path(output)
    else:
        # The filename comes from the message's Content-Disposition and
        # is therefore chosen by the sender: "../../.bashrc" would
        # otherwise be written outside the working directory.
        dest = Path.cwd() / safe_attachment_filename(payload.filename)
    if dest.exists() and not force:
        raise SystemExit(
            f"Refusing to overwrite existing file: {dest}\n"
            "Pass --force to overwrite, or -o PATH to write elsewhere."
        )
    dest.write_bytes(payload.data)
    print(f"Wrote {len(payload.data)} bytes to {dest}")
    return 0


def run_message_mime(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str,
    folder: str,
    message_id: str,
) -> int:
    """Dump the raw MIME structure of a message as a tree."""
    import email
    import email.policy
    from email.message import EmailMessage

    paths.ensure_runtime_dirs()
    config = require_config(config_path)

    acc = _require_account(config, account)

    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    indexed = _find_messages_or_fail(
        index,
        account_name=account,
        folder_name=folder,
        message_id=message_id,
    )[0]
    mirror = build_mirror(acc)
    try:
        raw = mirror.get_message_bytes(
            folder=FolderRef(account_name=account, folder_name=folder),
            storage_key=indexed.storage_key,
        )
    except (KeyError, FileNotFoundError, OSError) as exc:
        raise SystemExit(
            f"Message body not found in mirror: {account}/{folder}/{message_id}"
        ) from exc

    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    print(f"Message-ID: {message_id}")
    print(f"Subject:    {msg.get('Subject', '')}")
    print()

    def _dump_part(part: EmailMessage, depth: int) -> None:
        indent = "  " * depth
        ct = part.get_content_type()
        disp = part.get_content_disposition() or ""
        fname = part.get_filename() or ""
        charset = part.get_content_charset() or ""
        cte = part.get("Content-Transfer-Encoding", "")
        cid = part.get("Content-ID", "")

        payload = part.get_payload(decode=True)
        size = f"{len(payload)} B" if isinstance(payload, bytes) else "—"

        line = f"{indent}{ct}"
        if disp:
            line += f"  disposition={disp}"
        if fname:
            line += f"  filename={fname!r}"
        if charset:
            line += f"  charset={charset}"
        if cte:
            line += f"  cte={cte}"
        if cid:
            line += f"  cid={cid}"
        if not part.is_multipart():
            line += f"  size={size}"
        print(line)

        if part.is_multipart():
            for sub in part.get_payload():
                if isinstance(sub, EmailMessage):
                    _dump_part(sub, depth + 1)

    _dump_part(msg, 0)
    return 0


def run_search(*, paths: AppPaths, config_path: Path | None, query: str | None) -> int:
    """Run a real SQLite-backed search query and print results."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    repository = SqliteIndexRepository(database_path=paths.index_db_file)
    repository.initialize()
    requested_query = query or ""
    # Same parser the TUI's "/" search uses, so "from:alice" means the
    # same thing in both places.
    compiled_query = parse_query(requested_query)

    lines: list[str] = [
        "Search results",
        f"Query: {requested_query!r}",
        f"Index DB: {paths.index_db_file}",
    ]
    total_hits = 0
    for account in config.accounts:
        hits = repository.search(query=compiled_query, account_name=account.name)
        total_hits += len(hits)
        lines.append(f"Account {account.name}: {len(hits)} hit(s)")
        for hit in hits[:5]:
            lines.append(
                f" - {hit.message_ref.folder_name}/id={hit.message_ref.id}: "
                f"{hit.subject}",
            )
    lines.append(f"Total hits: {total_hits}")
    print("\n".join(lines))
    return 0


def run_server_summary(
    *, paths: AppPaths, config_path: Path | None, account: str | None
) -> int:
    """Connect to each IMAP server and print folder counts and last-message dates."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    credentials = build_credentials_provider(config, index)

    imap_accounts = [a for a in config.accounts if isinstance(a, AccountConfig)]
    accounts = (
        [a for a in imap_accounts if a.name == account] if account else imap_accounts
    )
    if account and not accounts:
        raise SystemExit(f"No account named {account!r} in config.")

    for acc in accounts:
        print(f"Account: {acc.name}  ({acc.imap_host}:{acc.imap_port})")
        try:
            password = credentials.get_password(account_name=acc.name)
            session = ImapSession(
                host=acc.imap_host,
                port=acc.imap_port,
                ssl=acc.imap_ssl,
                username=acc.username,
                password=password,
                connect_timeout=config.imap_connect_timeout_seconds,
            )
        except (ConfigError, OSError, ConnectionError) as exc:
            print(f"  Could not connect: {exc}")
            continue

        try:
            folders = session.list_folders()
        except OSError as exc:
            print(f"  Could not list folders: {exc}")
            session.logout()
            continue

        col_w = max((len(f) for f in folders), default=20)
        header = (
            f"      {'Folder':<{col_w}}  {'Messages':>9}  {'Unseen':>6}  Last message"
        )
        print(header)
        print("  " + "-" * (len(header) - 2))

        policy = acc.folders
        for folder in folders:
            if not policy.should_sync(folder):
                tag = "[I]"
            elif policy.is_read_only(folder):
                tag = "[R]"
            else:
                tag = "   "

            try:
                messages, unseen = session.get_folder_status(folder)
            except OSError as exc:
                print(f"  {tag} {folder:<{col_w}}  (error: {exc})")
                continue

            if messages > 0:
                try:
                    last = session.fetch_last_message_date(folder) or "—"
                except OSError:
                    last = "—"
            else:
                last = "—"

            unseen_str = str(unseen) if unseen else ""
            print(f"  {tag} {folder:<{col_w}}  {messages:>9}  {unseen_str:>6}  {last}")

        print()
        print("  [I] = ignored (excluded from sync)")
        print("  [R] = read-only (server-to-local only)")

        session.logout()

    return 0


def run_local_summary(
    *, paths: AppPaths, config_path: Path | None, account: str | None
) -> int:
    """Show local mirror contents, index stats, and application file sizes."""
    paths.ensure_runtime_dirs()
    config = try_load_config(config_path)

    # --- application files ---
    print("Pony Express local summary")
    print()
    print("Files")
    _print_path_row("Config", config_path or paths.config_file)
    _print_path_row("Index DB", paths.index_db_file)
    _print_path_row("Calendar dir", paths.calendar_dir, is_dir=True)
    log_files = list(paths.log_dir.glob("*")) if paths.log_dir.exists() else []
    log_suffix = f"  {len(log_files)} file(s)" if log_files else ""
    _print_path_row("Log dir", paths.log_dir, is_dir=True, suffix=log_suffix)

    if config is None:
        print()
        print("(no config loaded — account mirrors not shown)")
        return 0

    accounts = (
        [a for a in config.accounts if a.name == account]
        if account
        else list(config.accounts)
    )
    if account and not accounts:
        raise SystemExit(f"No account named {account!r} in config.")

    for acc in accounts:
        print()
        fmt = acc.mirror.format
        print(f"Account: {acc.name}  ({fmt}: {acc.mirror.path})")

        index_rows = _query_index(paths.index_db_file, acc.name)

        if fmt == "maildir":
            folder_rows = _maildir_folders(acc.mirror.path)
        else:
            folder_rows = _mbox_folders(acc.mirror.path)

        all_folders = sorted(set(list(folder_rows) + list(index_rows)))

        if not all_folders:
            print("  (no folders found)")
            continue

        col_w = max(len(f) for f in all_folders)
        print(f"  {'Folder':<{col_w}}  {'Mirror':>7}  {'Indexed':>7}  Last sync")
        print("  " + "-" * (col_w + 25))

        for folder in all_folders:
            mirror_count = folder_rows.get(folder)
            mirror_str = str(mirror_count) if mirror_count is not None else "—"
            idx = index_rows.get(folder, (0, None))
            idx_count, last_sync = idx
            idx_str = str(idx_count) if idx_count else "—"
            sync_str = last_sync or "—"
            print(f"  {folder:<{col_w}}  {mirror_str:>7}  {idx_str:>7}  {sync_str}")

    return 0


def _print_path_row(
    label: str,
    path: Path,
    *,
    is_dir: bool = False,
    suffix: str = "",
) -> None:
    exists = path.exists()
    if not exists:
        size_str = "missing"
    elif is_dir:
        size_str = _fmt_size(_dir_size(path))
    else:
        size_str = _fmt_size(path.stat().st_size)
    print(f"  {label + ':':<12} {str(path):<60}  {size_str}{suffix}")


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _maildir_folders(root: Path) -> dict[str, int | None]:
    """Return {folder_name: message_count} for a Maildir mirror tree."""
    result: dict[str, int | None] = {}
    if not root.exists():
        return result

    def _count(folder_path: Path) -> int:
        return sum(
            len(list((folder_path / sub).glob("*")))
            for sub in ("cur", "new")
            if (folder_path / sub).is_dir()
        )

    result["INBOX"] = _count(root)
    for entry in sorted(root.glob(".*")):
        if not entry.is_dir():
            continue
        name = entry.name[1:].replace(".", "/")
        if name:
            result[name] = _count(entry)
    return result


def _mbox_folders(root: Path) -> dict[str, int | None]:
    """Return {folder_name: None} for mbox — counting requires parsing."""
    result: dict[str, int | None] = {}
    if not root.exists():
        return result
    for mbox_file in sorted(root.glob("*.mbox")):
        folder_name = mbox_file.stem.replace(".", "/")
        result[folder_name] = None  # size shown via path row; count is expensive
    if not result:
        result["INBOX"] = None
    return result


def _query_index(db_path: Path, account_name: str) -> dict[str, tuple[int, str | None]]:
    """Return {folder_name: (message_count, last_sync_str)} from the index."""
    if not db_path.exists():
        return {}
    result: dict[str, tuple[int, str | None]] = {}
    try:
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            for folder, count in conn.execute(
                "SELECT folder_name, COUNT(*) FROM messages"
                " WHERE account_name = ? GROUP BY folder_name",
                (account_name,),
            ):
                result[folder] = (count, None)
            for folder, synced_at in conn.execute(
                "SELECT folder_name, synced_at FROM folder_sync_state"
                " WHERE account_name = ?",
                (account_name,),
            ):
                count = result.get(folder, (0, None))[0]
                # synced_at is ISO-8601; show date + time only
                display = synced_at[:16] if synced_at else None
                result[folder] = (count, display)
    except sqlite3.Error:
        pass
    return result


def unsafe_to_delete(target: Path, *, data_dir: Path) -> str | None:
    """Reason *target* must not be deleted wholesale, or None.

    ``mirror.path`` is a configured string.  Nothing stopped it naming
    ``/``, the home directory, or a parent of the data directory, and
    ``pony reset --yes`` would then hand that to ``shutil.rmtree``.

    These three shapes are a denylist, which is the wrong shape for a
    deletion guard and is why :func:`not_a_mirror` exists beside it: a
    mirror path is additionally required to look like a mirror.
    """
    resolved = target.resolve()
    if resolved.parent == resolved:
        return "it is a filesystem root"
    if resolved == Path.home().resolve():
        return "it is your home directory"
    data_resolved = data_dir.resolve()
    if data_resolved != resolved and data_resolved.is_relative_to(resolved):
        return "it contains the data directory"
    return None


# What a Maildir mirror root has in it from the moment it is opened:
# `MaildirMirrorRepository.__init__` creates all three.
_MAILDIR_ROOT_DIRS = frozenset({"cur", "new", "tmp"})


def not_a_mirror(path: Path) -> str | None:
    """Reason *path* does not look like a mirror Pony wrote, or None.

    ``unsafe_to_delete`` enumerates bad shapes, and enumerating is the
    wrong way round for something that calls ``shutil.rmtree``: with
    ``data_dir`` outside ``$HOME`` -- which an isolated or scratch
    profile has -- a mistyped ``mirror.path`` of ``/etc``, ``/usr`` or
    ``/home`` passed all three arms, and ``reset --yes`` deleted it
    without a prompt.  So the question is asked positively instead:
    does this directory hold a mirror?

    A Maildir mirror root carries ``cur``, ``new`` and ``tmp``; an mbox
    mirror holds ``*.mbox`` files.  A directory that does not exist, or
    that is empty, is nothing to refuse -- a mirror that has never been
    written looks exactly like that.
    """
    if not path.exists():
        return None
    if not path.is_dir():
        return "it is not a directory"
    entries = list(path.iterdir())
    if not entries:
        return None
    if {entry.name for entry in entries} >= _MAILDIR_ROOT_DIRS:
        return None
    if any(entry.suffix == ".mbox" and entry.is_file() for entry in entries):
        return None
    return (
        "it holds no mirror: a Maildir mirror has cur/, new/ and tmp/ in"
        " it, and an mbox mirror has .mbox files"
    )


def run_reset(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str | None,
    yes: bool,
) -> int:
    """Delete the index database and every remote account's mirror.

    A local account's mirror is kept: there is no server to re-fetch it
    from, so that directory is the only copy of the mail and "delete it
    all and re-sync" means nothing there.  It is listed as kept, and
    ``account`` naming it removes it anyway, because then it was asked
    for by name -- which is also the only way one account's rows and
    mirror go without the rest.  Credentials and other accounts stay
    intact either way.
    """
    if account is not None:
        return _run_reset_account(
            paths=paths, config_path=config_path, account=account, yes=yes
        )

    config = try_load_config(config_path)

    targets: list[Path] = [paths.index_db_file]
    kept_local: list[tuple[str, Path]] = []
    if config is not None:
        for acc in config.accounts:
            if isinstance(acc, LocalAccountConfig):
                # A local account has no server to re-fetch from: its
                # mirror *is* the mail.  "Delete it all and re-sync"
                # means nothing here, so a reset of the whole config
                # leaves it alone.  `--account <name>` still removes it,
                # because then it was asked for by name.
                kept_local.append((acc.name, acc.mirror.path))
                continue
            targets.append(acc.mirror.path)

    refused: list[tuple[Path, str]] = []
    safe_targets: list[Path] = []
    for t in targets:
        reason = unsafe_to_delete(t, data_dir=paths.data_dir)
        if reason is None and t != paths.index_db_file:
            # Every other target is a mirror directory, and a mirror
            # directory has to look like one before it is handed to
            # rmtree.
            reason = not_a_mirror(t)
        if reason is None:
            safe_targets.append(t)
        else:
            refused.append((t, reason))
    if refused:
        print("Refusing to delete:")
        for t, reason in refused:
            print(f"  {t}  -- {reason}")
        print("Fix the mirror.path in your config first.", file=sys.stderr)
        return 2
    targets = safe_targets

    print("The following will be permanently deleted:")
    for t in targets:
        exists = "(exists)" if t.exists() else "(not found)"
        print(f"  {t}  {exists}")
    if kept_local:
        print(
            "\nKept -- a local account has no server to re-fetch from, so its"
            " mirror is the only copy of that mail:"
        )
        for name, path in kept_local:
            print(f"  {name}: {path}")
        print("  (pony reset --account <name> removes one on purpose.)")

    if not yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Reset cancelled.")
            return 0

    for t in targets:
        if not t.exists():
            continue
        kind = "directory" if t.is_dir() else "file"
        print(f"Deleting {kind} {t} ...", end="", flush=True)
        if t.is_dir():
            shutil.rmtree(t)
        else:
            t.unlink()
        print(" done.")

    # The scan cache records an mtime per local folder, and the rescan
    # skips any folder whose mtime has not moved -- so leaving it here
    # left a kept local mirror indexed by nothing and invisible:
    # `pony rescan` reported 0/0 and the TUI opened empty.
    if paths.scan_state_file.exists():
        print(f"Clearing scan cache {paths.scan_state_file} ...", end="", flush=True)
        paths.scan_state_file.unlink()
        print(" done.")

    print("Reset complete. Run 'pony sync' for a clean synchronization.")
    if kept_local:
        # `pony sync` skips local accounts, so the line above is not the
        # whole answer for the mirrors this just kept.
        print(
            "For the kept local account(s), run 'pony rescan' to index the"
            " mail that is already on disk.",
        )
    return 0


def _run_reset_account(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str,
    yes: bool,
) -> int:
    """Scoped reset: one account's index rows + mirror directory only."""
    config = require_config(config_path)
    matches = [acc for acc in config.accounts if acc.name == account]
    if not matches:
        known = ", ".join(acc.name for acc in config.accounts) or "(none)"
        print(
            f"Unknown account: {account!r}. Configured accounts: {known}",
            file=sys.stderr,
        )
        return 2
    acc = matches[0]
    mirror_path = acc.mirror.path
    scan_state_path = paths.scan_state_file

    print(f"The following will be permanently deleted for account {acc.name!r}:")
    mirror_label = "(exists)" if mirror_path.exists() else "(not found)"
    print(f"  Mirror directory: {mirror_path}  {mirror_label}")
    db_label = "(exists)" if paths.index_db_file.exists() else "(not found)"
    print(f"  Index rows in:    {paths.index_db_file}  {db_label}")
    print("Credentials and other accounts are kept.")

    if not yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Reset cancelled.")
            return 0

    reason = unsafe_to_delete(mirror_path, data_dir=paths.data_dir) or not_a_mirror(
        mirror_path
    )
    if reason is not None:
        print(
            f"Refusing to delete {mirror_path} -- {reason}.",
            file=sys.stderr,
        )
        return 2
    if mirror_path.exists():
        print(f"Deleting mirror directory {mirror_path} ...", end="", flush=True)
        shutil.rmtree(mirror_path)
        print(" done.")
    else:
        print(f"Mirror directory {mirror_path} not present; skipping.")

    if paths.index_db_file.exists():
        print(
            f"Purging index rows for {acc.name!r} ...",
            end="",
            flush=True,
        )
        index = SqliteIndexRepository(database_path=paths.index_db_file)
        index.initialize()
        index.purge_account(account_name=acc.name)
        print(" done.")
    else:
        print("Index database not present; skipping row purge.")

    if scan_state_path.exists():
        state = _load_scan_state(scan_state_path)
        if acc.name in state:
            print(
                f"Clearing scan cache entry for {acc.name!r} ...",
                end="",
                flush=True,
            )
            del state[acc.name]
            _save_scan_state(scan_state_path, state)
            print(" done.")

    print(
        f"Reset complete for {acc.name!r}. "
        f"Run 'pony sync' (or re-open the TUI) to rebuild."
    )
    return 0


def run_compose(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str | None,
    to: str = "",
    cc: str = "",
    bcc: str = "",
    subject: str = "",
    body: str = "",
    markdown_mode: bool | None = None,
    theme: str | None = None,
) -> int:
    """Launch the composer directly for writing a new message."""
    _setup_tui_logging(paths)

    config = require_config(config_path)

    if not config.accounts:
        raise SystemExit("No accounts configured.")

    # Composing requires SMTP config.  Both IMAP accounts (which always
    # carry it) and local accounts with an [smtp] block qualify.
    sendable = [a for a in config.accounts if a.can_send]
    if not sendable:
        raise SystemExit(
            "Composing requires an account with SMTP configured. "
            "Add an [smtp] block to the account you want to send from."
        )

    if account:
        matched = [a for a in sendable if a.name == account]
        if not matched:
            all_matched = [a for a in config.accounts if a.name == account]
            if all_matched:
                raise SystemExit(
                    f"Account {account!r} has no SMTP configured "
                    "— add an [smtp] block to use it for sending."
                )
            raise SystemExit(f"No account named {account!r} in config.")
        selected = matched[0]
    else:
        selected = sendable[0]

    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    mirrors = build_mirrors(config)

    effective_theme, err = _resolve_theme(theme, config.theme)
    if err is not None:
        return err

    from ..mail.compose_utils import new_compose_body
    from ..tui import ComposeApp

    # --markdown/--no-markdown override; fall back to account default
    effective_markdown = (
        markdown_mode
        if markdown_mode is not None
        else selected.markdown_compose or config.markdown_compose
    )

    # Apply signature only when no body was explicitly supplied via --body
    effective_body = body if body else new_compose_body(selected.signature)

    app = ComposeApp(
        config=config,
        account=selected,
        index=index,
        mirrors=mirrors,
        contacts=index,
        credentials=build_credentials_provider(config, index),
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=effective_body,
        markdown_mode=effective_markdown,
        theme_name=effective_theme,
    )
    app.run()
    return 0


def run_account_test(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account_name: str,
) -> int:
    """Test IMAP connection and authentication for one account."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    credentials = build_credentials_provider(config, index)

    account = find_imap_account(config, account_name)
    if account is None:
        print(
            f"Account {account_name!r} not found in config.",
            file=sys.stderr,
        )
        return 1

    print(f"Testing {account_name}…")
    print(
        f"  IMAP: {account.imap_host}:{account.imap_port}"
        f" (SSL={'yes' if account.imap_ssl else 'no'})"
    )
    print(f"  User: {account.username}")

    try:
        password = credentials.get_password(account_name=account_name)
    except ConfigError as exc:
        print(f"  Password: FAILED — {exc}", file=sys.stderr)
        return 1
    print("  Password: OK (retrieved)")

    try:
        session = ImapSession(
            host=account.imap_host,
            port=account.imap_port,
            ssl=account.imap_ssl,
            username=account.username,
            password=password,
            connect_timeout=config.imap_connect_timeout_seconds,
        )
    except ImapAuthError:
        print("  Login: FAILED — authentication rejected", file=sys.stderr)
        print(
            f"\n  Check your password or run "
            f"`pony account set-password {account_name}`.",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"  Connection: FAILED — {exc}", file=sys.stderr)
        return 1

    folders = session.list_folders()
    session.logout()
    print("  Login: OK")
    print(f"  Folders: {len(folders)} found")
    for f in folders:
        print(f"    {f}")
    return 0


def run_account_add(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account_name: str | None,
) -> int:
    """Interactive wizard to add an account to the config file."""
    if not sys.stdin.isatty():
        # Non-interactive: fall back to printing a template.
        config_file = config_path or paths.config_file
        name = account_name or "my-account"
        print(
            f"# Add this block to {config_file}\n"
            f"# (The file itself must start with"
            f" `config_version = {CONFIG_VERSION}`.)\n"
            f"[[accounts]]\n"
            f'name             = "{name}"\n'
            f'email_address    = "you@example.com"\n'
            f'imap_host        = "imap.example.com"\n'
            f'username         = "you@example.com"\n'
            f'credentials_source = "plaintext"\n'
            f'password         = "your-password"\n'
            f"\n"
            f"[accounts.smtp]\n"
            f'host = "smtp.example.com"\n'
            f"\n"
            f"[accounts.mirror]\n"
            f'path   = "mirrors/{name}"\n'
            f'format = "maildir"\n'
        )
        return 0
    return run_account_add_interactive(
        paths=paths,
        config_path=config_path,
    )


def run_config_show(*, paths: AppPaths, config_path: Path | None) -> int:
    """Print the config file contents to stdout."""
    config_file = config_path or paths.config_file
    if not config_file.exists():
        print(f"No config file at {config_file}", file=sys.stderr)
        return 1
    print(config_file.read_text(encoding="utf-8"), end="")
    return 0


def starter_config(paths: AppPaths) -> str:
    """The least a configuration can say and still load.

    The fallback this replaces was two comment lines, which the loader
    refuses three times over -- no `config_version`, no `data_dir`, no
    `profile` -- so `pony config edit` created a file `pony doctor` then
    called broken. The version is interpolated rather than typed, as
    everywhere else that states one, and `data_dir` is this run's own
    resolved directory, which is platform-correct where the sample's
    `~/.local/share/pony` is not.
    """
    return (
        f"config_version = {CONFIG_VERSION}\n"
        f'data_dir = "{paths.data_dir.as_posix()}"\n'
        'profile = "default"\n'
        "accounts = []\n"
        "\n"
        "# `pony account add <name>` prints an account block to paste here.\n"
        "# `pony doctor` says what a run will use, and what is missing.\n"
    )


def run_config_edit(*, paths: AppPaths, config_path: Path | None) -> int:
    """Open the config file in the user's editor."""

    config_file = config_path or paths.config_file
    if not config_file.exists():
        paths.ensure_runtime_dirs()
        # The annotated sample where there is one -- a source checkout,
        # a PyInstaller bundle -- and a loadable minimum where there is
        # not, which is every ordinary wheel.
        sample = sample_config_path()
        if sample is not None:
            shutil.copy(sample, config_file)
            print(f"Created {config_file} from the sample config.")
        else:
            write_atomically(
                config_file, starter_config(paths).encode("utf-8"), suffix=".toml"
            )
            print(f"Created {config_file}.")

    # `pony.shell.pick_editor` is the one answer to "which editor": this
    # had its own, which preferred $EDITOR over $VISUAL where the
    # calendar's preferred $VISUAL (the POSIX convention), so the two
    # `config edit` commands could open different editors on one
    # machine. It also passed the variable as a single argv element, so
    # EDITOR="code --wait" tried to run a file by that name.
    editor = pick_editor()

    print(f"Opening {config_file} in {' '.join(editor)}…")
    return subprocess.run(
        [*editor, str(config_file)],
        check=False,  # noqa: S603
    ).returncode


def run_account_add_interactive(*, paths: AppPaths, config_path: Path | None) -> int:
    """Interactive wizard that prompts for account details and appends to config."""
    import getpass

    config_file = config_path or paths.config_file
    paths.ensure_runtime_dirs()

    print("Pony Express — Add Account\n")

    name = _prompt("Account name (e.g. Personal, Work): ")

    # Check for duplicate account names in the existing config.
    if config_file.exists():
        try:
            existing = load_config(config_file)
            dupes = [a for a in existing.accounts if a.name == name]
            if dupes:
                answer = (
                    input(f"Account {name!r} already exists. Replace it? [y/N] ")
                    .strip()
                    .lower()
                )
                if answer not in ("y", "yes"):
                    print("Cancelled.")
                    return 0
                # Remove the old account block by rewriting the config.
                _remove_account_from_config(config_file, name)
        except ConfigError:
            pass  # Config is broken — let the wizard fix it by appending.
    email = _prompt("Email address: ")
    imap_host = _prompt("IMAP server: ", default=_guess_imap_host(email))
    imap_ssl = _prompt("IMAP SSL/TLS (yes/no) [yes]: ", default="yes")
    imap_ssl_bool = imap_ssl.lower() in ("yes", "y", "true", "1", "")
    imap_port_default = "993" if imap_ssl_bool else "143"
    imap_port = _prompt(f"IMAP port [{imap_port_default}]: ", default=imap_port_default)
    smtp_host = _prompt("SMTP server: ", default=_guess_smtp_host(email))
    smtp_ssl = _prompt("SMTP SSL/TLS (yes/no) [yes]: ", default="yes")
    smtp_ssl_bool = smtp_ssl.lower() in ("yes", "y", "true", "1", "")
    smtp_port_default = "465" if smtp_ssl_bool else "587"
    smtp_port = _prompt(f"SMTP port [{smtp_port_default}]: ", default=smtp_port_default)
    username = _prompt("Username: ", default=email)

    print("\nCredentials source:")
    print("  1. plaintext  — password stored in config file")
    print("  2. encrypted  — password encrypted in SQLite")
    print("  3. command    — external command provides password")
    print("  4. env        — read from environment variable")
    cred_choice = _prompt("Choice [1]: ", default="1")
    cred_map = {"1": "plaintext", "2": "encrypted", "3": "command", "4": "env"}
    cred_source = cred_map.get(cred_choice, "plaintext")

    password_line = ""
    if cred_source == "plaintext":
        pw = getpass.getpass("Password: ")
        password_line = f'password         = "{pw}"\n'
    elif cred_source == "encrypted":
        pass  # Password is prompted after the config is saved.

    mirror_format = _prompt(
        "Mirror format (maildir/mbox) [maildir]: ", default="maildir"
    )
    if mirror_format not in ("maildir", "mbox"):
        mirror_format = "maildir"

    imap_ssl_str = "true" if imap_ssl_bool else "false"
    smtp_ssl_str = "true" if smtp_ssl_bool else "false"
    mirror_rel = f"mirrors/{name.lower().replace(' ', '-')}"
    mirror_abs = (paths.data_dir / mirror_rel).resolve()
    block = (
        f"\n[[accounts]]\n"
        f'name             = "{name}"\n'
        f'email_address    = "{email}"\n'
        f'imap_host        = "{imap_host}"\n'
        f"imap_port        = {imap_port}\n"
        f"imap_ssl         = {imap_ssl_str}\n"
        f'username         = "{username}"\n'
        f'credentials_source = "{cred_source}"\n'
        f"{password_line}"
        f"\n"
        f"[accounts.smtp]\n"
        f'host = "{smtp_host}"\n'
        f"port = {smtp_port}\n"
        f"ssl  = {smtp_ssl_str}\n"
        f"\n"
        f"[accounts.mirror]\n"
        f"# Relative paths resolve under {paths.data_dir}\n"
        f'path   = "{mirror_rel}"\n'
        f'format = "{mirror_format}"\n'
    )

    # Bootstrap or append.  A new file declares the schema version, the
    # data directory and the profile that owns it; `profile` is what
    # stops a second config from running against this index.
    # Single-quoted TOML: a literal string, no escapes.  A Windows path
    # in a double-quoted string makes \U and \t escapes, so the file
    # Pony had just written would not parse.
    header = (
        f"# Pony Express configuration\n"
        f"config_version = {CONFIG_VERSION}\n"
        f"data_dir = '{paths.data_dir}'\n"
        f"profile = 'default'\n"
    )
    if not config_file.exists():
        write_atomically(config_file, (header + block).encode("utf-8"), suffix=".toml")
        print(f"\nCreated {config_file}")
    else:
        with config_file.open("a", encoding="utf-8") as f:
            f.write(block)
        print(f"\nAppended to {config_file}")
    print(f"Mail will be stored in {mirror_abs}")

    # Validate the result.
    try:
        load_config(config_file)
        print("Config validated successfully.")
    except ConfigError as e:
        print(f"Warning: config validation failed: {e}", file=sys.stderr)
        print("You may need to edit the file manually.", file=sys.stderr)

    if cred_source == "encrypted":
        pw = getpass.getpass(f"Password for {name}: ")
        if pw:
            from ..mail.credentials import encrypt_password

            index = SqliteIndexRepository(database_path=paths.index_db_file)
            index.initialize()
            index.store_credential(
                account_name=name,
                encrypted=encrypt_password(pw),
            )
            print("Password encrypted and stored.")
        else:
            print(
                f"No password entered — run `pony account set-password {name}` later.",
            )

    return 0


def _remove_account_from_config(config_file: Path, name: str) -> None:
    """Remove an account block from a TOML config file by name.

    Reads the file line-by-line and drops the ``[[accounts]]`` block
    whose ``name`` field matches.  This is a text-level operation to
    preserve comments and formatting in the rest of the file.
    """
    lines = config_file.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        # Start of any [[accounts]] block.
        if stripped == "[[accounts]]":
            skip = False  # reset — we'll decide after peeking at name
        # Detect the name field inside an accounts block.
        if stripped.startswith("name") and "=" in stripped:
            # Extract the value: name = "Foo"
            val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if val == name:
                # Drop this entire block: walk back to remove the
                # preceding [[accounts]] line we already appended.
                while out and out[-1].strip() in ("", "[[accounts]]"):
                    out.pop()
                skip = True
                continue
        # Detect the start of a new section (end of the skipped block).
        if skip and stripped.startswith("["):
            skip = False
        if not skip:
            out.append(line)
    write_atomically(config_file, "".join(out).encode("utf-8"), suffix=".toml")


def _guess_imap_host(email: str) -> str:
    """Guess the IMAP host from an email domain."""
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return f"imap.{domain}" if domain else "imap.example.com"


def _guess_smtp_host(email: str) -> str:
    """Guess the SMTP host from an email domain."""
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return f"smtp.{domain}" if domain else "smtp.example.com"


def run_account_set_password(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account_name: str,
) -> int:
    """Encrypt and store the password for an account in the local index."""
    import getpass

    paths.ensure_runtime_dirs()
    config = require_config(config_path)

    account = find_imap_account(config, account_name)
    if account is None:
        print(f"error: account {account_name!r} not found in config", file=sys.stderr)
        return 1
    if account.credentials_source != "encrypted":
        print(
            f"error: account {account_name!r} has credentials_source="
            f"{account.credentials_source!r}, not 'encrypted'",
            file=sys.stderr,
        )
        return 1

    password = getpass.getpass(f"Password for {account_name}: ")
    if not password:
        print("error: password must not be empty", file=sys.stderr)
        return 1

    encrypted = encrypt_password(password)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    index.store_credential(account_name=account_name, encrypted=encrypted)
    print(f"Password stored for account {account_name!r}.")
    return 0


def run_eml_to_pdf(*, files: Sequence[Path], out_dir: Path | None) -> int:
    """Convert each ``.eml`` in *files* to a PDF, with no UI.

    The same rendering the viewer's ++ctrl+p++ uses — the message becomes
    the self-contained HTML of the browser view, which an external
    converter turns into a PDF.  Pony bundles no PDF engine; see
    :mod:`pony.mail.pdf_export` for the ones it will use.

    Output lands beside each input (``report.eml`` → ``report.pdf``) or in
    *out_dir*.  An existing file is never overwritten: the name gains
    ``-1``, ``-2``… exactly as saving an attachment twice does.

    A missing converter stops the run, since no later file could succeed
    either.  Anything wrong with one file — unreadable, or rejected by the
    converter — is reported and the rest still run, which is what makes a
    whole directory worth attempting in one go.  The exit status is 1 if
    any file failed.
    """
    from ..mail.message_renderer import build_browser_html, unique_destination
    from ..mail.pdf_export import NoPdfConverterError, html_to_pdf

    if out_dir is not None:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: could not create {out_dir}: {exc}", file=sys.stderr)
            return 1

    failures = 0
    for path in files:
        if not path.is_file():
            print(f"error: not a readable file: {path}", file=sys.stderr)
            failures += 1
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            print(f"error: could not read {path}: {exc}", file=sys.stderr)
            failures += 1
            continue
        destination = unique_destination(
            out_dir if out_dir is not None else path.parent,
            f"{path.stem}.pdf",
            fallback="message",
        )
        try:
            html_to_pdf(build_browser_html(raw), destination)
        except NoPdfConverterError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
            suffix = f": {detail}" if detail else ""
            print(f"error: converting {path} failed{suffix}", file=sys.stderr)
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001 — one bad file, not the run
            print(f"error: converting {path} failed: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"{path} -> {destination}")
    return 1 if failures else 0


def run_eml_viewer(
    *, path: Path, theme: str | None = None, config_path: Path | None = None
) -> int:
    """Open a single .eml file in the full-screen message viewer."""
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1
    if not path.is_file():
        print(f"error: not a regular file: {path}", file=sys.stderr)
        return 1
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        print(f"error: could not read {path}: {exc}", file=sys.stderr)
        return 1

    effective_theme, err = _resolve_theme(theme, None)
    if err is not None:
        return err

    from ..tui.app import EmlViewerApp

    config = try_load_config(config_path)
    viewers = config.viewers if config is not None else ()
    EmlViewerApp(
        raw_bytes=raw_bytes,
        theme_name=effective_theme,
        viewers=viewers,
        downloads_dir=config.downloads_path if config is not None else None,
    ).run()
    return 0


def run_contacts_browse(*, paths: AppPaths) -> int:
    """Open the interactive contacts browser TUI."""
    from ..tui.app import ContactsApp

    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    ContactsApp(contacts=index).run()
    return 0


def run_contacts_search(
    *,
    paths: AppPaths,
    prefix: str,
    limit: int,
) -> int:
    """Print contacts whose name or address matches *prefix*."""
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    results = index.search_contacts(prefix=prefix, limit=limit)
    if not results:
        print(f"No contacts matching {prefix!r}.")
        return 0
    print(f"Contacts matching {prefix!r} ({len(results)} result(s)):\n")
    for contact in results:
        name = contact.display_name or "(no name)"
        emails = ", ".join(contact.emails) if contact.emails else "(no email)"
        aliases = ""
        if contact.aliases:
            aliases = f"  aka {', '.join(contact.aliases)}"
        print(f"  {name}  <{emails}>  (seen {contact.message_count}x){aliases}")
    return 0


def run_contacts_show(*, paths: AppPaths, email: str) -> int:
    """Display full details for a contact identified by *email*."""
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    contact = index.find_contact_by_email(email_address=email)
    if contact is None:
        print(f"No contact found for {email!r}.")
        return 1
    name = contact.display_name or "(no name)"
    print(f"Name:         {name}")
    if contact.affix:
        print(f"Affix:        {', '.join(contact.affix)}")
    if contact.aliases:
        print(f"Aliases:      {', '.join(contact.aliases)}")
    if contact.organization:
        print(f"Organization: {contact.organization}")
    print(f"Emails:       {', '.join(contact.emails)}")
    print(f"Seen:         {contact.message_count} message(s)")
    if contact.last_seen:
        print(f"Last seen:    {contact.last_seen:%Y-%m-%d %H:%M}")
    if contact.notes:
        print(f"Notes:        {contact.notes}")
    print(f"Created:      {contact.created_at:%Y-%m-%d %H:%M}")
    print(f"Updated:      {contact.updated_at:%Y-%m-%d %H:%M}")
    return 0


def run_contacts_export(
    *,
    paths: AppPaths,
    output_path: str | None,
) -> int:
    """Export all contacts to a BBDB v3 file.

    With no path this writes Pony's own copy, never the file named by
    ``bbdb_path``.  That one belongs to the user's other tools and this
    export cannot represent it in full — it carries no phone numbers,
    postal addresses or xfields beyond ``notes``, and no stable record
    id — so writing over it would destroy data Pony never had.
    """
    from ..mail.bbdb import write_bbdb

    dest = Path(output_path) if output_path else paths.data_dir / "contacts.bbdb"

    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    contacts = index.list_all_contacts()
    write_bbdb(contacts, dest)
    print(f"Exported {len(contacts)} contact(s) to {dest}")
    return 0


def import_bbdb_contacts(
    *,
    index: SqliteIndexRepository,
    bbdb_path: Path,
) -> tuple[ImportTally, Outcome]:
    """Read a BBDB file and import it into the address book.

    Reading the file is this command's; what happens to each record --
    which existing contact it merges into, which merge policy applies,
    and what to say about one an import cannot arbitrate -- is
    `contact_actions.import_contacts`'.
    """
    from ..mail.bbdb import read_bbdb

    return import_contacts(index, read_bbdb(bbdb_path))


def run_contacts_import(
    *,
    paths: AppPaths,
    config_path: Path | None,
    input_path: str | None,
) -> int:
    """Import contacts from a BBDB v3 file."""
    src: Path | None = None
    if input_path:
        src = Path(input_path)
    else:
        config = try_load_config(config_path)
        if config and config.bbdb_path:
            src = config.bbdb_path
    if src is None:
        print(
            "No input path given and bbdb_path is not set in config.\n"
            "Usage: pony contacts import [path]"
        )
        return 1
    if not src.exists():
        print(f"File not found: {src}")
        return 1

    from ..mail.bbdb import BbdbError

    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    try:
        tally, outcome = import_bbdb_contacts(index=index, bbdb_path=src)
    except BbdbError as exc:
        print(f"Could not read {src}: {exc}")
        return 1
    for notice in outcome.notices:
        print(f"  {notice.text}")
    suffix = f", {tally.skipped} skipped" if tally.skipped else ""
    print(f"Imported from {src}: {tally.created} new, {tally.updated} updated{suffix}.")
    return 0
