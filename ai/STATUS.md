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

- **DataTable population cost on giant folders.** After the
  ``FolderMessageSummary`` work the DB+parse portion of
  ``MessageListPanel.load_folder`` is ~300 ms on a 17k-row folder
  (down from ~1.3 s).  The remaining bottleneck is Textual's
  ``DataTable.add_row`` loop — 17k ``add_row`` calls, each paying
  layout cost, with no built-in row virtualization.  Options to
  explore, in rough order of bang-for-buck:
  1. ``@work(thread=True)`` + ``call_from_thread`` batched inserts
     (~100 rows at a time) so the UI stays interactive while the
     table fills.  Total time unchanged, perceived latency down.
  2. Pagination / windowing — only add rows near the viewport,
     extend on scroll.  The real fix for 50k+ row folders, but a
     non-trivial UI refactor since ``DataTable`` isn't virtualized.
  3. A compound index on
     ``(account_name, folder_name, received_at DESC)`` to eliminate
     the temp B-tree sort EXPLAIN currently shows.  Drops the lean
     SELECT from ~200 ms to tens of ms, but requires a schema bump.
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
