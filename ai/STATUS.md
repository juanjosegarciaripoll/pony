# Project Status

## Current version

See `src/pony/version.py` and `pyproject.toml`. Release history with
per-version details lives in `CHANGELOG.md` — this file is not a
changelog.

## Shape of the project

All v1 capabilities from `SPECIFICATIONS.md` are implemented and
covered by tests:

- IMAP sync (two-pass plan/execute) and SMTP send.
- Maildir + mbox mirror backends with a shared conformance suite.
- SQLite index (FTS5-backed, diacritic- and case-insensitive search).
- Three-pane Textual TUI for reading, composing, searching, contacts.
- Person-centric contacts with BBDB import/export.
- MCP server (stdio + Streamable HTTP) exposing read-only mail tools.
- PyInstaller-based standalone builds with platform installers.

## Followups on the queue

Not commitments, just the known-next items worth keeping in mind.

- **Rebuild-from-mirrors command.** The new schema gate refuses legacy
  DBs and the CLI offers an export-contacts-then-wipe flow; rebuilding
  the index from mirror bytes (no re-download) is the missing third
  option. Needs synthetic Message-ID rework first (see below).
- **Synthetic Message-ID rework.** The current formula bakes UID into
  the hash (`sync.py`), so synthetic IDs cannot survive a rebuild.
  Revisit before implementing rebuild-from-mirror.
- **`body_preview` → `body_text` rename.** The column no longer holds a
  short preview; the name is inherited. Worth doing on the next schema
  bump, not on its own.
- **Background / periodic sync** (requires an exclusive write lock).
- **OAuth authentication flows.**
- **Browser-based reader/composer UI.**
- **POP support.**
- **Multi-machine conflict handling** (beyond the current state-based
  reconciliation).
- **Full multi-folder support for Gmail labels** (today aggregate
  folders are warned about and recommended for exclusion).
- **Per-folder single-transaction sync** (currently idempotent re-sync
  covers partial failures).

## Infrastructure

- MkDocs Material docs, published to GitHub Pages.
- PyInstaller release builds for Linux, macOS, Windows.
- Quality gates: `ruff`, `mypy` (strict), `basedpyright` (strict),
  `pytest`.
- Release workflow: manually dispatched, reads the version from a new
  `## [X.Y.Z]` heading in `CHANGELOG.md`, stamps the date, updates
  `pyproject.toml` + `version.py`, tags, and publishes the GitHub
  release.
