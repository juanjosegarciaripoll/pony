"""Command-line interface for Pony Express."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pony.paths import expand_path

from ..config import ConfigError, load_config, read_raw_config
from ..mail.accounts import (
    build_mirrors,
)
from ..mail.browser_preview import close_preview
from ..mail.credentials import build_credentials_provider
from ..mail.domain import (
    AppConfig,
)
from ..mail.handoff import activate as activate_handoff
from ..mail.index_store import (
    SchemaMismatchError,
    SqliteIndexRepository,
    load_contacts_for_backup,
)
from ..mail.protocols import (
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from ..mail.sync import (
    MailSyncScheduler,
    build_sync_service,
)
from ..paths import (
    PROFILE_STAMP,
    AppPaths,
    ProfileMismatchError,
    claim_profile,
    resolve,
)
from ..services import CheckStatus, ServiceStatus, build_service_status
from ..version import __version__
from . import calendar as calendar_cli

if TYPE_CHECKING:
    from ..calendar_runtime import CalendarRuntime


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
    parser.add_argument(
        "--theme",
        metavar="THEME",
        help="Textual theme name (overrides config). See --list-themes for names.",
    )
    parser.add_argument(
        "--list-themes",
        action="store_true",
        help="Print available Textual theme names and exit.",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("doctor", help="Inspect local Pony Express setup.")
    subparsers.add_parser(
        "fixture-ingest",
        help="Ingest deterministic local fixture messages into SQLite index.",
    )

    reindex_parser = subparsers.add_parser(
        "reindex",
        help="Rebuild index rows from the mirror on disk, recovering each "
        "message's server handle from its filename (no download).",
    )
    reindex_parser.add_argument(
        "--account",
        help="Only reindex one account.",
    )

    rescan_parser = subparsers.add_parser(
        "rescan",
        help="Re-project indexed messages from the local mirror (refresh cached "
        "fields like body_text without re-downloading).",
    )
    rescan_parser.add_argument(
        "--account",
        help="Only rescan one account (matches both IMAP-backed and "
        "local-mirror accounts).",
    )
    rescan_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-upsert every indexed message even when the projection "
        "appears unchanged.",
    )

    sync_parser = subparsers.add_parser("sync", help="Run mail synchronization.")
    sync_parser.add_argument("account", nargs="?", help="Only sync one account.")
    sync_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and execute immediately.",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and exit without writing anything: the index "
        "and the mirrors are left alone. Planning still connects to the "
        "server and reads from it.",
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
        help="Delete the index and every remote account's mirror for a clean re-sync.",
        description=(
            "Delete the index database and every remote account's mirror, "
            "for a clean re-sync. A local account's mirror is the only copy "
            "of that mail -- there is no server to re-fetch it from -- so it "
            "is kept, and listed as kept."
        ),
    )
    reset_parser.add_argument(
        "--account",
        dest="account",
        help=(
            "Reset only this account: drop its index rows and mirror "
            "directory, local account or not, because it was asked for by "
            "name. Credentials and other accounts are left untouched."
        ),
    )
    reset_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )

    tui_parser = subparsers.add_parser("tui", help="Launch the terminal UI.")
    tui_parser.add_argument("account", nargs="?", help="Focus the given account.")

    web_parser = subparsers.add_parser("web", help="Serve the browser interface.")
    web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Address to bind (default: 127.0.0.1). Pass a tailnet address, "
            "or use 'tailscale serve' to publish the loopback port instead."
        ),
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=8730,
        help="Port to bind (default: 8730).",
    )
    web_parser.add_argument(
        "--no-token",
        action="store_true",
        help=(
            "Serve without the access token. Only sensible when the "
            "address is already restricted to people who may read the mail."
        ),
    )

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
        help="Output file path (default: <data_dir>/contacts.bbdb).",
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
        "folder",
        help="Inspect mail folders.",
    )
    folder_subparsers = folder_parser.add_subparsers(
        dest="folder_command",
        required=True,
    )
    folder_list = folder_subparsers.add_parser(
        "list",
        help="List folders with indexed message counts and sync status.",
    )
    folder_list.add_argument(
        "account",
        nargs="?",
        help="Only list folders for one account.",
    )

    folder_dedup = folder_subparsers.add_parser(
        "dedup",
        help="Find and eliminate duplicate messages in a folder (same Message-ID).",
    )
    folder_dedup.add_argument("account", help="Account name.")
    folder_dedup.add_argument("folder", help="Folder name.")
    folder_dedup.add_argument(
        "--apply",
        action="store_true",
        help="Mark duplicates TRASHED; without this flag the command is a dry-run.",
    )

    folder_mirror = folder_subparsers.add_parser(
        "mirror",
        help="Copy all messages from one folder into another, replacing its contents.",
    )
    folder_mirror.add_argument("src_account", help="Source account name.")
    folder_mirror.add_argument("src_folder", help="Source folder name.")
    folder_mirror.add_argument("dst_account", help="Destination account name.")
    folder_mirror.add_argument("dst_folder", help="Destination folder name.")

    message_parser = subparsers.add_parser(
        "message",
        help="Inspect individual messages.",
    )
    message_subparsers = message_parser.add_subparsers(
        dest="message_command",
        required=True,
    )
    message_get = message_subparsers.add_parser(
        "get",
        help="Print metadata for one message by ID.",
    )
    message_get.add_argument("account")
    message_get.add_argument("folder")
    message_get.add_argument("message_id")
    message_body = message_subparsers.add_parser(
        "body",
        help="Print the full body of one message by ID.",
    )
    message_body.add_argument("account")
    message_body.add_argument("folder")
    message_body.add_argument("message_id")

    message_attachment = message_subparsers.add_parser(
        "attachment",
        help="Extract one attachment from a message by its 1-based index.",
    )
    message_attachment.add_argument("account")
    message_attachment.add_argument("folder")
    message_attachment.add_argument("message_id")
    message_attachment.add_argument(
        "index",
        type=int,
        help="1-based attachment index (see 'message get').",
    )
    message_attachment.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="Write bytes to PATH.  Default: attachment's own filename in cwd.",
    )
    message_attachment.add_argument(
        "--stdout",
        action="store_true",
        help="Write raw bytes to stdout; no file is created.",
    )
    message_attachment.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )

    message_mime = message_subparsers.add_parser(
        "mime",
        help="Dump the raw MIME structure of a message (types, dispositions, sizes).",
    )
    message_mime.add_argument("account")
    message_mime.add_argument("folder")
    message_mime.add_argument("message_id")

    view_parser = subparsers.add_parser(
        "view",
        help="Open an .eml file in the viewer, or convert .eml files to PDF.",
    )
    view_parser.add_argument(
        "file",
        type=Path,
        nargs="+",
        help="Path to an RFC 5322 .eml file. Several are allowed with --pdf.",
    )
    view_parser.add_argument(
        "--pdf",
        action="store_true",
        help="Convert to PDF instead of opening the viewer.",
    )
    view_parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Where --pdf writes. Default: beside each input file.",
    )

    subparsers.add_parser(
        "docs",
        help="Open the Pony Express documentation in a browser.",
    )

    subparsers.add_parser(
        "mcp",
        help="Start the MCP server (bridges to TUI if running, else stdio).",
    )

    # Everything after `calendar` belongs to the calendar's own parser,
    # so it is taken verbatim rather than described twice.  `main` splits
    # The calendar's own commands, in this parser rather than in one of
    # their own: `pony --help` lists both halves, `pony calendar --help`
    # lists the calendar's, and a flag cannot mean two things.
    calendar_cli.add_calendar_parser(subparsers)

    # Recorded so the bare-filename shortcut can tell a subcommand from a
    # path without keeping a second list of command names beside this one.
    parser.set_defaults(_commands=frozenset(subparsers.choices))

    return parser


def _expand_bare_filename(
    argv: Sequence[str] | None, parser: argparse.ArgumentParser
) -> Sequence[str] | None:
    """Rewrite ``pony some-message.eml`` as ``pony view some-message.eml``.

    This is the shape a desktop file association hands over: one
    argument, the path to a message. Handling it by rewriting the
    arguments means the shortcut goes through the ``view`` command
    proper rather than duplicating it.

    Only a leading argument is considered, and only when it names an
    existing file that is not also a subcommand name. Rewriting a later
    argument would be guesswork — ``pony --config pony.toml`` has a
    readable file in it too, and turning that into a request to view the
    config would be worse than the shortcut not firing.

    The previous attempt at this inspected ``parse_known_args``'s
    leftovers, which cannot work: argparse rejects an unrecognised
    subcommand while parsing, so the check was never reached.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    if not tokens:
        return argv
    first = tokens[0]
    commands: frozenset[str] = parser.get_default("_commands") or frozenset()
    if first.startswith("-") or first in commands:
        return argv
    if not Path(first).is_file():
        return argv
    return ["view", *tokens]


# Pony's own options that take a separate value, so the scan below can
# step over the value as well as the option.
_VALUE_OPTIONS = frozenset({"--config", "--theme"})


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    parser = build_parser()
    # `None` means "whatever the shell handed us", which is what the
    # split this replaced resolved it to before parsing.
    expanded = _expand_bare_filename(argv, parser)
    tokens = list(sys.argv[1:] if expanded is None else expanded)
    tokens = calendar_cli.expand_ics_shortcut(tokens)
    args = parser.parse_args(tokens)
    _configure_logging(debug=args.debug)
    paths = resolve(args.config)
    declared_data_dir, profile = _peek_profile(paths.config_file)
    if declared_data_dir is not None:
        paths = resolve(args.config, data_dir=declared_data_dir)
    # Files handed to an external viewer go there too, rather than into
    # the system temporary directory where nothing removed them.
    activate_handoff(paths.data_dir)
    if profile:
        try:
            claim_profile(paths.data_dir, profile)
        except ProfileMismatchError as exc:
            print(
                f"Refusing to run: this data directory belongs to profile"
                f" {exc.found!r}, but the config declares"
                f" {exc.declared!r}.\n"
                f"\n"
                f"  config:   {paths.config_file}\n"
                f"  data dir: {paths.data_dir}\n"
                f"  stamp:    {paths.data_dir / PROFILE_STAMP}\n"
                f"\n"
                f"Point this config at its own data_dir. To share this"
                f" one on purpose, declare profile {exc.found!r} in both"
                f" configs. If the stamp is what is wrong -- a directory"
                f" claimed under a name you did not mean -- delete that"
                f" file and the next run claims it again.",
                file=sys.stderr,
            )
            return 2

    try:
        return _dispatch(args=args, paths=paths, parser=parser)
    except SchemaMismatchError as err:
        return run_schema_reset_prompt(
            paths=paths,
            config_path=args.config,
            error=err,
        )


def _peek_profile(config_path: Path) -> tuple[Path | None, str]:
    """Read ``data_dir`` and ``profile`` without validating the rest.

    Runs before the index is opened, so it cannot wait for a full parse
    -- but it holds `data_dir` to the same shape the loader does, and
    answers "no profile" for anything else, because what follows this
    *writes*: `claim_profile` stamps the directory it is handed.

    A config with a `profile` and no `data_dir` stamped the
    platform-convention directory -- the one a run with no loadable
    config uses -- and then reported itself broken, after which the
    user's real config was refused there. A relative `data_dir` stamped
    and created directories under the current working directory. The
    loader refuses both (`config.py:_require_data_dir`), so nothing may
    be stamped for either.
    """
    try:
        raw = read_raw_config(config_path)
    except (ConfigError, OSError):
        return None, ""
    if not isinstance(raw, dict):
        return None, ""
    data = cast("dict[str, object]", raw)
    declared = data.get("data_dir")
    instance = data.get("profile")
    if not isinstance(declared, str) or not declared.strip():
        return None, ""
    path = expand_path(declared)
    if not path.is_absolute():
        return None, ""
    return path, instance if isinstance(instance, str) else ""


def _dispatch(
    *,
    args: argparse.Namespace,
    paths: AppPaths,
    parser: argparse.ArgumentParser,
) -> int:
    """Route *args* to the matching subcommand handler."""

    from .mail import (
        run_account_add,
        run_account_set_password,
        run_account_test,
        run_compose,
        run_config_edit,
        run_config_show,
        run_contacts_browse,
        run_contacts_export,
        run_contacts_import,
        run_contacts_search,
        run_contacts_show,
        run_dedup_folder,
        run_eml_to_pdf,
        run_eml_viewer,
        run_fixture_ingest_command,
        run_folder_list,
        run_local_summary,
        run_message_attachment,
        run_message_body,
        run_message_get,
        run_message_mime,
        run_mirror_folder,
        run_reindex,
        run_rescan,
        run_reset,
        run_search,
        run_server_summary,
        run_sync,
    )

    if args.list_themes:
        return run_list_themes()

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
            dry_run=args.dry_run,
        )
    if args.command == "fixture-ingest":
        return run_fixture_ingest_command(paths=paths, config_path=args.config)
    if args.command == "reindex":
        return run_reindex(
            paths=paths,
            config_path=args.config,
            account=args.account,
        )
    if args.command == "rescan":
        return run_rescan(
            paths=paths,
            config_path=args.config,
            account=args.account,
            force=args.force,
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
        return run_reset(
            paths=paths,
            config_path=args.config,
            account=args.account,
            yes=args.yes,
        )
    if args.command == "config":
        if args.config_command == "show":
            return run_config_show(paths=paths, config_path=args.config)
        return run_config_edit(paths=paths, config_path=args.config)
    if args.command == "calendar":
        return run_calendar(paths=paths, args=args, config_path=args.config)
    if args.command == "tui":
        return run_tui(
            paths=paths,
            config_path=args.config,
            account=args.account,
            theme=args.theme,
        )
    if args.command == "web":
        return run_web(
            paths=paths,
            config_path=args.config,
            host=args.host,
            port=args.port,
            use_token=not args.no_token,
        )
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
            theme=args.theme,
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
            return run_contacts_export(paths=paths, output_path=args.path)
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
            paths=paths,
            config_path=args.config,
            account=args.account,
        )
    if args.command == "folder" and args.folder_command == "dedup":
        dedup_account = args.account
        assert dedup_account is not None
        return run_dedup_folder(
            paths=paths,
            config_path=args.config,
            account=dedup_account,
            folder=args.folder,
            apply=args.apply,
        )
    if args.command == "folder" and args.folder_command == "mirror":
        return run_mirror_folder(
            paths=paths,
            config_path=args.config,
            src_account=args.src_account,
            src_folder=args.src_folder,
            dst_account=args.dst_account,
            dst_folder=args.dst_folder,
        )

    if args.command == "message":
        # `account` is a required positional for every `message` subcommand,
        # so argparse guarantees it is set here — narrow from `Any | None`.
        account = args.account
        assert account is not None
        if args.message_command == "get":
            return run_message_get(
                paths=paths,
                config_path=args.config,
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
        if args.message_command == "attachment":
            return run_message_attachment(
                paths=paths,
                config_path=args.config,
                account=account,
                folder=args.folder,
                message_id=args.message_id,
                index=args.index,
                output=args.output,
                to_stdout=args.stdout,
                force=args.force,
            )
        if args.message_command == "mime":
            return run_message_mime(
                paths=paths,
                config_path=args.config,
                account=account,
                folder=args.folder,
                message_id=args.message_id,
            )

    if args.command == "view" and args.pdf:
        return run_eml_to_pdf(files=args.file, out_dir=args.out_dir)
    if args.command == "view":
        if len(args.file) > 1:
            parser.error("the viewer opens one file at a time; use --pdf for many")
        return run_eml_viewer(
            path=args.file[0], theme=args.theme, config_path=args.config
        )

    if args.command == "docs":
        return run_docs()

    if args.command == "mcp":
        return run_mcp_server_command(config_path=args.config, paths=paths)

    parser.error("Unhandled command.")
    return 2


def run_schema_reset_prompt(
    *,
    paths: AppPaths,
    config_path: Path | None,
    error: SchemaMismatchError,
) -> int:
    """Warn the user about a schema break and offer automatic recovery.

    The underlying cause is that :meth:`SqliteIndexRepository.initialize`
    refuses to open an out-of-date database.  This handler explains the
    three manual steps required (export contacts, delete index + mirrors,
    resync) and offers to perform the first two automatically.  The
    default answer is ``No`` — nothing is deleted unless the user types
    ``y`` / ``yes``.
    """
    from datetime import UTC, datetime

    from ..mail.bbdb import write_bbdb

    config = try_load_config(config_path)
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = paths.data_dir / f"contacts-backup-{timestamp}.bbdb"

    targets: list[Path] = [error.database_path]
    if config is not None:
        targets.extend(account.mirror.path for account in config.accounts)

    print("=" * 66)
    print(
        "Pony's index database schema has changed "
        f"(found v{error.found}, needs v{error.expected})."
    )
    print()
    print("To move to the new schema, the following three steps are needed:")
    print(f"  1. Export your contacts to {backup_path}")
    print("  2. Delete the index database and all local mirror directories:")
    for t in targets:
        exists = "(exists)" if t.exists() else "(not found)"
        print(f"       {t}  {exists}")
    print("  3. Run `pony sync` to redownload mail from IMAP.")
    print()
    print("Steps 1 and 2 can be performed automatically now; step 3 is yours.")
    print("The backup file lets you re-import contacts afterwards with:")
    print(f"  pony contacts import {backup_path}")
    print("=" * 66)

    answer = input("Proceed with automatic reset? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted. No changes made.")
        return 1

    contacts = load_contacts_for_backup(database_path=error.database_path)
    if contacts:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        write_bbdb(contacts, backup_path)
        print(f"Exported {len(contacts)} contact(s) to {backup_path}")
    else:
        print("No contacts to export.")

    for t in targets:
        if not t.exists():
            continue
        if t.is_dir():
            shutil.rmtree(t)
        else:
            t.unlink()
        print(f"Deleted: {t}")

    print()
    print("Reset complete. Next steps:")
    print("  1. Run `pony sync` to redownload mail.")
    if contacts:
        print(f"  2. Run `pony contacts import {backup_path}` to restore contacts.")
    return 0


def run_docs() -> int:
    """Open the documentation in the default browser."""
    import webbrowser

    from ..paths import bundled_docs_path

    bundled = bundled_docs_path()
    if bundled is not None:
        url = (bundled / "index.html").as_uri()
        print(f"Opening bundled docs: {url}")
    else:
        url = "https://juanjosegarciaripoll.github.io/pony/"
        print(f"Opening online docs: {url}")
    webbrowser.open(url)
    return 0


def run_mcp_server_command(*, config_path: Path | None, paths: AppPaths) -> int:
    """Start the program's MCP server: both halves' tools.

    Bridges to a running application when one is serving; otherwise
    opens its own connections and serves over stdio.
    """
    from ..mcp.server import run_server

    run_server(config_path=config_path, state_file=paths.mcp_state_file)
    return 0


def run_doctor(*, paths: AppPaths, config_path: Path | None) -> int:
    """Print a high-level health summary for the local app state.

    Exits non-zero when a check failed.  It used to return 0 whatever it
    found, so the command the documentation points at for a broken
    config reported the breakage and then told the shell all was well.
    """
    paths.ensure_runtime_dirs()
    config_error: str | None = None
    try:
        config = load_config(config_path)
    except ConfigError as error:
        config, config_error = None, str(error)
    service_status = build_service_status(
        paths=paths,
        config_path=config_path,
        config=config,
        config_error=config_error,
    )
    print(render_doctor_report(service_status))
    if any(check.status is CheckStatus.ERROR for check in service_status.checks):
        return 1
    return 0


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


def run_list_themes() -> int:
    """Print the names of all Textual themes available in this installation."""
    from textual.app import App

    app: App[None] = App()
    for name in sorted(app.available_themes):
        print(name)
    return 0


def _resolve_theme(
    cli_theme: str | None, config_theme: str | None
) -> tuple[str | None, int | None]:
    """Return (effective_theme, None) or (None, error_code) on unknown name."""
    effective = cli_theme if cli_theme is not None else config_theme
    if effective is not None:
        from textual.app import App as _TextualApp

        available = _TextualApp().available_themes
        if effective not in available:
            names = ", ".join(sorted(available))
            print(f"error: unknown theme {effective!r}. Available: {names}")
            return None, 1
    return effective, None


def _install_tui_log_handler(log_file: Path) -> None:
    """Wire up the TUI's rotating log handler.

    imapclient's ``imaplib`` logger emits the full RFC 822 byte stream of
    every FETCH at DEBUG, which can produce single log records of many
    megabytes — the rotating handler then preserves them whole and the
    file becomes unreadable. Pin imapclient to INFO so connect/login/move
    lines stay but per-message protocol traffic does not.
    """
    import logging
    from logging.handlers import RotatingFileHandler

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
    logging.getLogger("imapclient").setLevel(logging.INFO)


def _setup_tui_logging(paths: AppPaths) -> Path:
    """Prepare runtime dirs and attach the TUI log handler; return the log path."""
    paths.ensure_runtime_dirs()
    log_file = paths.log_dir / "pony-tui.log"
    _install_tui_log_handler(log_file)
    return log_file


def run_tui(
    *,
    paths: AppPaths,
    config_path: Path | None,
    account: str | None,  # noqa: ARG001
    theme: str | None = None,
) -> int:
    """Launch the interactive terminal UI."""

    import logging

    from .mail import rescan_local_accounts

    log_file = _setup_tui_logging(paths)
    logging.getLogger("pony").info("TUI starting; log file: %s", log_file)

    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()

    credentials = build_credentials_provider(config, index)

    mirrors = build_mirrors(config)

    rescan_local_accounts(
        paths=paths,
        config=config,
        index=index,
        mirrors=mirrors,
    )

    effective_theme, err = _resolve_theme(theme, config.theme)
    if err is not None:
        return err

    from ..tui import PonyApp

    calendar = _open_calendar_or_warn(paths, config_path)

    app = PonyApp(
        config=config,
        index=index,
        mirrors=mirrors,
        credentials=credentials,
        contacts=index,
        config_path=config_path,
        theme_name=effective_theme,
        ui_state_path=paths.data_dir / "ui_state.json",
        calendar=calendar,
    )
    try:
        app.run()
    finally:
        # Stop answering for whatever `w` last served, rather than
        # leaving a message fetchable on loopback after the app is gone.
        close_preview()
        if calendar is not None:
            calendar.close()
        index.close()
    return 0


def run_web(
    *,
    paths: AppPaths,
    config_path: Path | None,
    host: str,
    port: int,
    use_token: bool,
) -> int:
    """Serve the browser interface.

    The same stores the TUI opens, behind HTTP instead of a terminal,
    and the same operations: triage, compose and send, the calendar, the
    contacts, and syncing.  The server keeps one sync slot of its own so
    two tabs cannot sync at once; whether a terminal is open elsewhere
    is the user's business, as it is for `pony sync`.

    With background sync enabled in the configuration, the periodic
    thread runs here too, so a phone sees new mail without anyone
    pressing a key.

    Bound to loopback by default.  Published through a tailnet (either
    by binding the tailnet address or by pointing ``tailscale serve`` at
    the loopback port), the device is already authenticated, so
    ``--no-token`` is reasonable there; anywhere else the token is what
    stands between the port and the mail.
    """

    import secrets

    from .mail import rescan_local_accounts

    try:
        import uvicorn

        from ..web import WebServices, build_web_app
    except ImportError:
        print(
            "error: the browser interface needs its extra. "
            "Install it with: pip install 'pony[web]'",
        )
        return 1

    config = require_config(config_path)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    credentials = build_credentials_provider(config, index)
    mirrors = build_mirrors(config)
    # The same reconciliation the terminal does at startup, for the same
    # reason: a local account's mirror is the only source of truth, and
    # the server would otherwise serve whatever the index last knew.
    rescan_local_accounts(
        paths=paths,
        config=config,
        index=index,
        mirrors=mirrors,
    )
    calendar = _open_calendar_or_warn(paths, config_path)
    scheduler = _web_mail_scheduler(
        config=config,
        index=index,
        mirrors=mirrors,
        credentials=credentials,
    )

    token = secrets.token_urlsafe(24) if use_token else None
    server = uvicorn.Server(
        uvicorn.Config(
            build_web_app(
                services := WebServices(
                    config=config,
                    index=index,
                    mirrors=mirrors,
                    contacts=index,
                    calendar=calendar,
                    credentials=credentials,
                    scheduler=scheduler,
                    token=token,
                ),
            ),
            host=host,
            port=port,
            log_level="warning",
        ),
    )
    # `Q` in the browser stops the server, as it stops the reader.
    # Setting the flag rather than signalling lets uvicorn finish the
    # request that asked -- the page needs the answer before the socket
    # closes under it.
    services.shutdown.stop = lambda: setattr(server, "should_exit", True)
    shown_host = f"[{host}]" if ":" in host else host
    suffix = f"/?token={token}" if token is not None else "/"
    print(f"Pony Express on http://{shown_host}:{port}{suffix}")
    if token is not None:
        # Said once, because the cookie it sets lasts a year: a phone
        # needs the full URL only the first time.
        print("The link carries the access token; it is remembered per browser.")
    try:
        server.run()
    finally:
        if scheduler is not None:
            scheduler.stop()
        if calendar is not None:
            calendar.close()
        # The index commits in bounded batches, so a run that ends
        # mid-batch has work to flush.  Closing here rather than
        # leaving it to __del__, which runs at an unspecified moment
        # during interpreter shutdown and swallows what it cannot do.
        index.close()
    return 0


def _web_mail_scheduler(
    *,
    config: AppConfig,
    index: IndexRepository,
    mirrors: dict[str, MirrorRepository],
    credentials: CredentialsProvider,
) -> MailSyncScheduler | None:
    """The periodic mail sync for the web server, if it is enabled.

    Config-gated exactly as the terminal's is: with background sync off
    there is no thread, and the key still works.  The gate the manual
    sync claims is the scheduler's own, so the two cannot overlap.
    """
    if not config.background_sync_enabled:
        return None
    from pony.scheduler import PeriodicSync

    service = build_sync_service(
        config=config,
        index=index,
        mirrors=mirrors,
        credentials=credentials,
    )
    scheduler: MailSyncScheduler = PeriodicSync(
        name="mail-sync",
        # A mail sync has no cancellation point, so the keyword is
        # swallowed here rather than threaded into the engine.
        runner=lambda **_kwargs: service.sync(),
        interval=config.background_sync_interval_seconds,
    )
    scheduler.start()
    return scheduler


def _open_calendar_or_warn(
    paths: AppPaths, config_path: Path | None
) -> CalendarRuntime | None:
    """Open the calendar for the TUI, or log why it stays closed.

    A mistake in `[calendar]` must not stand between the user and their
    mail, so it is reported to the log rather than raised: the mail
    client opens and F2 says the calendar is unavailable.  Every other
    calendar entry point — `pony calendar ...` — reports it to the
    terminal instead.
    """
    import logging

    from ..calendar_runtime import calendar_paths, open_calendar_runtime

    try:
        return open_calendar_runtime(calendar_paths(paths), config_path)
    except ConfigError as error:
        logging.getLogger("pony").warning("calendar disabled: %s", error)
        return None


def run_calendar(
    *, paths: AppPaths, args: argparse.Namespace, config_path: Path | None
) -> int:
    """Run the calendar command this invocation named.

    The parsing is the program's, one parser for both halves; what is
    left here is the wiring: the configuration comes from Pony's file,
    where the calendar's settings live under `[calendar]`, and the
    stores come from the one function that opens them.
    """
    from datetime import UTC, datetime

    from ..calendar_runtime import (
        CalendarNotConfiguredError,
        calendar_paths,
        hosted_calendar_document,
        open_calendar_runtime,
    )
    from . import calendar as calendar_cli

    layout = calendar_paths(paths)

    def _context() -> calendar_cli.CliContext:
        runtime = open_calendar_runtime(
            layout,
            config_path,
            interactive_authorizer=calendar_cli.default_cli_authorizer,
        )
        if runtime is None:
            raise CalendarNotConfiguredError(
                "no calendar configured: add a [calendar] table to "
                "config.toml (see config-sample.toml)."
            )
        return calendar_cli.CliContext(
            config=runtime.config,
            mirror=runtime.mirror,
            index=runtime.index,
            creds=runtime.credentials,
            paths=runtime.paths,
            stdout=sys.stdout,
            stderr=sys.stderr,
            now=datetime.now(UTC),
        )

    try:
        return calendar_cli.run(
            args,
            paths=layout,
            document=hosted_calendar_document(
                config_path or resolve(config_path).config_file
            ),
            context_factory=_context,
            # Where a running application publishes its MCP endpoint:
            # `pony calendar reset` asks it whether one is holding the
            # index it is about to delete.
            mcp_endpoint_file=paths.mcp_state_file,
        )
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _prompt(message: str, default: str = "") -> str:
    """Prompt the user with an optional default value."""
    if default:
        result = input(f"{message}[{default}] ").strip()
        return result if result else default
    return input(message).strip()


def try_load_config(config_path: Path | None) -> AppConfig | None:
    """Load config if present, otherwise return ``None``."""
    try:
        return load_config(config_path)
    except ConfigError:
        return None


def require_config(config_path: Path | None) -> AppConfig:
    """Load config, offering interactive recovery on failure."""

    from .mail import run_account_add_interactive, run_config_edit

    try:
        return load_config(config_path)
    except ConfigError as error:
        paths = resolve(config_path)
        config_file = paths.config_file
        if not sys.stdin.isatty():
            raise SystemExit(f"Configuration error: {error}") from error

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
        f"  Log dir:      {p.log_dir}",
        f"  Index DB:     {p.index_db_file}",
        f"  Calendar dir: {p.calendar_dir}",
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
