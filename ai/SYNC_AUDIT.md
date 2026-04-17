# Sync Engine Audit

Red-team analysis of `SYNCHRONIZATION.md` vs the implementation in
`sync.py`, `index_store.py`, and `imap_client.py`. Each issue is tagged
**BUG**, **GAP** (spec feature not implemented), or **RISK** (robustness
concern not in the spec).

Status: `[x]` = fixed, `[-]` = deferred by design.

## Fixed issues

- **1a BUG** Duplicate Message-ID within a folder (C-7). Within-folder dedup
  in `_plan_folders`; second occurrence gets synthetic ID during fetch.

- **1b BUG** Duplicate Message-ID across folders. Ambiguous mids removed from
  `remote_mid_map`; move detection checks `local_mid_folders` to avoid false
  moves.

- **1c RISK** Message-ID changes between syncs. Step 3 falls back to fresh
  mid from `uid_to_mid` when stored mid yields no index row. Logs warning.

- **2a RISK** UIDVALIDITY = 0. Warning logged when UIDVALIDITY <= 0.

- **3a BUG** `\Deleted` not excluded from union merge. `_merge_flags` strips
  `\Deleted` before merging.

- **3b GAP** Custom/unknown IMAP flags lost. `_parse_imap_flags` returns
  `(known, extra)`. Extra stored in `extra_imap_flags`, included in STORE.

- **3c RISK** Both sides remove all flags = spurious conflict. Conflict only
  reported when both sides changed AND disagree.

- **3d BUG** base_flags vs server_flags comparison. Step 3 now uses
  `local_row.base_flags` as baseline.

- **4a GAP** C-1: server-delete + local-modify. Locally-modified messages
  re-uploaded via APPEND (ReUploadOp). Unmodified messages trash normally.

- **4b GAP** C-2: local-trash + server-flag-change. Deletion cancelled,
  message restored with updated flags (RestoreOp + PullFlagsOp).

- **4c GAP** C-6: mass-deletion safety halt. `needs_confirmation` set when
  >20% of known UIDs disappear (minimum 5).

- **4d GAP** Trash retention garbage collection. `trashed_at` timestamp;
  `_run_trash_gc()` purges expired messages.

- **4e RISK** PushDeleteOp with server_uid = None. Logged; behavior correct.

- **5b BUG** Per-message fetch failure kills folder. Each op wrapped in
  try/except via `_execute_single_op`.

- **8a RISK** Modified UTF-7 folder names. `_decode_imap_utf7` /
  `_encode_imap_utf7` added for LIST/SELECT/APPEND/STATUS.

- **8b RISK** Folder names with path separators. `_sanitize_for_path` replaces
  illegal characters with dots.

- **8c RISK** Zero-byte messages. `_fetch_and_ingest` returns early with
  warning for empty raw bytes.

## Deferred by design

- **2b RISK** UIDVALIDITY reset with pending local modifications. Not a
  problem: upsert on (account, folder, message_id) overwrites old row.

- **6a GAP** Per-folder sync not one transaction. Idempotent re-sync makes
  this safe for v1. Batched transactions via `connection()` reduce the window.

- **7a RISK** Move detection + FetchNewOp double-ingest. Low impact: index
  correct, old mirror file orphaned. Covered by mirror integrity scan.

- **7b GAP** Same message in multiple folders (Gmail labels). Recommend
  excluding aggregate folders. Aggregate folder detection warns. Full
  multi-folder support deferred.

- **9a GAP** No exclusive write lock during sync. TUI blocks during `G` sync.
  Revisit when background sync is added.

- **9b RISK** Stale plan applied after confirmation delay. State-based
  reconciliation self-corrects. Stale ops fail safely (per-op try/except).

- **5a RISK** Connection lost mid-folder. Idempotent re-sync handles this.

- **5c RISK** SQLite write failure. Single-process in v1, 5s lock timeout,
  per-op try/except catches and logs.

- **10a RISK** Folder include patterns matching nothing. Log line sufficient.
