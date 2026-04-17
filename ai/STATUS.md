# Project Status

## Current version

0.2.0 (adds archive action and `uid=NULL`-based local-move sync)

## What's done

All v1 features are implemented and tested (250 tests, 2 skipped):

- **Phases 1-5**: Repository foundation, domain models, config, Maildir/mbox
  storage, SQLite index with search, IMAP sync engine
- **Phase 6**: Three-pane TUI (Textual): folder panel, message list, message
  view, sync from TUI, flag operations, attachments
- **Phase 7**: Compose/reply/forward, SMTP send, drafts, search UI with query
  parser
- **Phase 8**: Markdown composition (ctrl+x m toggle, multipart/alternative)
- **Phase 9**: `pony doctor` with mirror integrity scan, fixture corpora,
  user-facing docs, cross-platform verification, LICENSE
- **Phases 10-12**: Person-centric contacts with BBDB import/export, contacts
  browser/editor TUI (search, mark, delete, merge, edit)
- **Phase 13**: Unified message table (merged `messages` + `message_server_state`)
- **Phase 14**: Batched SQLite transactions (`connection()` context manager)
- **Phase 15**: Sync progress reporting (ProgressInfo, TUI progress bar, CLI
  counter)
- **Phase 16**: TUI binding isolation (bindings moved from PonyApp to
  MainScreen, three App classes formalized, SyncConfirmScreen uses callback)
- **Phase 17**: Archive action (`A` key) + generalised local-move sync.
  `uid IS NULL` is now the canonical signal for "push this row to the
  server." New plan ops: `PushMoveOp`, `PushAppendOp`, `LinkLocalOp`.
  Per-account `archive_folder` config. Maildir and mbox both support
  cross-folder move of mirror files. Uses RFC 6851 `UID MOVE` when
  supported, falls back to `COPY` + `\Deleted` + `EXPUNGE` otherwise.
- **Phase 18**: Local folder creation. `N` key opens a one-line dialog;
  the folder is created in the mirror immediately. Sync compares mirror
  folders against server folders at the top of the execution pass and
  issues `IMAP CREATE` for any that are local-only — subsuming the
  archive-folder auto-create path. `MirrorRepository.create_folder` and
  `ImapClientSession.create_folder` added; both are idempotent.

## Infrastructure

- MkDocs Material documentation site with GitHub Pages deployment
- PyInstaller multi-platform release builds (Linux, macOS, Windows)
- Versioning: pyproject.toml + version.py, release GitHub Action with
  changelog stamping
- Quality: ruff + mypy + basedpyright + pytest

## Future directions (not yet planned)

These are potential next steps, not commitments:

- Background / periodic sync
- OAuth authentication
- Browser-based reader/composer UI
- POP support
- Multi-machine conflict handling
- Full multi-folder support for Gmail labels
- Exclusive write lock during sync (for background sync)
- Per-folder transaction scope (currently idempotent re-sync)
