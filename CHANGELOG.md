# Changelog

All notable changes to Pony Express are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-04-18
### Fixed

- **Mirror retrieval for IMAP-synced mail**: the `MirrorRepository`
  protocol silently required `MessageRef.message_id` to equal the
  backend's on-disk storage key. For messages ingested from IMAP,
  that field holds the RFC 5322 `Message-ID` header instead; the real
  storage key lives on `IndexedMessage.storage_key`. Every caller that
  did not apply the kludge of rebuilding a `MessageRef(message_id=
  storage_key)` silently failed:
    - `pony rescan` counted every synced message as "missing" and never
      refreshed its projection.
    - `pony message body` and MCP `get_message_body` returned *"not
      found in mirror"* / `null` for every synced message.
    - `PushDeleteOp` silently no-opped the mirror delete; sync removed
      the index row but left the file behind, so mirrors grew
      unbounded on every archive.
    - The retention-based trash purge hit the same bug.
- **HTML `<style>` / `<script>` content in body previews** ([0.3.x
  regression]): the previous regex-only tag stripper left the CSS rules
  from `<style>` blocks and Outlook conditional comments as literal
  preview text. New `pony.html_sanitize` module strips comments (including
  conditional comments), `<head>`, `<style>`, `<script>`, and `<noscript>`
  blocks before tag removal, and decodes HTML entities.
- **Sync deadlock on folder transitions**: `_execute_folder_plan` ran a
  background producer thread that called `session.fetch_messages_batch`
  while the main consumer thread concurrently called other
  `session.*` methods on the same `imapclient` / `imaplib` session.
  `imaplib` is not thread-safe; two threads racing one socket
  occasionally interleaved commands (e.g. two `SELECT INBOX`s with
  consecutive tags) and deadlocked `imaplib`'s tag dispatcher. The
  TUI would then freeze with `Q` unable to cancel. Fixed by
  restructuring per-folder execution into two phases: Phase 1 runs
  session-free ops (`FetchNewOp`, `ServerDeleteOp`, `ServerMoveOp`,
  `PullFlagsOp`, `MergeFlagsOp`, `LinkLocalOp`, `RestoreOp`) with the
  producer holding exclusive use of `session`; Phase 2 runs
  session-touching ops (`PushFlagsOp`, `PushDeleteOp`, `PushMoveOp`,
  `PushAppendOp`, `ReUploadOp`) serially on the main thread after the
  producer has joined. The producer is now the sole thread that ever
  touches the session while it is running.

### Changed

- **`MirrorRepository` protocol**: now keys every method off the
  backend's own `storage_key` (the maildir filename or mbox integer)
  instead of a `MessageRef`. `store_message`, `list_messages`, and
  `move_message_to_folder` return `str` storage keys rather than
  synthetic `MessageRef`s. The layering is now honest: RFC 5322
  identity is an index-side concern; mirror methods do not see it.
- **`SqliteIndexRepository.purge_expired_trash`** now returns
  `list[tuple[FolderRef, str]]` so callers can clean the mirror.
- **TUI `R` / `u` are now explicit set/clear**: pressing `R` always
  marks the target(s) as read; `u` always marks them unread.  (`R`
  previously toggled `SEEN`, which is inconsistent with its label
  and would produce chaotic results on mixed-state bulk selections.)
  `!` still toggles `FLAGGED`.
- **Shared TUI key bindings**: `src/pony/tui/bindings.py` now holds
  `MARK_BINDINGS` (`m` / `Shift+Down` / `Shift+Up`) and
  `MOTION_BINDINGS` (`n` / `p` / `<` / `>`), used by both the contacts
  browser and the messages panel.

### Added

- **Embedded MCP server**: add `[mcp]` to `config.toml` to have the MCP
  HTTP server start automatically in a background thread when `pony tui`
  launches. A TUI notification shows the URL on startup. Recommended for
  users who keep the TUI open and want simultaneous AI assistant access
  without managing a separate process.
- **`pony rescan [account]`** CLI command: re-project every indexed
  message from local mirror bytes. Refreshes cached fields (sender,
  recipients, subject, body_preview, has_attachments, received_at)
  without re-downloading from IMAP. Preserves all sync state.
- **`pony folder list [account]`**, **`pony message get`**, **`pony
  message body`**: CLI counterparts to the existing read-only MCP
  tools. `folder list` shows indexed counts and last-sync status per
  folder.
- **Richer MCP `list_folders` output**: each folder entry now includes
  `message_count`, `highest_uid`, and `synced_at` (matching what the
  CLI displays).
- Regression tests pinning the mirror identity contract: a conformance
  case verifies the returned storage_key is distinct from the RFC 5322
  Message-ID; sync tests round-trip a synced message through
  `mirror.get_message_bytes` and verify `PushDeleteOp` plus the
  retention purge actually remove mirror files.
- **Multi-select in the messages panel**: `m` / `Shift+Down` /
  `Shift+Up` mark rows (the icon cell shows `*` while marked,
  replacing the normal `!` / `+` / blank glyph — no new column).
  The existing action keys `R` / `u` / `!` / `d` / `A` act on every
  marked row when any are marked, falling back to the cursor row
  otherwise.  Marks clear on folder switch, search entry/exit, and
  after any bulk action.  Bindings mirror the contacts browser.

## [0.3.0] - 2026-04-17
### Added

- **Standalone executables with bundled documentation**: each release now
  ships two artifacts per platform — a platform installer and a portable
  archive suitable for Homebrew, Scoop, or similar package managers.
    - **Windows**: Inno Setup `.exe` installer (with optional PATH
      registration) and a portable `.zip`.
    - **macOS**: drag-to-Applications `.dmg` and a portable `.tar.gz`.
    - **Linux**: self-contained `.AppImage` and a portable `.tar.gz`.
- **Offline documentation**: the pre-built MkDocs HTML site is bundled
  inside the binary via PyInstaller's `--add-data`. `pony docs` opens
  the bundled docs in the default browser; falls back to the GitHub Pages
  URL when running from source.
- **`pony docs` command**: open the documentation without leaving the
  terminal.
- **MCP server** (`pony mcp-server`): exposes read-only mail operations as
  MCP tools for use with Claude Desktop, Claude Code, and any network MCP
  client. Tools: `search_messages`, `list_folders`, `list_messages`,
  `get_message`, `get_message_body`, `search_contacts`, `get_sync_status`.
  Runs over stdio by default (local use, Claude Desktop); pass `--port N`
  for Streamable HTTP (Docker / remote deployments). HTTP mode is
  compatible with running `pony tui` in a separate process. New runtime
  dependency: `mcp>=1.0`.
- **`scripts/build.py`**: cross-platform local build script. Run with
  `uv run python scripts/build.py [--installer] [--skip-tests]
  [--skip-docs]`. Artifacts land in `artifacts/`.
- **`pony.spec`**: PyInstaller spec file controlling what is bundled
  (docs, config sample, platform icon). Replaces the ad-hoc command
  previously generated inline by the CI workflow.

### Changed

- **`release-build.yml`** modernised: switched from bare `pip install` to
  `uv sync`; MkDocs site is built before PyInstaller so docs are always
  bundled; Inno Setup is installed via Chocolatey on Windows runners;
  deprecated `actions/upload-release-asset@v1` replaced with
  `gh release upload`.
- **`pyproject.toml`**: added `[dependency-groups] build` group containing
  `pyinstaller>=6.0`. Install with `uv sync --group build`.

## [0.2.0]

### Added

- **Archive action**: press ++shift+a++ in the TUI to move the selected
  message into the account's archive folder. Configure with
  `archive_folder = "..."` on any IMAP account. The move is applied
  immediately in the local mirror and index; the next sync pushes it to
  the server. The archive folder is created on the server automatically
  on first use via the same machinery that handles manual folder
  creation.
- **New folder action**: press ++shift+n++ in the TUI to create a folder
  in the current account's local mirror. The next sync compares local
  mirror folders against server folders and issues `IMAP CREATE` for any
  folder that exists only locally. Deletion of folders is intentionally
  not supported.
- **Generalised local-move sync**: `uid IS NULL` on an index row is now
  the canonical signal that a row must be pushed to the server. The sync
  planner introduces three new operation types that cover archive and
  any future local mutation that round-trips through sync:
    - `PushMoveOp` — run `UID MOVE` (RFC 6851) or `UID COPY` +
      `\Deleted` + `EXPUNGE` when a local pending row is in a different
      folder than the server's current location.
    - `PushAppendOp` — `APPEND` the mirror bytes when the server has no
      copy of the message anywhere.
    - `LinkLocalOp` — adopt a freshly-assigned server UID into the
      existing pending row, no refetch, no duplicate mirror file.
- **Local-only folders propagate upstream**: the planner diffs mirror
  folders against server folders at the top of the execution pass; any
  folder present only locally and passing the sync policy gets a
  server-side `CREATE` before per-folder ops run. `AccountSyncPlan`
  gains a `creates` field.
- `MirrorRepository.move_message_to_folder()` for cross-folder relocation
  of mirror bytes (rename in Maildir; copy-and-delete in mbox).
- `MirrorRepository.create_folder()` for creating empty mirror folders
  (idempotent).
- `ImapClientSession.move_message()` in the session protocol, with RFC
  6851 `UID MOVE` fast path and a compatible fallback.
- `ImapClientSession.create_folder()` (idempotent).
- **Application icon**: a coral pony-head + envelope mark ships under
  `icons/` as `.png`, `.svg`, `.ico` (Windows), and `.icns` (macOS).
  Release builds embed the platform-appropriate icon via PyInstaller's
  `--icon` flag; the MkDocs site uses it as the header logo and
  favicon; the README displays it above the title.

### Changed

- Release workflow: CHANGELOG.md is now the source of truth for the
  release version. Write a new undated `## [X.Y.Z]` heading and trigger
  the workflow; it propagates the version to `pyproject.toml` and
  `version.py`, stamps the date, and tags. The only guard is that the
  tag `vX.Y.Z` must not already exist.
- Sync algorithm documentation (`ai/SYNCHRONIZATION.md` and
  `docs/synchronization.md`) updated to describe the `uid IS NULL`
  signal, the new operation types, and the local-move flow. Conflict
  taxonomy gains C-9 for pending local moves.

## [0.1.0] - 2026-04-17
First feature-complete release of Pony Express.

### Added

- **IMAP sync engine**: two-pass plan/execute architecture with three-way flag
  merge, mass-deletion protection (20% threshold), UIDVALIDITY reset handling,
  and per-folder SSL/port configuration.
- **Maildir and mbox storage**: per-account configurable local mirrors with
  shared conformance tests across both backends.
- **SQLite index**: unified message table with full-text search across sender,
  recipients, subject, and body; case-sensitive and case-insensitive modes;
  sync checkpoints and pending operations.
- **Batched SQLite transactions**: `connection()` context manager with
  thread-local reuse and reentrant nesting for efficient bulk operations.
- **Terminal UI (Textual)**: three-pane reader (folder list, message list,
  message preview) with screen-specific keybinding isolation.
- **Composer**: reply, forward, compose from scratch; `ctrl+x` prefix chord
  for send, attach, external editor, Markdown toggle, cancel.
- **Markdown composition**: `ctrl+x m` toggle per message; produces
  `multipart/alternative` (plain Markdown source + rendered HTML) via
  `markdown-it-py`.
- **Search**: query parser supporting `from:`, `to:`, `cc:`, `subject:`,
  `body:`, `case:yes`, bare words, and quoted phrases; search dialog in TUI
  scoped to folder or account.
- **SMTP sending**: SSL and STARTTLS support; sent/draft folder auto-discovery;
  failure recovery with draft save option.
- **Person-centric contacts**: multiple emails per contact, aliases, affix,
  organization, notes; auto-harvest from To/Cc during sync; ranked
  autocomplete in composer.
- **Contacts browser/editor**: DataTable with search, mark, delete, merge;
  edit screen with all fields; detail view with `Enter`.
- **BBDB import/export**: bidirectional sync with Emacs BBDB v3 files;
  `bbdb_path` config option for auto-sync on `pony sync`; smart merge by
  email matching.
- **Sync progress reporting**: `ProgressInfo` dataclass with per-folder and
  per-operation callbacks; TUI progress bar; CLI counter line.
- **Diagnostics**: `pony doctor` checks Python version, config, index DB,
  mirror paths, mirror integrity (orphan files, stale index rows), and
  optional dependencies.
- **Four credential backends**: plaintext, environment variable, external
  command, OS-encrypted blob (DPAPI on Windows, PBKDF2+SHAKE-256 on
  Linux/macOS).
- **Cross-platform support**: `pathlib.Path` throughout, XDG/APPDATA/
  LOCALAPPDATA resolution, `_sanitize_for_path` for unsafe characters.
- **CLI commands**: `pony tui`, `pony sync`, `pony compose`, `pony search`,
  `pony doctor`, `pony server-summary`, `pony local-summary`, `pony reset`,
  `pony config edit`, `pony account add`, `pony account set-password`,
  `pony contacts browse/search/show/export/import`, `pony --version`.
- **HTML rendering**: style and script block stripping for clean plain-text
  display of HTML-only emails; `w` key opens full HTML in browser.
- **Documentation**: MkDocs Material site with configuration reference, CLI
  reference, TUI guide, composer guide, contacts guide, sync overview,
  architecture overview, and development guide; automated GitHub Pages
  deployment.
- **Release automation**: PyInstaller-based multi-platform builds (Linux,
  macOS, Windows) triggered by GitHub releases.
[0.1.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.1.0
[0.3.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.3.0
[0.4.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.4.0
