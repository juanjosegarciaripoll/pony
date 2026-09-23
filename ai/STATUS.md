# Scope & Status

Standalone open-source Python 3.13 MUA. Clear mail workflow, flexible local
storage, strong offline behavior. Version: `src/pony/version.py` /
`pyproject.toml`. Release history: `CHANGELOG.md`.

## Capabilities

1. Mail synchronization (IMAP)
2. Mail reading (TUI)
3. Mail composing (SMTP + attachments + Markdown)
4. Mail search (FTS5)

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
- Background/periodic sync: non-blocking `ctrl+g` worker that auto-confirms every folder, plus a config-gated periodic timer (`background_sync_enabled` / `background_sync_interval_seconds`).
- RFC 5322 threading on replies (`In-Reply-To` / `References`), carried through a draft round-trip.
- Sending resolves credentials through the same provider as sync, so every `credentials_source` works from the composer; local accounts with `[smtp]` included.
- Backend/presentation split: `accounts.py`, `mailbox_ops.py`, `composer.py` are headless and enforced as such by `tests/test_layering.py`.

## Queue

- **Rebuild-from-mirrors command.** Re-index from mirror bytes (no re-download) is missing. Blocked on synthetic Message-ID rework (below).
- **Synthetic Message-ID rework.** Hash bakes UID (`sync.py`); synthetic IDs can't survive a rebuild.
- **`body_preview` → `body_text` rename.** Column name is stale; defer to next schema bump.
- **Per-folder single-transaction sync** (idempotent re-sync covers failures now).
- **Gmail label multi-folder support** (aggregate folders currently warned + excluded).
- **TUI coverage gaps:** snapshot tests deferred until UI stabilises; `ContactBrowserScreen` edit/merge and `SyncConfirmScreen` phase transitions not yet Pilot-tested (worker-thread interaction non-trivial).
- **Code simplification backlog:** see `ai/SIMPLIFICATIONS.md`.
- **imapclient 4.x: `1:*` rejected by `fetch()`.** `pyproject.toml` caps the
  dependency at `<4` because 4.0 filters every fetch reply through
  `to_ints(messages)`, which cannot express a sequence set: `fetch("1:*", ...)`
  builds and sends a correct `UID FETCH 1:*`, the server answers, and the reply
  filter then raises `ValueError: invalid literal for int() with base 10: '1:*'`.
  This aborts planning for every folder on the slow or medium path, so an
  install resolving 4.x syncs nothing. Only `imap_client.py`
  `fetch_uid_to_message_id` and `fetch_flags_changed_since` pass sequence sets;
  `fetch_flags` and `fetch_messages_batch` already enumerate UIDs and are
  4.x-clean.

  Upstream: [mjs/imapclient#666][ic666], opened 2026-09-20, no maintainer reply
  as of 2026-09-23. It arrived with [PR #647][ic647] (merged 2026-09-07), whose
  actual goal — dropping unsolicited FETCH responses for messages other than the
  ones requested — is sound; only the `to_ints` implementation is wrong. 4.x is
  young (4.0.0 on 2026-09-11, 4.1.0 on 2026-09-18), so a fix may well land.

  **Re-check before doing any work here:** if upstream restores sequence sets,
  lift the cap and nothing else is needed. If it does not, the port is to take
  the UID set from `UID SEARCH` and enumerate — batched, since
  `CSIC/Archives.2026`'s 15,934 UIDs comma-join to an 86 KB command argument,
  i.e. ~32 round trips in place of one. The CHANGEDSINCE call should instead use
  `UID SEARCH MODSEQ <n>` (RFC 7162), which returns only the changed UIDs and is
  cheaper than today's `1:*` on either major version.

  Note no test can catch a recurrence: `FakeImapSession` never reaches
  imapclient, and the live Dovecot suite runs against whatever the lockfile
  pins. Verifying a lift means installing 4.x deliberately and running
  `PONY_LIVE_IMAP=1`.

[ic666]: https://github.com/mjs/imapclient/issues/666
[ic647]: https://github.com/mjs/imapclient/pull/647

## Deferred (out of scope for now)

- POP support
- OAuth
- Browser UI
- Multi-machine conflict handling (beyond current state-based reconciliation)
- Aggressive auto remote mutations
