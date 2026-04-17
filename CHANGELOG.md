# Changelog

All notable changes to Pony Express are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0]

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
