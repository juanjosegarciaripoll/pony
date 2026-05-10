# Project Status

Version: `src/pony/version.py` / `pyproject.toml`. Release history: `CHANGELOG.md`.

## Delivered

All v1 capabilities implemented and tested:

- IMAP sync (two-pass plan/execute) and SMTP send (IMAP accounts and local accounts with `[smtp]`).
- Maildir + mbox mirror backends, shared conformance suite. Mbox uses an mmap-based TOC builder with an on-disk sidecar so subsequent opens skip the rebuild.
- SQLite+FTS5 index (diacritic/case-insensitive search) with covering index on `(account_name, folder_name, received_at DESC)`.
- Three-pane Textual TUI: read, compose, search, contacts. Folder-open streams rows in batches via a Textual worker so opening 10k+-row folders never freezes the UI; the message list is single-column and CSS-driven (dim default, unread pops).
- Goto-folder (`G`) fuzzy jump, new-folder (`N`) creation on local and IMAP accounts, F1 keybinding cheatsheet, OSC-driven terminal title push/pop/restore.
- BBDB-compatible contacts with import/export plus in-TUI create/edit/merge.
- MCP server (read-only) on `tinymcp`: stdio standalone, or TCP-bridged to a running TUI via a per-session auth token.
- PyInstaller standalone builds with platform installers.
- TUI flow tests: 13 Pilot-driven tests in `tests/test_tui_flows.py`.
- Textual theme selection via `theme` in `config.toml`, `--theme` CLI flag, and `--list-themes`.
- Mass-deletion confirmation (`>20%` server-side) surfaced per-folder in CLI and TUI plans; `--yes` / `Y` applies them.
- Local-mirror rescan with mtime sidecar cache and a lean storage-key projection on cold scans.
- Scoped `pony reset --account NAME` rebuild path.

## Queue

- **Rebuild-from-mirrors command.** Re-index from mirror bytes (no re-download) is missing. Blocked on synthetic Message-ID rework (below).
- **Synthetic Message-ID rework.** Hash bakes UID (`sync.py`); synthetic IDs can't survive a rebuild.
- **`body_preview` → `body_text` rename.** Column name is stale; defer to next schema bump.
- **Background/periodic sync** (needs exclusive write lock).
- **OAuth.**
- **Browser UI.**
- **POP support.**
- **Multi-machine conflict handling** (beyond current state-based reconciliation).
- **Gmail label multi-folder support** (aggregate folders currently warned + excluded).
- **Per-folder single-transaction sync** (idempotent re-sync covers failures now).
- **TUI coverage gaps:** snapshot tests deferred until UI stabilises; `ContactBrowserScreen` edit/merge and `SyncConfirmScreen` phase transitions not yet Pilot-tested (worker-thread interaction non-trivial).
