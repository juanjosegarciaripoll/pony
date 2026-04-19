# Changelog

All notable changes to Pony Express are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.5.0]
### Added

- **Accent- and case-insensitive full-text search via FTS5**: message
  and contact search was broken for any locale with non-ASCII letters
  because SQLite's built-in case folding only covers ASCII — so `maria`
  never matched `María`. The LIKE-based query paths are gone; search
  now runs against FTS5 virtual tables tokenised with
  `unicode61 remove_diacritics 2`, which folds both case and diacritics
  by construction. Applies to `pony search`, the TUI search dialog, the
  contacts browser, and the MCP `search_messages` / `search_contacts`
  tools.
- **Guided recovery flow on index-schema mismatch**: opening an older
  index (schema v1) with a newer binary used to abort with a bare
  error. `pony` now explains the three recovery steps (export contacts,
  delete index + mirrors, resync) and offers to perform the first two
  automatically, defaulting to **No**. Contacts are snapshotted to
  `<data_dir>/contacts-backup-<UTC-timestamp>.bbdb` via a new
  `load_contacts_for_backup()` entry point that reads directly from the
  mismatched DB.
- **Folder browser: unread indicator + hierarchical display**:
    - Folders whose messages are all read are rendered dim; folders
      with unread messages are rendered bright.  A synthetic parent
      (see below) follows the same rule based on whether any
      *descendant* has unread, so you can tell an account has
      something new without expanding its subtree.
    - Dotted / slashed folder names like ``Archives.2026`` or
      ``Lists/Unions`` are displayed as nested subtrees
      (``Archives`` → ``2026``).  The delimiter (``.`` or ``/``) is
      detected per-folder-name, which handles both Dovecot and Cyrus
      server conventions without configuration.  The stored name on
      the server, in the mirror, and in the index is unchanged — this
      is purely a display-side transformation.  When both a parent
      folder (e.g. ``Archives``) and its nested child
      (``Archives.2026``) exist on the server, the parent is
      selectable and shows its own unread count with the child nested
      beneath it.
- **Per-attachment retrieval on CLI and MCP**: both surfaces now let
  you pull a single attachment's bytes, not just see that attachments
  exist.
    - `pony message get` now lists every attachment (index, filename,
      content-type, size) after the metadata block when the message
      body is locally available — previously only an `Attach.: yes/no`
      line.
    - New `pony message attachment <account> <folder> <message-id>
      <index>` writes the bytes to the attachment's own filename in
      cwd, or to `-o PATH`, or to stdout via `--stdout`.  Refuses to
      clobber an existing file unless `-f/--force` is passed.
    - MCP `get_message` now carries the same `attachments` array as
      `get_message_body` (when the mirror holds the bytes) so AI
      agents can discover what's available without pulling the full
      body.
    - New MCP `get_attachment(account, folder, message_id, index)`
      tool returns `{filename, content_type, size_bytes, data_base64,
      text?}`.  `data_base64` is always present (transport-safe for
      any attachment type); `text` is added for `text/*` attachments
      so agents can read them without base64-decoding on the client
      side.  Tool docstrings steer callers to `get_attachment` from
      `get_message` / `get_message_body`.
    - Internals: the MIME-walking logic that used to live in the TUI
      (`MessageViewPanel.save_attachment`) is extracted into a shared
      `extract_attachment(raw, index)` helper in `message_renderer`;
      CLI, MCP and TUI now share one indexing contract.
- **Move a message to another folder (`M`)**: new TUI action.  For
  same-account moves the mirror file is renamed in place and the index
  row switches folders with `uid=NULL` — Message-ID is preserved and
  the next sync emits `UID MOVE` server-side.  For cross-account moves
  there's no atomic IMAP primitive, so the operation decomposes into
  cross-account copy (MID preserved) + retire source: IMAP sources are
  marked `TRASHED` so the next sync `EXPUNGE`s them, local sources are
  deleted outright.  Target-first ordering means an interruption
  leaves a duplicate, not a loss.  Guards refuse moves out of or into
  read-only folders, and into folders excluded from sync.
- **Copy a message to another folder (`C`)**: new TUI action, modelled
  on archive. Opens a folder picker spanning every account in the
  config (including local accounts, which the main folder panel hides),
  copies the raw bytes into the chosen target's mirror, and inserts a
  `uid=NULL` index row so the next sync pushes the copy server-side
  via `APPEND`. Multi-select works: every marked row is copied.
  Same-account copies rewrite `Message-ID` to a synthetic
  `<pony-copy-*@pony.local>` id so the sync planner doesn't mistake
  the duplicate for a move (cross-folder identity is keyed on MID and
  multi-folder identity is a deferred feature). Cross-account copies
  preserve the original `Message-ID` — accounts are independent
  identity namespaces, so a true copy keeps IMAP thread integrity
  intact.
- **Automatic mirror rescan for local accounts on TUI startup**: local
  accounts (`account_type = "local"`) have no sync step, so files added
  or removed in the mirror by external tools (offlineimap, getmail,
  procmail, Emacs/Gnus) never reached the SQLite index. `pony tui` now
  reconciles the delta before the reader opens — new files are
  projected and indexed, rows whose files vanished are pruned, and
  pending-append rows with empty `storage_key` are preserved so the
  sync engine can still push them upstream. A per-folder liveness
  line, an announcement of the planned work, and a per-item progress
  bar are rendered on stderr so startup is never silent, even on
  large mbox archives.

### Changed

- **Index schema bumped to version 2. Existing v1 databases refuse to
  open.** Detected via `PRAGMA user_version` with a legacy-table
  sentinel so DBs that predate schema stamping are still flagged. The
  in-place migration is intentionally deferred; the guided reset above
  is the recovery path. Users upgrading from 0.4.x will be prompted on
  first run.
- **`body_preview` removed from CLI and MCP responses.** `pony message
  get` no longer prints a `Preview:` block, and MCP `search_messages` /
  `get_message` no longer include `body_preview` in the returned dict.
  The index is now pure metadata; callers that need body text fetch it
  from the mirror via `pony message body` or MCP `get_message_body`.
  This keeps the FTS5 index lean and removes a redundant, lossy copy
  of the body. The 4000-byte per-MIME-part cap on projected previews
  is also gone; a 256 KB byte-safe cap is applied once on the final
  collapsed text.

### Fixed

- **RFC 2047 encoded-words with unknown charset labels** (e.g.
  `=?x-unknown?Q?…?=`) no longer abort mbox import. Python's
  `bytes.decode` raises `LookupError` on unregistered codec names —
  `errors="replace"` only handles bad bytes inside a *known* codec —
  so one malformed header could tank the whole ingest.
  `_decode_header` now falls back to latin-1 (a total mapping that
  never fails) when the declared charset is unknown, yielding a
  lossless best-effort string instead of crashing.

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
