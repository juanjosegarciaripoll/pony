"""Helpers for Textual TUI tests.

Plain functions (no pytest fixtures) that build the repositories, config,
and app instances the TUI needs.  The codebase prefers explicit builder
helpers over fixtures (see ``tests/test_fixture_flow.py`` and
``tests/test_contacts.py``); these functions match that style.

Every builder writes under ``tests/conftest.TMP_ROOT`` so the existing
atexit cleanup applies — no per-test teardown required.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from conftest import TMP_ROOT
from textual import work
from textual.await_complete import AwaitComplete
from textual.pilot import Pilot
from textual.widgets import DirectoryTree, Tree
from textual.widgets._directory_tree import DirEntry
from textual.widgets.tree import TreeNode

from pony.credentials import PlaintextCredentialsProvider
from pony.domain import (
    AccountConfig,
    AnyAccount,
    AppConfig,
    FolderRef,
    MessageRef,
    MirrorConfig,
    SmtpConfig,
)
from pony.index_store import SqliteIndexRepository
from pony.message_projection import project_rfc822_message
from pony.paths import AppPaths
from pony.protocols import CredentialsProvider, IndexRepository, MirrorRepository
from pony.storage import MaildirMirrorRepository
from pony.tui.app import ComposeApp, PonyApp


class _ExecutorCleanupMixin:
    """Release Textual's executor before pytest closes the event loop."""

    __test__ = False

    @asynccontextmanager
    async def run_test(self, **kwargs: object) -> AsyncIterator[Pilot[None]]:
        async with super().run_test(**kwargs) as pilot:  # type: ignore[misc]
            yield pilot
        loop = asyncio.get_running_loop()
        executor = loop._default_executor  # type: ignore[attr-defined]
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            loop._default_executor = None  # type: ignore[attr-defined]


class TestPonyApp(_ExecutorCleanupMixin, PonyApp):
    """PonyApp test harness with deterministic executor cleanup."""

    __test__ = False


class TestComposeApp(_ExecutorCleanupMixin, ComposeApp):
    """ComposeApp test harness with deterministic executor cleanup."""

    __test__ = False


class TestDirectoryTree(DirectoryTree):
    """DirectoryTree variant without thread dispatch for fixture filesystems."""

    __test__ = False

    @work(exit_on_error=False)
    async def _load_directory(self, node: TreeNode[DirEntry]) -> list[Path]:
        assert node.data is not None
        path = node.data.path.expanduser().resolve()
        try:
            entries = list(path.iterdir())
        except OSError:
            return []
        return sorted(
            entries,
            key=lambda entry: (not self._safe_is_dir(entry), entry.name.lower()),
        )

    def reload(self) -> AwaitComplete:
        """Reload the root without starting Textual's infinite queue worker."""
        return self.reload_node(self.root)

    def _add_to_load_queue(self, node: TreeNode[DirEntry]) -> AwaitComplete:
        """Load one node directly instead of waiting on a background queue."""
        assert node.data is not None
        if node.data.loaded:
            return AwaitComplete.nothing()
        node.data.loaded = True

        async def load() -> None:
            content = await self._load_directory(node).wait()
            if content:
                self._populate_node(node, content)

        return AwaitComplete(load())

    async def _on_tree_node_expanded(self, event: Tree.NodeExpanded[DirEntry]) -> None:
        event.stop()
        entry = event.node.data
        if entry is None:
            return
        if self._safe_is_dir(entry.path):
            await self._add_to_load_queue(event.node)
        else:
            self.post_message(self.FileSelected(event.node, entry.path))

    async def _on_tree_node_selected(self, event: Tree.NodeSelected[DirEntry]) -> None:
        event.stop()
        entry = event.node.data
        if entry is None:
            return
        if self._safe_is_dir(entry.path):
            self.post_message(self.DirectorySelected(event.node, entry.path))
        else:
            self.post_message(self.FileSelected(event.node, entry.path))


class TestDirOnlyTree(TestDirectoryTree):
    """Non-threaded directory-only tree for save-folder picker tests."""

    __test__ = False

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if path.is_dir()]


def make_tmp_paths(label: str) -> AppPaths:
    """Return an :class:`AppPaths` rooted in a fresh temp directory."""
    root = TMP_ROOT / f"tui-{label}" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return AppPaths(
        config_file=root / "config.toml",
        data_dir=root / "data",
        state_dir=root / "state",
        cache_dir=root / "cache",
        log_dir=root / "state" / "logs",
        index_db_file=root / "data" / "index.sqlite3",
    )


def make_test_account(
    paths: AppPaths,
    name: str = "acct",
    *,
    with_smtp: bool = True,
    archive_folder: str | None = None,
    password: str | None = "secret",
) -> AccountConfig:
    """Build a deterministic IMAP-shaped account for tests.

    The SMTP block is real (example.com) but tests patch ``smtp_send``
    before hitting the wire, so no network is ever involved.
    """
    mirror_dir = paths.data_dir / "mirrors" / name
    mirror_dir.mkdir(parents=True, exist_ok=True)
    smtp = (
        SmtpConfig(host="smtp.example.com")
        if with_smtp
        else SmtpConfig(
            host="smtp.example.com",
        )
    )
    return AccountConfig(
        name=name,
        email_address=f"{name}@example.com",
        imap_host="imap.example.com",
        smtp=smtp,
        username=name,
        credentials_source="plaintext",
        mirror=MirrorConfig(path=mirror_dir, format="maildir"),
        password=password,
        archive_folder=archive_folder,
    )


def make_test_config(
    accounts: Sequence[AnyAccount] = (),
) -> AppConfig:
    """Wrap *accounts* in an :class:`AppConfig`."""
    return AppConfig(accounts=tuple(accounts))


def make_index(paths: AppPaths) -> SqliteIndexRepository:
    """Return an initialised SQLite index at ``paths.index_db_file``."""
    paths.index_db_file.parent.mkdir(parents=True, exist_ok=True)
    repo = SqliteIndexRepository(database_path=paths.index_db_file)
    repo.initialize()
    return repo


def make_mirrors(config: AppConfig) -> dict[str, MaildirMirrorRepository]:
    """One :class:`MaildirMirrorRepository` per account."""
    mirrors: dict[str, MaildirMirrorRepository] = {}
    for account in config.accounts:
        mirrors[account.name] = MaildirMirrorRepository(
            account_name=account.name,
            root_dir=account.mirror.path,
        )
    return mirrors


def make_credentials(config: AppConfig) -> CredentialsProvider:
    """Plaintext credentials provider — tests set password=... on the account."""
    return PlaintextCredentialsProvider(config)


def seed_message(
    *,
    index: IndexRepository,
    mirror: MirrorRepository,
    folder: FolderRef,
    raw: bytes,
    message_id: str | None = None,
) -> MessageRef:
    """Write *raw* into *mirror*/*folder* and insert its projection into *index*.

    Returns the assigned ``MessageRef`` (with the row id set) so tests
    can look it up later.  When *message_id* is None the projection
    uses whatever Message-ID header the raw bytes carry; callers that
    share the same raw bytes across folders should pass a distinct
    *message_id* per placement to avoid Message-ID display collisions.
    """
    import dataclasses

    storage_key = mirror.store_message(folder=folder, raw_message=raw)
    ref = MessageRef(
        account_name=folder.account_name,
        folder_name=folder.folder_name,
        id=0,
    )
    projected = project_rfc822_message(
        message_ref=ref,
        raw_message=raw,
        storage_key=storage_key,
    )
    if message_id is not None:
        projected = dataclasses.replace(projected, message_id=message_id)
    saved = index.insert_message(message=projected)
    return saved.message_ref


def build_pony_app(
    *,
    label: str = "pony",
    accounts: Sequence[AnyAccount] | None = None,
    seed: Sequence[tuple[FolderRef, bytes]] = (),
) -> tuple[
    PonyApp,
    AppConfig,
    AppPaths,
    SqliteIndexRepository,
    dict[str, MaildirMirrorRepository],
]:
    """Construct a :class:`PonyApp` wired to real Sqlite + Maildir repos.

    When *accounts* is None a single default IMAP account is created.
    Every ``(folder, raw)`` in *seed* is written both to the mirror and
    to the index before the app is returned — tests assert against the
    resulting state after a Pilot session.
    """
    paths = make_tmp_paths(label)
    if accounts is None:
        accounts = (make_test_account(paths),)
    config = dataclasses.replace(
        make_test_config(accounts=accounts),
        downloads_path=paths.data_dir / "downloads",
    )
    index = make_index(paths)
    mirrors = make_mirrors(config)
    credentials = make_credentials(config)
    for folder, raw in seed:
        seed_message(
            index=index,
            mirror=mirrors[folder.account_name],
            folder=folder,
            raw=raw,
        )
    app = TestPonyApp(
        config=config,
        index=index,
        mirrors=dict(mirrors),
        credentials=credentials,
        config_path=paths.config_file,
    )
    return app, config, paths, index, mirrors


def build_compose_app(
    *,
    label: str = "compose",
    account: AnyAccount | None = None,
    to: str = "",
    cc: str = "",
    bcc: str = "",
    subject: str = "",
    body: str = "",
    markdown_mode: bool = False,
) -> tuple[
    ComposeApp,
    AppConfig,
    AppPaths,
    SqliteIndexRepository,
    dict[str, MaildirMirrorRepository],
]:
    """Construct a :class:`ComposeApp` ready for Pilot-driven tests."""
    paths = make_tmp_paths(label)
    if account is None:
        account = make_test_account(paths)
    config = dataclasses.replace(
        make_test_config(accounts=(account,)),
        downloads_path=paths.data_dir / "downloads",
    )
    index = make_index(paths)
    mirrors = make_mirrors(config)
    app = TestComposeApp(
        config=config,
        account=account,
        index=index,
        mirrors=dict(mirrors),
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        markdown_mode=markdown_mode,
    )
    return app, config, paths, index, mirrors
