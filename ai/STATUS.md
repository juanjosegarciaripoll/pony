# Scope & Status

Standalone open-source Python 3.13 MUA. Clear mail workflow, flexible local
storage, strong offline behavior. Version: `src/pony/version.py` /
`pyproject.toml`. Release history: `CHANGELOG.md`.

## Capabilities

1. Mail synchronization (IMAP)
2. Mail reading (TUI and browser)
3. Mail composing (SMTP + attachments + Markdown)
4. Mail search (FTS5)
5. Calendar (CalDAV) in both front ends
6. Contacts (BBDB-compatible) in both front ends

## Delivered

All v1 capabilities implemented and tested:

- IMAP sync (two-pass plan/execute) and SMTP send (IMAP accounts and local accounts with `[smtp]`).
- Maildir + mbox mirror backends, shared conformance suite. Mbox uses an mmap-based TOC builder with an on-disk sidecar so subsequent opens skip the rebuild.
- SQLite+FTS5 index (diacritic/case-insensitive search) with covering index on `(account_name, folder_name, received_at DESC)`.
- Three-pane Textual TUI: read, compose, search, contacts. Folder-open makes one SQL call for the folder's summaries and streams their insertion into the table in batches via a Textual worker, so a 10k+-row folder paints progressively; the message list is single-column and CSS-driven (dim default, unread pops).
- Goto-folder (`G`) fuzzy jump, new-folder (`N`) creation on local and IMAP accounts, F1 keybinding cheatsheet, OSC-driven terminal title push/pop/restore.
- BBDB-compatible contacts with import/export plus in-TUI create/edit/merge.
- MCP server (read-only) on `tinymcp`: stdio standalone, or TCP-bridged to a running TUI via a per-session auth token.
- PyInstaller standalone builds with platform installers.
- TUI flow tests: 13 Pilot-driven tests in `tests/test_tui_flows.py`.
- Textual theme selection via `theme` in `config.toml`, `--theme` CLI flag, and `--list-themes`.
- Two per-folder safety halts surfaced in the CLI, TUI and browser plans: mass deletion (`>20%` of known UIDs gone server-side) and mass upload (`APPEND`s over half the folder, or 50 outright). `--yes` / `Y` / the browser's confirm applies them; the paths with nobody to ask skip a halted folder.
- Local-mirror rescan with mtime sidecar cache and a lean storage-key projection on cold scans.
- Scoped `pony reset --account NAME` rebuild path.
- `pony reindex`: index rows and folder watermarks rebuilt from the mirror on disk, with no server, by reading each message's handle out of its Maildir filename (`,V=,U=`). A file whose name carries no handle, or one from a superseded epoch, is indexed as local-only, which is what makes the next sync upload it; a duplicate handle in one folder is reported and left unindexed.
- Background/periodic sync: non-blocking `ctrl+g` run that confirms every safe folder and skips one that tripped a safety halt, plus a config-gated periodic thread (`background_sync_enabled` / `background_sync_interval_seconds`). Both halves schedule themselves through `chronos.scheduler.PeriodicSync`, owned by the application rather than by a screen, so the cadence does not depend on which subsystem is in front.
- RFC 5322 threading on replies (`In-Reply-To` / `References`), carried through a draft round-trip.
- Sending resolves credentials through the same provider as sync, so every `credentials_source` works from the composer; local accounts with `[smtp]` included.
- Backend/presentation split: `accounts.py`, `mailbox_ops.py`, `composer.py` are headless and enforced as such by `tests/test_layering.py`.
- Model/view split across both halves: operations return `chronos.outcome.Outcome` from action modules (`mailbox_actions`, `delivery`, `contact_actions`, `invitation_actions`, `chronos.event_actions`), projections live in `pony.views` / `chronos.views` (grid geometry included), and the keymaps are data (`pony.keymap`, `chronos.keymap`) that each front end converts.
- Browser front end (`pony web`, extra `pony[web]`): the terminal's feature set over HTTP — folder tree, list and reader with the message as text, archive/trash/flag/mark-read, move, copy, new folder, goto, search, harvest, compose and send (reply, reply-all, forward, resumed draft, attachments uploaded as bytes), answering invitations, syncing (with the server's own one-at-a-time slot and the periodic thread), the contacts browser with delete and merge, and the calendar's agenda, day, multi-day and month views with its event form. Full-screen pane layout, one pane at a time on a phone, loopback plus token by default.

## Queue

- **A UIDVALIDITY reset duplicates two messages that share a Message-ID.**
  Not the reset's own doing: a UID whose Message-ID names several rows
  falls through to the body fetch, where `_adopt_existing_row` compares
  the projection — sender, subject and date — and two indistinguishable
  copies match several rows there too, so it inserts rather than
  guessing. The folder then holds both the old UID-less rows and the
  new ones, and the *next* pass, no longer a reset, plans a
  `PushAppendOp` for each orphan: a folder with two identical copies
  comes back with four, on the server. Reproduced with
  `tests/test_sync.py::UidValidityResetCostTestCase::test_a_shared_message_id_is_downloaded_rather_than_guessed`
  (2 rows → 4 rows → 4 on the server). The fix is in the fetch path, not
  the planner: where several UID-less rows are provably interchangeable,
  claim one per fetched message within the pass instead of inserting a
  copy. Pairing provably identical rows arbitrarily is not the same as
  guessing which message a UID is.
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
- Multi-user serving (one config file, one credential store — several
  people means an instance each)
- A cross-process mail sync lock. The calendar has one
  (`chronos.locking`); the mail half never has, and `pony sync`, the TUI
  and `pony web` can each start a sync. The web server holds one slot of
  its own, so two tabs cannot overlap, but two processes still can — as
  they always could. Deliberate: it is the user's machine and their call.
- Editing a message body in `$EDITOR` from the browser (the only key in
  the keymap the browser reports as terminal-only)
- Attachment *previews* in the browser; they download instead
- Multi-machine conflict handling (beyond current state-based reconciliation)
- Aggressive auto remote mutations
