"""Service health checks for the doctor command."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .config import ConfigError
from .mail.domain import AnyAccount, AppConfig
from .mail.protocols import IndexRepository
from .paths import AppPaths


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One diagnostic check with a status and optional detail message."""

    name: str
    status: CheckStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """Aggregated diagnostics returned by ``build_service_status``."""

    paths: AppPaths
    checks: tuple[DoctorCheck, ...]


def _calendar_status(status: object) -> CheckStatus:
    """The calendar's severity word for this one.

    Two ladders for one thing: the mail side says ``ERROR`` where the
    calendar says ``FAIL``, which is why the two renderers cannot share
    a tag table.
    """
    from pony.calendar.services import DiagnosticStatus

    if status is DiagnosticStatus.FAIL:
        return CheckStatus.ERROR
    if status is DiagnosticStatus.WARN:
        return CheckStatus.WARN
    return CheckStatus.OK


def _calendar_checks(config_path: Path, *, root: Path) -> list[DoctorCheck]:
    """Report the calendar's configuration and local state.

    The calendar is half of this program, so `pony doctor` answers for
    it too. *root* is where its files live: `data_dir/calendar`, shared
    with the mail side rather than resolved separately.

    An unconfigured calendar is not a problem to report: plenty of
    people only want the mail client.
    """
    from pony.calendar import config as calendar_config
    from pony.calendar import services as calendar_services
    from pony.calendar.index_store import (
        SqliteIndexRepository as CalendarIndexRepository,
    )
    from pony.calendar.paths import CalendarPaths
    from pony.calendar.storage import VdirMirrorRepository

    from .calendar_runtime import load_calendar_config

    calendar_paths = CalendarPaths(root=root)

    try:
        config = load_calendar_config(config_path)
    except ConfigError as exc:
        return [DoctorCheck("Calendar config", CheckStatus.ERROR, str(exc))]
    if config is None:
        return [
            DoctorCheck(
                "Calendar",
                CheckStatus.OK,
                "Not configured (add a [calendar] table to enable it)",
            )
        ]

    checks = [
        DoctorCheck(
            "Calendar config",
            CheckStatus.OK,
            f"{len(config.accounts)} account(s): "
            + ", ".join(a.name for a in config.accounts),
        )
    ]
    index_path = calendar_paths.index_file
    if index_path.exists():
        checks.append(DoctorCheck("Calendar index", CheckStatus.OK, str(index_path)))
    else:
        checks.append(
            DoctorCheck(
                "Calendar index",
                CheckStatus.WARN,
                f'Not yet created (run "pony calendar sync" first): {index_path}',
            )
        )
    mirror_root = calendar_paths.mirror_dir
    if not mirror_root.is_dir():
        checks.append(
            DoctorCheck(
                "Calendar mirror",
                CheckStatus.WARN,
                f"Does not exist (created on first sync): {mirror_root}",
            )
        )
        return checks

    mirror = VdirMirrorRepository(
        mirror_root,
        account_roots=calendar_config.mirror_roots(config, calendar_paths),
    )
    calendars = sum(len(mirror.list_calendars(a.name)) for a in config.accounts)
    checks.append(
        DoctorCheck(
            "Calendar mirror",
            CheckStatus.OK,
            f"{calendars} calendar(s): {mirror_root}",
        )
    )
    if not index_path.exists():
        # Nothing to compare against, and opening the index would
        # create the file this check just reported as missing.
        return checks
    index = CalendarIndexRepository(index_path)
    try:
        report = calendar_services.check_local_integrity(
            config=config, mirror=mirror, index=index
        )
    finally:
        index.close()
    checks.extend(
        DoctorCheck(
            f'Calendar integrity "{result.scope}"',
            _calendar_status(result.status),
            result.message,
        )
        for result in report.results
        if result.status is not calendar_services.DiagnosticStatus.OK
    )
    return checks


def build_service_status(
    *,
    paths: AppPaths,
    config_path: Path | None,
    config: AppConfig | None,
    config_error: str | None = None,
) -> ServiceStatus:
    """Run all doctor checks and return a :class:`ServiceStatus`.

    *config_error* is why the config could not be loaded, when it could
    not.  Without it this check could only say "Could not parse", which
    is the least useful sentence available at the one moment a user runs
    this command: the loader already knows whether the version is
    unsupported, a key is missing or the TOML is malformed.
    """
    effective_config_path = config_path or paths.config_file
    checks: list[DoctorCheck] = []

    # ------------------------------------------------------------------ #
    # Python version
    # ------------------------------------------------------------------ #
    ver = sys.version_info
    ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"
    if ver >= (3, 13):
        checks.append(DoctorCheck("Python version", CheckStatus.OK, ver_str))
    else:
        checks.append(
            DoctorCheck(
                "Python version",
                CheckStatus.WARN,
                f"{ver_str} (3.13+ recommended)",
            )
        )

    # ------------------------------------------------------------------ #
    # Configuration file
    # ------------------------------------------------------------------ #
    if not effective_config_path.exists():
        checks.append(
            DoctorCheck(
                "Config file",
                CheckStatus.ERROR,
                f"Not found: {effective_config_path}",
            )
        )
    elif config is None:
        detail = f"Could not parse: {effective_config_path}"
        if config_error:
            detail = f"{detail}\n    {config_error}"
        checks.append(DoctorCheck("Config file", CheckStatus.ERROR, detail))
    else:
        names = ", ".join(a.name for a in config.accounts)
        checks.append(
            DoctorCheck(
                "Config file",
                CheckStatus.OK,
                f"{len(config.accounts)} account(s): {names}",
            )
        )

    # ------------------------------------------------------------------ #
    # Index database
    # ------------------------------------------------------------------ #
    if not paths.index_db_file.exists():
        checks.append(
            DoctorCheck(
                "Index database",
                CheckStatus.WARN,
                f'Not yet created (run "pony sync" first): {paths.index_db_file}',
            )
        )
    else:
        try:
            conn = sqlite3.connect(str(paths.index_db_file))
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
            conn.close()
            table_count = row[0] if row else 0
            checks.append(
                DoctorCheck(
                    "Index database",
                    CheckStatus.OK,
                    f"{table_count} table(s): {paths.index_db_file}",
                )
            )
        except sqlite3.Error as exc:
            checks.append(
                DoctorCheck(
                    "Index database",
                    CheckStatus.ERROR,
                    f"Cannot open: {exc}",
                )
            )

    # ------------------------------------------------------------------ #
    # Per-account mirror paths
    # ------------------------------------------------------------------ #
    if config is not None:
        for account in config.accounts:
            label = f'Mirror "{account.name}"'
            mirror_path = account.mirror.path
            if not mirror_path.exists():
                checks.append(
                    DoctorCheck(
                        label,
                        CheckStatus.WARN,
                        f"Does not exist (created on first sync): {mirror_path}",
                    )
                )
            elif not mirror_path.is_dir():
                checks.append(
                    DoctorCheck(
                        label,
                        CheckStatus.ERROR,
                        f"Not a directory: {mirror_path}",
                    )
                )
            else:
                test_file = mirror_path / ".pony_write_test"
                try:
                    test_file.touch()
                    test_file.unlink()
                    checks.append(DoctorCheck(label, CheckStatus.OK, str(mirror_path)))
                except OSError:
                    checks.append(
                        DoctorCheck(
                            label,
                            CheckStatus.WARN,
                            f"Not writable: {mirror_path}",
                        )
                    )

    # ------------------------------------------------------------------ #
    # Calendar
    # ------------------------------------------------------------------ #
    checks.extend(_calendar_checks(effective_config_path, root=paths.calendar_dir))

    # ------------------------------------------------------------------ #
    # Markdown renderer (optional runtime dep)
    # ------------------------------------------------------------------ #
    import importlib.util

    if importlib.util.find_spec("markdown_it") is not None:
        checks.append(
            DoctorCheck(
                "Markdown renderer",
                CheckStatus.OK,
                "markdown-it-py available",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "Markdown renderer",
                CheckStatus.WARN,
                "markdown-it-py not installed (Markdown mode unavailable)",
            )
        )

    # ------------------------------------------------------------------ #
    # Mirror integrity (only when index exists and config is available)
    # ------------------------------------------------------------------ #
    if config is not None and paths.index_db_file.exists():
        try:
            from .mail.index_store import SqliteIndexRepository

            index = SqliteIndexRepository(
                database_path=paths.index_db_file,
            )
            index.initialize()
            for account in config.accounts:
                result = check_mirror_integrity(
                    account=account,
                    index=index,
                )
                checks.append(result)
        except Exception as exc:  # noqa: BLE001
            checks.append(
                DoctorCheck(
                    "Mirror integrity",
                    CheckStatus.WARN,
                    f"Scan failed: {exc}",
                )
            )

    return ServiceStatus(paths=paths, checks=tuple(checks))


# ====================================================================== #
# Mirror integrity scan
# ====================================================================== #


@dataclass(frozen=True, slots=True)
class MirrorIntegrityResult:
    """Outcome of scanning one account's mirror against its index."""

    account_name: str
    orphan_files: list[Path]
    stale_keys: list[str]


def check_mirror_integrity(
    *,
    account: AnyAccount,
    index: IndexRepository,
) -> DoctorCheck:
    """Compare one account's mirror files against its index rows.

    Returns a :class:`DoctorCheck` summarising orphan mirror files
    (present on disk but not referenced by any index row) and stale
    index rows (``storage_key`` points to a missing file).

    For mbox accounts only stale-key detection is performed because
    mbox stores all messages in a single file (no per-message files
    to orphan).
    """
    from .mail.domain import FolderRef

    label = f'Mirror integrity "{account.name}"'
    mirror_path = account.mirror.path

    if not mirror_path.exists():
        return DoctorCheck(label, CheckStatus.OK, "not yet created")

    # --- Build set of indexed storage keys per folder ---
    # Discover synced folders from folder_sync_state, then load their
    # storage keys from the messages table.
    indexed_keys: dict[str, set[str]] = {}  # folder_name → {storage_key}
    sync_states = index.list_folder_sync_states(
        account_name=account.name,
    )
    if not sync_states:
        return DoctorCheck(label, CheckStatus.OK, "no indexed messages")

    for fs in sync_states:
        folder = FolderRef(
            account_name=account.name,
            folder_name=fs.folder_name,
        )
        # Only the key set is wanted, and `list_folder_storage_keys`
        # drops the empty ones (pending appends) in SQL.  Asking for the
        # full rows materialised the whole account to throw 22 of 23
        # columns away.
        indexed_keys[fs.folder_name] = set(
            index.list_folder_storage_keys(folder=folder)
        )

    orphan_files: list[Path] = []
    stale_keys: list[str] = []

    if account.mirror.format == "maildir":
        orphan_files, stale_keys = _scan_maildir(
            mirror_path,
            account.name,
            indexed_keys,
        )
    else:
        # mbox: only check stale keys (no per-message files to orphan).
        stale_keys = _scan_mbox_stale(
            mirror_path,
            account.name,
            indexed_keys,
        )

    if not orphan_files and not stale_keys:
        total = sum(len(v) for v in indexed_keys.values())
        return DoctorCheck(
            label,
            CheckStatus.OK,
            f"{total} message(s) verified",
        )

    parts: list[str] = []
    if orphan_files:
        parts.append(f"{len(orphan_files)} orphan file(s)")
    if stale_keys:
        parts.append(f"{len(stale_keys)} stale index row(s)")
    # A stale index row is a message the reader lists and then cannot
    # open: the row points at a file that is not there.  That is a
    # failure, not a warning, and `pony doctor`'s error count has to
    # reflect it -- the calendar's own doctor already calls the same
    # corruption a FAIL ("index rows point at missing mirror files").
    # A mirror file with no index row is the other way round and loses
    # nothing, so it stays a warning, as it does there.
    status = CheckStatus.ERROR if stale_keys else CheckStatus.WARN
    return DoctorCheck(label, status, ", ".join(parts))


def _maildir_base_key(filename: str) -> str:
    """Strip Maildir flag suffixes to recover the base storage key.

    Maildir filenames look like ``<key>`` in ``new/`` or
    ``<key>!2,FLAGS`` (or ``<key>:2,FLAGS``) in ``cur/``.
    """
    for sep in ("!2,", ":2,"):
        idx = filename.find(sep)
        if idx != -1:
            return filename[:idx]
    return filename


def _scan_maildir(
    mirror_path: Path,
    account_name: str,
    indexed_keys: dict[str, set[str]],
) -> tuple[list[Path], list[str]]:
    """Scan a Maildir mirror for orphans and stale keys."""
    orphan_files: list[Path] = []
    stale_keys: list[str] = []

    # Discover folder directories.  INBOX lives at the root; other
    # folders are dot-prefixed subdirectories.
    folder_dirs: dict[str, Path] = {"INBOX": mirror_path}
    for candidate in mirror_path.iterdir():
        if candidate.is_dir() and candidate.name.startswith("."):
            folder_name = candidate.name[1:]
            if folder_name:
                folder_dirs[folder_name] = candidate

    for folder_name, folder_path in folder_dirs.items():
        keys_on_disk: set[str] = set()
        file_by_key: dict[str, Path] = {}
        for subdir_name in ("cur", "new"):
            subdir = folder_path / subdir_name
            if not subdir.is_dir():
                continue
            for entry in subdir.iterdir():
                if not entry.is_file():
                    continue
                base = _maildir_base_key(entry.name)
                keys_on_disk.add(base)
                file_by_key[base] = entry

        keys_in_index = indexed_keys.get(folder_name, set())

        # Orphans: on disk but not in index.
        for key in keys_on_disk - keys_in_index:
            orphan_files.append(file_by_key[key])

        # Stale: in index but not on disk.
        for key in keys_in_index - keys_on_disk:
            stale_keys.append(f"{account_name}/{folder_name}/{key}")

    return orphan_files, stale_keys


def _scan_mbox_stale(
    mirror_path: Path,
    account_name: str,
    indexed_keys: dict[str, set[str]],
) -> list[str]:
    """Check mbox index rows for stale keys (missing mbox files)."""
    stale: list[str] = []
    for folder_name, keys in indexed_keys.items():
        mbox_file = mirror_path / f"{folder_name}.mbox"
        if not mbox_file.exists():
            # Entire folder file is missing — all keys are stale.
            for key in keys:
                stale.append(f"{account_name}/{folder_name}/{key}")
    return stale
