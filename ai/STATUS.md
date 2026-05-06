# Project Status

Version: `src/pony/version.py` / `pyproject.toml`. Release history: `CHANGELOG.md`.

## Delivered

All v1 capabilities implemented and tested:

- IMAP sync (two-pass plan/execute) and SMTP send.
- Maildir + mbox mirror backends, shared conformance suite.
- SQLite+FTS5 index (diacritic/case-insensitive search).
- Three-pane Textual TUI: read, compose, search, contacts.
- BBDB-compatible contacts with import/export.
- MCP server (stdio + Streamable HTTP), read-only.
- PyInstaller standalone builds with platform installers.
- TUI flow tests: 12 Pilot-driven tests in `tests/test_tui_flows.py`.
- Textual theme selection via `theme` in `config.toml`, `--theme` CLI flag, and `--list-themes`.

## Queue

- **DataTable cost on large folders.** `MessageListPanel.load_folder` ~300 ms on 17k rows (down from ~1.3 s); bottleneck is Textual's `add_row` loop (no built-in virtualization). Options: (1) `@work(thread=True)` + batched `call_from_thread` — same time, better perceived latency; (2) pagination/windowing — real fix for 50k+ rows, non-trivial refactor; (3) compound index on `(account_name, folder_name, received_at DESC)` — drops sort from ~200 ms to tens of ms, needs schema bump.
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
