# Changelog

All notable changes to Pony Express are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
