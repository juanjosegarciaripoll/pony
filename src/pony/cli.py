"""Command-line interface for Pony Express."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigError, load_config
from .credentials import build_credentials_provider, encrypt_password
from .domain import (
    AccountConfig,
    AnyAccount,
    AppConfig,
    Contact,
    FolderRef,
    IndexedMessage,
    SearchQuery,
)
from .fixture_flow import run_fixture_ingest
from .imap_client import ImapAuthError, ImapSession
from .index_store import SqliteIndexRepository
from .message_projection import project_rfc822_message
from .paths import AppPaths
from .protocols import ImapClientSession, MirrorRepository
from .services import CheckStatus, ServiceStatus, build_service_status
from .storage import MaildirMirrorRepository, MboxMirrorRepository
from .sync import (
    FetchNewOp,
    ImapSyncService,
    MergeFlagsOp,
    ProgressInfo,
    PullFlagsOp,
    PushDeleteOp,
    PushFlagsOp,
    RestoreOp,
    ServerDeleteOp,
    ServerMoveOp,
    SyncPlan,
)
from .version import __version__


def _configure_logging(*, debug: bool) -> None:
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level CLI parser."""
    parser = argparse.ArgumentParser(
        prog="pony",
        description="Pony Express mail user agent",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to a Pony Express TOML configuration file.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging to stderr.",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("doctor", help="Inspect local Pony Express setup.")
    subparsers.add_parser(
        "fixture-ingest",
        help="Ingest deterministic local fixture messages into SQLite index.",
    )

    rescan_parser = subparsers.add_parser(
        "rescan",
        help="Re-project indexed messages from the local mirror (refresh cached "
        "fields like body_preview without re-downloading).",
    )
    rescan_parser.add_argument(
        "account", nargs="?", help="Only rescan one account.",
    )

    sync_parser = subparsers.add_parser("sync", help="Run mail synchronization.")
    sync_parser.add_argument("account", nargs="?", help="Only sync one account.")
    sync_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and execute immediately.",
    )

    search_parser = subparsers.add_parser("search", help="Search indexed mail.")
    search_parser.add_argument("query", nargs="?", help="Search query string.")

    summary_parser = subparsers.add_parser(
        "server-summary", help="List remote folders with message counts and dates."
    )
    summary_parser.add_argument("account", nargs="?", help="Only show one account.")

    local_summary_parser = subparsers.add_parser(
        "local-summary",
        help="Show local mirrors, index, and config file status.",
    )
    local_summary_parser.add_argument(
        "account", nargs="?", help="Only show one account."
    )

    reset_parser = subparsers.add_parser(
        "reset",
        help="Delete the index database and all local mirrors for a clean re-sync.",
    )
    reset_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )

    tui_parser = subparsers.add_parser("tui", help="Launch the terminal UI.")
    tui_parser.add_argument("account", nargs="?", help="Focus the given account.")

    compose_parser = subparsers.add_parser(
        "compose", help="Open the composer directly to write a new message."
    )
    compose_parser.add_argument(
        "--account",
        dest="account",
        help="Account to compose from (default: first account in config).",
    )
    compose_parser.add_argument("--to", default="", help="Pre-fill the To field.")
    compose_parser.add_argument("--cc", default="", help="Pre-fill the Cc field.")
    compose_parser.add_argument("--bcc", default="", help="Pre-fill the Bcc field.")
    compose_parser.add_argument(
        "--subject", default="", help="Pre-fill the Subject field."
    )
    compose_parser.add_argument("--body", default="", help="Pre-fill the message body.")
    md_group = compose_parser.add_mutually_exclusive_group()
    md_group.add_argument(
        "--markdown",
        dest="markdown_mode",
        action="store_true",
        default=None,
        help="Enable Markdown composition mode.",
    )
    md_group.add_argument(
        "--no-markdown",
        dest="markdown_mode",
        action="store_false",
        help="Disable Markdown composition mode.",
    )

    config_parser = subparsers.add_parser(
        "config", help="Open the config file in your editor."
    )
    config_subparsers = config_parser.add_subparsers(
        dest="config_command",
    )
    config_subparsers.add_parser(
        "edit", help="Open the config file in $EDITOR (default)."
    )
    config_subparsers.add_parser("show", help="Print the config file to stdout.")

    account_parser = subparsers.add_parser("account", help="Manage accounts.")
    account_subparsers = account_parser.add_subparsers(
        dest="account_command",
        required=True,
    )
    account_add = account_subparsers.add_parser(
        "add",
        help="Describe how to add an account configuration.",
    )
    account_add.add_argument("name", nargs="?", help="Optional account name.")
    account_test = account_subparsers.add_parser(
        "test",
        help="Test IMAP connection and authentication for an account.",
    )
    account_test.add_argument("name", help="Account name to test.")
    account_set_password = account_subparsers.add_parser(
        "set-password",
        help="Encrypt and store the password for an account.",
    )
    account_set_password.add_argument("name", help="Account name.")

    contacts_parser = subparsers.add_parser(
        "contacts", help="Browse and manage the contacts store."
    )
    contacts_subparsers = contacts_parser.add_subparsers(
        dest="contacts_command",
    )
    contacts_subparsers.add_parser(
        "browse", help="Open the interactive contacts browser (default)."
    )
    contacts_search = contacts_subparsers.add_parser(
        "search", help="Search contacts by name or address prefix."
    )
    contacts_search.add_argument("prefix", help="Prefix to search for.")
    contacts_search.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of results (default: 20).",
    )
    contacts_show = contacts_subparsers.add_parser(
        "show", help="Show full details for a contact by email address."
    )
    contacts_show.add_argument("email", help="Email address to look up.")
    contacts_export = contacts_subparsers.add_parser(
        "export",
        help="Export contacts to a BBDB v3 file.",
    )
    contacts_export.add_argument(
        "path",
        nargs="?",
        help="Output file path (default: bbdb_path from config).",
    )
    contacts_import = contacts_subparsers.add_parser(
        "import",
        help="Import contacts from a BBDB v3 file.",
    )
    contacts_import.add_argument(
        "path",
        nargs="?",
        help="Input file path (default: bbdb_path from config).",
    )

    folder_parser = subparsers.add_parser(
        "folder", help="Inspect mail folders.",
    )
    folder_subparsers = folder_parser.add_subparsers(
        dest="folder_command", required=True,
    )
    folder_list = folder_subparsers.add_parser(
        "list",
        help="List folders with indexed message counts and sync status.",
    )
    folder_list.add_argument(
        "account", nargs="?", help="Only list folders for one account.",
    )

    message_parser = subparsers.add_parser(
        "message", help="Inspect individual messages.",
    )
    message_subparsers = message_parser.add_subparsers(
        dest="message_command", required=True,
    )
    message_get = message_subparsers.add_parser(
        "get", help="Print metadata for one message by ID.",
    )
    message_get.add_argument("account")
    message_get.add_argument("folder")
    message_get.add_argument("message_id")
    message_body = message_subparsers.add_parser(
        "body", help="Print the full body of one message by ID.",
    )
    message_body.add_argument("account")
    message_body.add_argument("folder")
    message_body.add_argument("message_id")

    subparsers.add_parser(
        "docs",
        help="Open the Pony Express documentation in a browser.",
    )

    mcp_parser = subparsers.add_parser(
        "mcp-server",
        help="Start an MCP server exposing read-only mail tools.",
    )
    mcp_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for HTTP mode (default: 127.0.0.1).",
    )
    mcp_parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="Port for Streamable HTTP transport. Omit to use stdio.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(debug=args.debug)
    paths = AppPaths.default()

    if args.command is None:
        args.command = "tui"
        args.account = None

    if args.command == "doctor":
        return run_doctor(paths=paths, config_path=args.config)
    if args.command == "sync":
        return run_sync(
            paths=paths,
            config_path=args.config,
            account=args.account,
            yes=args.yes,
        )
    if args.command == "fixture-ingest":
        return run_fixture_ingest_command(paths=paths, config_path=args.config)
    if args.command == "rescan":
        return run_rescan(
            paths=paths, config_path=args.config, account=args.account,
        )
    if args.command == "search":
        return run_search(paths=paths, config_path=args.config, query=args.query)
    if args.command == "server-summary":
        return run_server_summary(
            paths=paths, config_path=args.config, account=args.account
        )
    if args.command == "local-summary":
        return run_local_summary(
            paths=paths, config_path=args.config, account=args.account
        )
    if args.command == "reset":
        return run_reset(paths=paths, config_path=args.config, yes=args.yes)
    if args.command == "config":
        if args.config_command == "show":
            return run_config_show(paths=paths, config_path=args.config)
        return run_config_edit(paths=paths, config_path=args.config)
    if args.command == "tui":
        return run_tui(paths=paths, config_path=args.config, account=args.account)
    if args.command == "compose":
        return run_compose(
            paths=paths,
            config_path=args.config,
            account=args.account,
            to=args.to,
            cc=args.cc,
            bcc=args.bcc,
            subject=args.subject,
            body=args.body,
            markdown_mode=args.markdown_mode,
        )
    if args.command == "account" and args.account_command == "test":
        return run_account_test(
            paths=paths,
            config_path=args.config,
            account_name=args.name,
        )
    if args.command == "account" and args.account_command == "add":
        return run_account_add(
            paths=paths,
            config_path=args.config,
            account_name=args.name,
        )
    if args.command == "account" and args.account_command == "set-password":
        return run_account_set_password(
            paths=paths,
            config_path=args.config,
            account_name=args.name,
        )
    if args.command == "contacts":
        if args.contacts_command == "search":
            return run_contacts_search(
                paths=paths,
                prefix=args.prefix,
                limit=args.limit,
            )
        if args.contacts_command == "show":
            return run_contacts_show(paths=paths, email=args.email)
        if args.contacts_command == "export":
            return run_contacts_export(
                paths=paths,
                config_path=args.config,
                output_path=args.path,
            )
        if args.contacts_command == "import":
            return run_contacts_import(
                paths=paths,
                config_path=args.config,
                input_path=args.path,
            )
        # Default: open the interactive browser.
        return run_contacts_browse(paths=paths)

    if args.command == "folder" and args.folder_command == "list":
        return run_folder_list(
            paths=paths, config_path=args.config, account=args.account,
        )

    if args.command == "message":
        # `account` is a required positional for every `message` subcommand,
        # so argparse guarantees it is set here — narrow from `Any | None`.
        account = args.account
        assert account is not None
        if args.message_command == "get":
            return run_message_get(
                paths=paths,
                account=account,
                folder=args.folder,
                message_id=args.message_id,
            )
        if args.message_command == "body":
            return run_message_body(
                paths=paths,
                config_path=args.config,
                account=account,
                folder=args.folder,
                message_id=args.message_id,
            )

    if args.command == "docs":
        return run_docs()

    if args.command == "mcp-server":
        return run_mcp_server_command(
            config_path=args.config,
            host=args.host,
            port=args.port,
        )

    parser.error("Unhandled command.")
    return 2


def run_docs() -> int:
    """Open the documentation in the default browser."""
    import webbrowser

    from .paths import bundled_docs_path

    bundled = bundled_docs_path()
    if bundled is not None:
        url = (bundled / "index.html").as_uri()
        print(f"Opening bundled docs: {url}")
    else:
        url = "https://juanjosegarciaripoll.github.io/pony/"
        print(f"Opening online docs: {url}")
    webbrowser.open(url)
    return 0


def run_mcp_server_command(
    *,
    config_path: Path | None,
    host: str,
    port: int | None,
) -> int:
    """Start the MCP server (stdio or Streamable HTTP)."""
    from .mcp_server import run_mcp_server

    run_mcp_server(config_path=config_path, host=host, port=port)
    return 0


def run_doctor(*, paths: AppPaths, config_path: Path | None) -> int:
    """Print a high-level health summary for the local app state."""
    paths.ensure_runtime_dirs()
    config = try_load_config(config_path)
    service_status = build_service_status(
        paths=paths, config_path=config_path, config=config
    )
    print(render_doctor_report(service_status))
    return 0


def run_sync(
    *, paths: AppPaths, config_path: Path | None, account: str | None, yes: bool
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
        )

    service = ImapSyncService(
        config=config,
        mirror_factory=_build_mirror,
        index=index,
        credentials=credentials,
        session_factory=session_factory,
    )

    if account and not any(a.name == account for a in config.accounts):
        raise SystemExit(f"No account named {account!r} in config.")

    def _cli_progress(info: ProgressInfo) -> None:
        if info.total > 0:
            print(f"\r{info.message}", end="", flush=True)
        else:
            print(info.message)

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

    if plan.is_empty():
        print("Nothing to sync — already up to date.")
        return 0

    print(render_sync_plan(plan))

    if not yes:
        while True:
            try:
                answer = input("Proceed? [y/N/l] ").strip().lower()
            except EOFError:
                answer = ""
            if answer in ("y", "yes"):
                break
            if answer in ("l", "list"):
                _run_pager(_build_ops_detail_table(plan, index))
                print(render_sync_plan(plan))
                continue
            print("Aborted.")
            return 0

    # Pass 2: execute
    print()  # newline after any \r progress
    result = service.execute(plan, progress=_cli_progress)
    print()  # newline after any \r progress

    for account_result in result.accounts:
        total_fetched = sum(f.fetched for f in account_result.folders)
        total_flag_updates = sum(
            f.flag_updates_from_server for f in account_result.folders
        )
        total_pushes = sum(f.flag_pushes_to_server for f in account_result.folders)
        skipped = account_result.skipped_folders
        skipped_suffix = (
            f", {len(skipped)} skipped ({', '.join(skipped)})" if skipped else ""
        )
        print(
            f"{account_result.account_name}: "
            f"{len(account_result.folders)} folder(s) synced{skipped_suffix}, "
            f"{total_fetched} new message(s), "
            f"{total_flag_updates} flag update(s) from server, "
            f"{total_pushes} flag push(es) to server"
        )
        for f in account_result.folders:
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

    # Auto-sync contacts with BBDB when configured.
    if config.bbdb_path:
        _bbdb_auto_sync(config.bbdb_path, index, paths)

    return 0


def _fmt_ms(ms: int) -> str:
    """Format a millisecond duration as a human-readable string."""
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def _bbdb_auto_sync(
    bbdb_path: Path,
    index: SqliteIndexRepository,
    paths: AppPaths,
) -> None:
    """Import from BBDB if newer than last import, then export a copy.

    The user's BBDB file (``bbdb_path``) is **read-only** — Pony never
    overwrites it, because the roundtrip is lossy (phone numbers,
    addresses, and xfields beyond ``notes`` are not preserved).

    Instead, Pony exports its full contacts database to a separate
    ``contacts.bbdb`` file inside the data directory.  This file can
    be loaded into Emacs as a secondary BBDB source if desired.

    The last-import timestamp is stored in a ``.bbdb_imported`` marker
    file next to the index database.
    """
    from .bbdb import write_bbdb

    marker = paths.data_dir / ".bbdb_imported"

    # Import if BBDB file is newer than our last import.
    if bbdb_path.exists():
        bbdb_mtime = bbdb_path.stat().st_mtime
        last_imported = 0.0
        if marker.exists():
            with contextlib.suppress(ValueError, OSError):
                last_imported = float(marker.read_text().strip())
        if bbdb_mtime > last_imported:
            created, updated = import_bbdb_contacts(
                index=index,
                bbdb_path=bbdb_path,
            )
            if created or updated:
                print(f"BBDB import: {created} new, {updated} updated from {bbdb_path}")
            # Record the source file's mtime so we don't re-import
            # until Emacs edits it again.
            with contextlib.suppress(OSError):
                marker.write_text(str(bbdb_mtime))

    # Export a Pony-managed copy (never overwrites the user's file).
    export_path = paths.data_dir / "contacts.bbdb"
    contacts = index.list_all_contacts()
    write_bbdb(contacts, export_path)


def render_sync_plan(plan: SyncPlan) -> str:
    """Format a SyncPlan as a human-readable summary."""
    lines: list[str] = ["Sync plan"]
    for acc in plan.accounts:
        skipped_note = (
            f"  ({len(acc.skipped_folders)} folder(s) skipped: "
            f"{', '.join(acc.skipped_folders)})"
            if acc.skipped_folders
            else ""
        )
        lines.append(f"  Account: {acc.account_name}{skipped_note}")
        for folder in acc.folders:
            counts = {
                "fetch": 0,
                "delete": 0,
                "move": 0,
                "pull_flags": 0,
                "push_flags": 0,
                "merge_flags": 0,
                "expunge": 0,
                "restore": 0,
            }
            for op in folder.ops:
                if isinstance(op, FetchNewOp):
                    counts["fetch"] += 1
                elif isinstance(op, ServerDeleteOp):
                    counts["delete"] += 1
                elif isinstance(op, ServerMoveOp):
                    counts["move"] += 1
                elif isinstance(op, PullFlagsOp):
                    counts["pull_flags"] += 1
                elif isinstance(op, PushFlagsOp):
                    counts["push_flags"] += 1
                elif isinstance(op, MergeFlagsOp):
                    counts["merge_flags"] += 1
                elif isinstance(op, PushDeleteOp):
                    counts["expunge"] += 1
                else:
                    counts["restore"] += 1
            parts: list[str] = []
            if counts["fetch"]:
                parts.append(f"{counts['fetch']} new message(s) to download")
            if counts["delete"]:
                parts.append(f"{counts['delete']} message(s) deleted on server → trash")
            if counts["move"]:
                parts.append(f"{counts['move']} message(s) moved on server")
            if counts["pull_flags"]:
                parts.append(f"{counts['pull_flags']} flag update(s) from server")
            if counts["push_flags"]:
                parts.append(f"{counts['push_flags']} flag update(s) to push")
            if counts["merge_flags"]:
                parts.append(f"{counts['merge_flags']} flag conflict(s) to merge")
            if counts["expunge"]:
                parts.append(f"{counts['expunge']} local deletion(s) to expunge")
            if counts["restore"]:
                parts.append(
                    f"{counts['restore']} locally-trashed message(s) to restore"
                    " (read-only folder)"
                )
            summary = ", ".join(parts) if parts else "no changes"
            lines.append(f"    {folder.folder_name}: {summary}")
    return "\n".join(lines)


def _build_ops_detail_table(plan: SyncPlan, index: SqliteIndexRepository) -> str:
    """Build a table of non-download operations for the pager detail view.

    Includes: server deletions → trash, server moves, local expunges,
    flag conflicts (merge), and flag pushes.  Downloads are excluded because
    there is nothing locally to show yet.
    """
    # Column widths — computed from data, with minimums.
    # columns: op, account, folder, from, subject
    rows: list[tuple[str, str, str, str, str]] = []

    for acc in plan.accounts:
        # Build a cache of message_id → IndexedMessage for this account.
        mid_to_row: dict[str, IndexedMessage | None] = {}
        for folder_plan in acc.folders:
            folder_ref = FolderRef(
                account_name=acc.account_name,
                folder_name=folder_plan.folder_name,
            )
            for msg in index.list_folder_messages(folder=folder_ref):
                mid_to_row[msg.message_ref.rfc5322_id] = msg

        for folder_plan in acc.folders:
            for op in folder_plan.ops:
                if isinstance(op, ServerDeleteOp):
                    hit = mid_to_row.get(op.message_id)
                    rows.append(
                        (
                            "server→trash",
                            acc.account_name,
                            folder_plan.folder_name,
                            hit.sender if hit else "",
                            hit.subject if hit else op.message_id,
                        )
                    )
                elif isinstance(op, ServerMoveOp):
                    hit = mid_to_row.get(op.message_id)
                    rows.append(
                        (
                            f"move→{op.new_folder}",
                            acc.account_name,
                            folder_plan.folder_name,
                            hit.sender if hit else "",
                            hit.subject if hit else op.message_id,
                        )
                    )
                elif isinstance(op, PushDeleteOp):
                    hit = mid_to_row.get(op.message_ref.rfc5322_id)
                    rows.append(
                        (
                            "expunge",
                            op.message_ref.account_name,
                            op.message_ref.folder_name,
                            hit.sender if hit else "",
                            hit.subject if hit else op.message_ref.rfc5322_id,
                        )
                    )
                elif isinstance(op, RestoreOp):
                    hit = mid_to_row.get(op.message_ref.rfc5322_id)
                    rows.append(
                        (
                            "restore",
                            op.message_ref.account_name,
                            op.message_ref.folder_name,
                            hit.sender if hit else "",
                            hit.subject if hit else op.message_ref.rfc5322_id,
                        )
                    )
                elif isinstance(op, MergeFlagsOp):
                    flag_str = ",".join(sorted(f.value for f in op.merged_flags)) or "—"
                    rows.append(
                        (
                            f"merge({flag_str})",
                            op.message_ref.account_name,
                            op.message_ref.folder_name,
                            "",
                            op.message_ref.rfc5322_id,
                        )
                    )
                elif isinstance(op, PushFlagsOp):
                    flag_str = ",".join(sorted(f.value for f in op.new_flags)) or "—"
                    rows.append(
                        (
                            f"push({flag_str})",
                            op.message_ref.account_name,
                            op.message_ref.folder_name,
                            "",
                            op.message_ref.rfc5322_id,
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


def _run_pager(text: str) -> None:
    """Display *text* in a pager.

    Tries ``less -FRSX`` first (present on Linux/macOS and Git-for-Windows).
    Falls back to a simple built-in pager that pages by terminal height when
    ``less`` is not available (e.g. bare Windows without Git).
    """
    less = shutil.which("less")
    if less:
        try:
            proc = subprocess.run(
                [less, "-FRSX"],
                input=text.encode(),
                check=False,
            )
            if proc.returncode == 0 or proc.returncode == 1:  # 1 = user quit early
                return
        except OSError:
            pass

    # Built-in fallback: page by terminal height.
    try:
        term_height = shutil.get_terminal_size().lines - 2
    except Exception:  # noqa: BLE001
        term_height = 24

    lines = text.splitlines()
    pos = 0
    while pos < len(lines):
        chunk = lines[pos : pos + term_height]
        print("\n".join(chunk))
        pos += term_height
        if pos >= len(lines):
            break
        try:
            prompt = input("-- more -- [Enter/q] ").strip().lower()
        except EOFError:
            break
        if prompt in ("q", "quit"):
            break
    sys.stdout.flush()


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
    *, paths: AppPaths, config_path: Path | None, account: str | None,
) -> int:
    """Re-project indexed messages from the local mirror.

    Refreshes cached projection fields (sender, recipients, subject,
    body_preview, has_attachments, received_at) without re-downloading
    from IMAP.  Preserves sync state (flags, uid, status).
    """
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    accounts = [a for a in config.accounts if isinstance(a, AccountConfig)]
    if account:
        accounts = [a for a in accounts if a.name == account]
        if not accounts:
            raise SystemExit(f"No account named {account!r} in config.")

    total = 0
    changed = 0
    missing = 0
    for acc in accounts:
        mirror = _build_mirror(acc)
        print(f"Rescanning {acc.name}...")
        for folder in mirror.list_folders(account_name=acc.name):
            messages = index.list_folder_messages(folder=folder)
            folder_total = len(messages)
            folder_changed = 0
            for i, stored in enumerate(messages, start=1):
                total += 1
                try:
                    raw = mirror.get_message_bytes(
                        folder=folder, storage_key=stored.storage_key,
                    )
                except (KeyError, FileNotFoundError):
                    missing += 1
                    _rescan_progress(folder.folder_name, i, folder_total)
                    continue
                fresh = project_rfc822_message(
                    message_ref=stored.message_ref,
                    raw_message=raw,
                    storage_key=stored.storage_key,
                )
                if not _projection_matches(stored, fresh):
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
                    index.upsert_message(message=merged)
                    folder_changed += 1
                _rescan_progress(folder.folder_name, i, folder_total)
            changed += folder_changed
            # Final line overwrites the progress ticker and terminates with \n.
            print(
                f"\r  {folder.folder_name}: "
                f"{folder_changed} updated ({folder_total} scanned)"
                + " " * 20
            )

    print(
        f"Rescan complete: {changed}/{total} message(s) updated"
        + (f", {missing} missing from mirror." if missing else ".")
    )
    return 0


def _rescan_progress(folder_name: str, done: int, total: int) -> None:
    """Overwrite-in-place progress ticker; throttled to avoid stdout flooding."""
    if done != total and done % 20 != 0:
        return
    print(f"\r  {folder_name}: {done}/{total}", end="", flush=True)


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


def _build_mirror(acc: AccountConfig) -> MirrorRepository:
    if acc.mirror.format == "maildir":
        return MaildirMirrorRepository(
            account_name=acc.name, root_dir=acc.mirror.path
        )
    return MboxMirrorRepository(account_name=acc.name, root_dir=acc.mirror.path)


def run_folder_list(
    *, paths: AppPaths, config_path: Path | None, account: str | None,
) -> int:
    """List folders with indexed message counts and sync status."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    accounts = [a for a in config.accounts if isinstance(a, AccountConfig)]
    if account:
        accounts = [a for a in accounts if a.name == account]
        if not accounts:
            raise SystemExit(f"No account named {account!r} in config.")

    for acc in accounts:
        mirror = _build_mirror(acc)
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
                f"  {ref.folder_name:<{name_width}}  "
                f"{count:>6} messages{sync_suffix}"
            )
    return 0


def run_message_get(
    *, paths: AppPaths, account: str, folder: str, message_id: str,
) -> int:
    """Print metadata for a single indexed message."""
    paths.ensure_runtime_dirs()
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    from .domain import MessageRef
    ref = MessageRef(
        account_name=account, folder_name=folder, rfc5322_id=message_id,
    )
    msg = index.get_message(message_ref=ref)
    if msg is None:
        raise SystemExit(
            f"Message not found in index: {account}/{folder}/{message_id}"
        )
    flags = ", ".join(sorted(f.value for f in msg.local_flags)) or "(none)"
    print(f"Account:    {msg.message_ref.account_name}")
    print(f"Folder:     {msg.message_ref.folder_name}")
    print(f"Message-ID: {msg.message_ref.rfc5322_id}")
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
    print(f"Attach.:    {'yes' if msg.has_attachments else 'no'}")
    if msg.body_preview:
        print()
        print("Preview:")
        print(f"  {msg.body_preview}")
    return 0


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
    from .domain import MessageRef
    from .tui.message_renderer import fmt_size, render_message

    acc = next(
        (
            a for a in config.accounts
            if isinstance(a, AccountConfig) and a.name == account
        ),
        None,
    )
    if acc is None:
        raise SystemExit(f"No account named {account!r} in config.")

    # Users pass an RFC 5322 Message-ID (from search results); the mirror
    # keys off the backend's own storage_key.  Resolve one to the other
    # via the index.
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    indexed = index.get_message(
        message_ref=MessageRef(
            account_name=account,
            folder_name=folder,
            rfc5322_id=message_id,
        ),
    )
    if indexed is None:
        raise SystemExit(
            f"Message not found in index: {account}/{folder}/{message_id}"
        )
    mirror = _build_mirror(acc)
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


def run_search(*, paths: AppPaths, config_path: Path | None, query: str | None) -> int:
    """Run a real SQLite-backed search query and print results."""
    paths.ensure_runtime_dirs()
    config = require_config(config_path)
    repository = SqliteIndexRepository(database_path=paths.index_db_file)
    repository.initialize()
    requested_query = query or ""
    compiled_query = SearchQuery(text=requested_query)

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
                f" - {hit.message_ref.folder_name}/"
                f"{hit.message_ref.rfc5322_id}: {hit.subject}",
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
    _print_path_row("State dir", paths.state_dir, is_dir=True)
    _print_path_row("Cache dir", paths.cache_dir, is_dir=True)
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
        pending_counts = _query_pending(paths.index_db_file, acc.name)

        if fmt == "maildir":
            folder_rows = _maildir_folders(acc.mirror.path)
        else:
            folder_rows = _mbox_folders(acc.mirror.path)

        all_folders = sorted(set(list(folder_rows) + list(index_rows)))

        if not all_folders:
            print("  (no folders found)")
            continue

        col_w = max(len(f) for f in all_folders)
        print(
            f"  {'Folder':<{col_w}}  {'Mirror':>7}  {'Indexed':>7}"
            f"  {'Pending':>7}  Last sync"
        )
        print("  " + "-" * (col_w + 34))

        for folder in all_folders:
            mirror_count = folder_rows.get(folder)
            mirror_str = str(mirror_count) if mirror_count is not None else "—"
            idx = index_rows.get(folder, (0, None))
            idx_count, last_sync = idx
            idx_str = str(idx_count) if idx_count else "—"
            pending = pending_counts.get(folder, 0)
            pending_str = str(pending) if pending else ""
            sync_str = last_sync or "—"
            print(
                f"  {folder:<{col_w}}  {mirror_str:>7}  {idx_str:>7}"
                f"  {pending_str:>7}  {sync_str}"
            )

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
        with sqlite3.connect(db_path) as conn:
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


def _query_pending(db_path: Path, account_name: str) -> dict[str, int]:
    """Return {folder_name: pending_op_count} from the index."""
    if not db_path.exists():
        return {}
    result: dict[str, int] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for folder, count in conn.execute(
                "SELECT folder_name, COUNT(*) FROM pending_operations"
                " WHERE account_name = ? GROUP BY folder_name",
                (account_name,),
            ):
                result[folder] = count
    except sqlite3.Error:
        pass
    return result


def run_reset(*, paths: AppPaths, config_path: Path | None, yes: bool) -> int:
    """Delete the index database and all local mirror directories."""
    config = try_load_config(config_path)

    targets: list[Path] = [paths.index_db_file]
    if config is not None:
        for account in config.accounts:
            targets.append(account.mirror.path)

    print("The following will be permanently deleted:")
    for t in targets:
        exists = "(exists)" if t.exists() else "(not found)"
        print(f"  {t}  {exists}")

    if not yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Reset cancelled.")
            return 0

    for t in targets:
        if not t.exists():
            continue
        if t.is_dir():
            shutil.rmtree(t)
        else:
            t.unlink()
        print(f"Deleted: {t}")

    print("Reset complete. Run 'pony sync' for a clean synchronization.")
    return 0


def run_tui(*, paths: AppPaths, config_path: Path | None, account: str | None) -> int:  # noqa: ARG001
    """Launch the interactive terminal UI."""
    import logging
    from logging.handlers import RotatingFileHandler

    paths.ensure_runtime_dirs()

    log_file = paths.log_dir / "pony-tui.log"
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root_logger = logging.getLogger()
    # Remove any StreamHandlers (stderr) so they don't corrupt the TUI display.
    for h in root_logger.handlers[:]:
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler
        ):
            root_logger.removeHandler(h)  # pyright: ignore[reportUnknownArgumentType]
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)
    logging.getLogger("pony").info("TUI starting; log file: %s", log_file)

    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    credentials = build_credentials_provider(config, index)

    def _make_mirror(acc: AnyAccount) -> MirrorRepository:
        if acc.mirror.format == "maildir":
            return MaildirMirrorRepository(
                account_name=acc.name, root_dir=acc.mirror.path
            )
        return MboxMirrorRepository(account_name=acc.name, root_dir=acc.mirror.path)

    mirrors = {acc.name: _make_mirror(acc) for acc in config.accounts}

    # Auto-import BBDB contacts if configured.
    if config.bbdb_path:
        _bbdb_auto_sync(config.bbdb_path, index, paths)

    from .tui import PonyApp

    app = PonyApp(
        config=config,
        index=index,
        mirrors=mirrors,
        credentials=credentials,
        contacts=index,
        config_path=config_path,
    )
    app.run()
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
) -> int:
    """Launch the composer directly for writing a new message."""
    import logging
    from logging.handlers import RotatingFileHandler

    paths.ensure_runtime_dirs()

    log_file = paths.log_dir / "pony-tui.log"
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root_logger = logging.getLogger()
    for h in root_logger.handlers[:]:
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler
        ):
            root_logger.removeHandler(h)  # pyright: ignore[reportUnknownArgumentType]
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)

    config = require_config(config_path)

    if not config.accounts:
        raise SystemExit("No accounts configured.")

    if account:
        matched = [a for a in config.accounts if a.name == account]
        if not matched:
            raise SystemExit(f"No account named {account!r} in config.")
        selected = matched[0]
    else:
        selected = config.accounts[0]

    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    def _make_mirror(acc: AnyAccount) -> MirrorRepository:
        if acc.mirror.format == "maildir":
            return MaildirMirrorRepository(
                account_name=acc.name, root_dir=acc.mirror.path
            )
        return MboxMirrorRepository(account_name=acc.name, root_dir=acc.mirror.path)

    mirrors = {acc.name: _make_mirror(acc) for acc in config.accounts}

    from .tui import ComposeApp
    from .tui.compose_utils import new_compose_body

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
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=effective_body,
        markdown_mode=effective_markdown,
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

    account = next(
        (
            a
            for a in config.accounts
            if isinstance(a, AccountConfig) and a.name == account_name
        ),
        None,
    )
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
            f"[[accounts]]\n"
            f'name             = "{name}"\n'
            f'email_address    = "you@example.com"\n'
            f'imap_host        = "imap.example.com"\n'
            f'smtp_host        = "smtp.example.com"\n'
            f'username         = "you@example.com"\n'
            f'credentials_source = "plaintext"\n'
            f'password         = "your-password"\n'
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


def run_config_edit(*, paths: AppPaths, config_path: Path | None) -> int:
    """Open the config file in the user's editor."""
    import os

    config_file = config_path or paths.config_file
    if not config_file.exists():
        paths.ensure_runtime_dirs()
        # Bootstrap with the sample config.
        sample = Path(__file__).parent.parent.parent / "config-sample.toml"
        if sample.exists():
            shutil.copy(sample, config_file)
            print(f"Created {config_file} from sample config.")
        else:
            config_file.write_text(
                "# Pony Express configuration\n"
                "# Run `pony account add` to add an account.\n",
                encoding="utf-8",
            )
            print(f"Created {config_file}.")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        # Platform defaults.
        if sys.platform == "win32":
            editor = "notepad"
        else:
            editor = "vi"

    print(f"Opening {config_file} in {editor}…")
    return subprocess.run(
        [editor, str(config_file)],
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
        f'smtp_host        = "{smtp_host}"\n'
        f"smtp_port        = {smtp_port}\n"
        f"smtp_ssl         = {smtp_ssl_str}\n"
        f'username         = "{username}"\n'
        f'credentials_source = "{cred_source}"\n'
        f"{password_line}"
        f"\n"
        f"[accounts.mirror]\n"
        f"# Relative paths resolve under {paths.data_dir}\n"
        f'path   = "{mirror_rel}"\n'
        f'format = "{mirror_format}"\n'
    )

    if not config_file.exists():
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            "# Pony Express configuration\n" + block,
            encoding="utf-8",
        )
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
            from .credentials import encrypt_password

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
    config_file.write_text("".join(out), encoding="utf-8")


def _prompt(message: str, default: str = "") -> str:
    """Prompt the user with an optional default value."""
    if default:
        result = input(f"{message}[{default}] ").strip()
        return result if result else default
    return input(message).strip()


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

    account = next(
        (
            a
            for a in config.accounts
            if isinstance(a, AccountConfig) and a.name == account_name
        ),
        None,
    )
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


def run_contacts_browse(*, paths: AppPaths) -> int:
    """Open the interactive contacts browser TUI."""
    from .tui.app import ContactsApp

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
    config_path: Path | None,
    output_path: str | None,
) -> int:
    """Export all contacts to a BBDB v3 file."""
    from .bbdb import write_bbdb

    dest: Path | None = None
    if output_path:
        dest = Path(output_path)
    else:
        config = try_load_config(config_path)
        if config and config.bbdb_path:
            dest = config.bbdb_path
    if dest is None:
        print(
            "No output path given and bbdb_path is not set in config.\n"
            "Usage: pony contacts export [path]"
        )
        return 1

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
) -> tuple[int, int]:
    """Import contacts from a BBDB file, merging with existing records.

    For each BBDB contact:
    - If any of its emails match an existing contact, merge into that
      record (add new emails/aliases, update name/org/notes if richer).
    - Otherwise create a new contact.

    Returns ``(created, updated)`` counts.
    """
    from .bbdb import read_bbdb

    imported = read_bbdb(bbdb_path)
    created = 0
    updated = 0

    with index.connection():
        for contact in imported:
            # Find an existing contact that shares an email.
            existing = None
            for email in contact.emails:
                existing = index.find_contact_by_email(
                    email_address=email,
                )
                if existing is not None:
                    break

            if existing is not None:
                # Merge: combine emails and aliases, prefer richer name.
                merged_emails = set(existing.emails) | set(contact.emails)
                merged_aliases = set(existing.aliases) | set(contact.aliases)
                first = contact.first_name or existing.first_name
                last = contact.last_name or existing.last_name
                org = contact.organization or existing.organization
                notes_parts = [p for p in (existing.notes, contact.notes) if p]
                notes = existing.notes if existing.notes else contact.notes
                if (
                    existing.notes
                    and contact.notes
                    and contact.notes not in existing.notes
                ):
                    notes = "\n".join(notes_parts)
                affix = contact.affix or existing.affix
                index.upsert_contact(
                    contact=Contact(
                        id=existing.id,
                        first_name=first,
                        last_name=last,
                        emails=tuple(sorted(merged_emails)),
                        aliases=tuple(sorted(merged_aliases)),
                        affix=affix,
                        organization=org,
                        notes=notes,
                        message_count=existing.message_count,
                        last_seen=existing.last_seen,
                    ),
                )
                updated += 1
            else:
                index.upsert_contact(contact=contact)
                created += 1

    return created, updated


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

    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    created, updated = import_bbdb_contacts(index=index, bbdb_path=src)
    print(f"Imported from {src}: {created} new, {updated} updated.")
    return 0


def try_load_config(config_path: Path | None) -> AppConfig | None:
    """Load config if present, otherwise return ``None``."""
    try:
        return load_config(config_path)
    except ConfigError:
        return None


def require_config(config_path: Path | None) -> AppConfig:
    """Load config, offering interactive recovery on failure."""
    try:
        return load_config(config_path)
    except ConfigError as error:
        config_file = config_path or AppPaths.default().config_file
        if not sys.stdin.isatty():
            raise SystemExit(f"Configuration error: {error}") from error

        paths = AppPaths.default()

        if not config_file.exists():
            print(f"No config file found at {config_file}.\n")
            answer = input("Would you like to set up an account now? [Y/n] ")
            if (
                answer.strip().lower() in ("", "y", "yes")
                and run_account_add_interactive(
                    paths=paths,
                    config_path=config_path,
                )
                == 0
            ):
                return load_config(config_path)
        else:
            print(f"Configuration error: {error}\n")
            answer = input("Would you like to open the config file to fix it? [Y/n] ")
            if answer.strip().lower() in ("", "y", "yes"):
                run_config_edit(paths=paths, config_path=config_path)
                # Retry after editing.
                try:
                    return load_config(config_path)
                except ConfigError as retry_error:
                    raise SystemExit(
                        f"Configuration still invalid: {retry_error}"
                    ) from retry_error

        raise SystemExit(f"Configuration error: {error}") from error


def render_doctor_report(service_status: ServiceStatus) -> str:
    """Render a structured pass/warn/fail doctor report."""
    p = service_status.paths
    lines: list[str] = [
        "Pony Express doctor",
        "",
        "Paths:",
        f"  Config file:  {p.config_file}",
        f"  Data dir:     {p.data_dir}",
        f"  State dir:    {p.state_dir}",
        f"  Cache dir:    {p.cache_dir}",
        f"  Log dir:      {p.log_dir}",
        f"  Index DB:     {p.index_db_file}",
        "",
        "Checks:",
    ]

    tag_label = {
        CheckStatus.OK: "OK   ",
        CheckStatus.WARN: "WARN ",
        CheckStatus.ERROR: "ERROR",
    }
    for check in service_status.checks:
        tag = tag_label[check.status]
        detail = f": {check.detail}" if check.detail else ""
        lines.append(f"  [{tag}] {check.name}{detail}")

    ok_count = sum(1 for c in service_status.checks if c.status == CheckStatus.OK)
    warn_count = sum(1 for c in service_status.checks if c.status == CheckStatus.WARN)
    err_count = sum(1 for c in service_status.checks if c.status == CheckStatus.ERROR)
    lines.append("")
    if err_count == 0 and warn_count == 0:
        lines.append(f"All {ok_count} checks passed.")
    else:
        parts: list[str] = []
        if err_count:
            parts.append(f"{err_count} error{'s' if err_count != 1 else ''}")
        if warn_count:
            parts.append(f"{warn_count} warning{'s' if warn_count != 1 else ''}")
        lines.append(f"{ok_count} OK, " + ", ".join(parts))

    return "\n".join(lines)
