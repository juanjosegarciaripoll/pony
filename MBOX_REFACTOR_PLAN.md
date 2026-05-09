# Speeding up mailbox handling (rescan in particular)

## Context

`pony rescan` and related local-mirror operations feel slow on real mailboxes. Recent commits already chipped away at the obvious wins (`21ab80c` skipped full-row hydration on cold local-mirror scans; `1b68e2f` fixed projection regex and added `--force`). But the CLI `run_rescan` path still has unbatched per-message I/O and SQLite commits, while a faster sibling (`rescan_local_account`) already exists in `storage_indexing.py` and is not being used by the CLI.

This plan inventories the remaining bottlenecks, points at the existing primitives that should be reused, and proposes a layered fix.

## Findings (ranked by impact / risk)

### 1. `cli.run_rescan` does not wrap its loop in a SQLite transaction — biggest single win
- **Where:** `src/pony/cli.py:1174–1273` (loop at ~`1212–1241`).
- **Symptom:** Every `index.upsert_message()` autocommits via `_use()` (`src/pony/index_store.py:316–320`). For a 10 k-message folder that's 10 k commits.
- **Contrast:** Sync execution already wraps a full folder in `with self._index.connection():` (`src/pony/sync.py:~1535`). Same primitive applies here.
- **Estimated impact:** 10–50× on large folders. Effort: minutes. Risk: minimal.

### 2. `run_rescan` ignores the optimized `rescan_local_account` path
- **Where:** `src/pony/storage_indexing.py:99–205` already implements:
  - lean `list_folder_storage_keys()` (`index_store.py:698`) — no full-row hydration
  - per-folder `mtime` skip (`storage_indexing.py:142–151`) — entire folders bypassed when unchanged
- **The CLI re-implements its own loop** in `cli.py` instead of calling this.
- **Estimated impact:** Cold rescan of an unchanged tree drops from O(messages) to O(folders). Effort: a couple of hours of plumbing. Risk: low (path is already used elsewhere).

### 3. Per-message storage reads, especially for mbox
- **Where:** `MboxMirrorRepository.list_messages` and `get_message_bytes` (`src/pony/storage.py:352–366`).
- **Symptom:** mbox key enumeration parses every From-line, then `get_message_bytes` seeks again per message.
- **Opportunity:** stream a single mbox pass that yields `(storage_key, raw_bytes)` together; reuse it in rescan. Maildir is already cheap but `_find_message_file` falls back to `glob` (`storage.py:137–166`) — exact-match fast path covers the common case.
- **Estimated impact:** 2–3× on mbox users. Effort: small. Risk: low (storage layer has a conformance suite).

### 4. Projection check could short-circuit on unchanged file mtime
- **Where:** projection diff in `cli.py:1228` and `message_projection.py`.
- **Today:** even when nothing changed we still read bytes and parse headers to compare 7 fields.
- **Opportunity:** if the mirror file's mtime ≤ index `mtime` (already tracked per folder; could be tracked per message or just trusted at folder level), skip the read entirely. Layered on top of #2.
- **Estimated impact:** 2–5× on archive folders that rarely change. Effort: medium. Risk: medium — needs care around flag-only changes in Maildir filenames.

### 5. Pipeline projection and SQLite writes
- Projection is CPU-bound; SQLite writes are I/O-bound. A bounded queue between a parser thread and the writer (mirroring `sync.py`'s producer/consumer at `1499–1526`) would overlap them.
- **Estimated impact:** ~1.5–2×. Effort: higher. Risk: medium. Worth doing only after #1 and #2 land.

## Already optimized — don't redo

- IMAP fetch batching (25-UID groups) in `sync.py:1499–1526` and `imap_client.py:370–400`.
- STATUS-gated fast/medium/slow sync path (`sync.py:653–700`).
- Lean projection list via `list_folder_storage_keys` (`index_store.py:698`).
- Folder-mtime skip in `rescan_local_account`.

## Recommended sequencing

1. ~~**Quick win:** wrap `run_rescan`'s loop in `with index.connection():`. Verify with a stopwatch on a known mailbox.~~ **Done** (`b333a7f`).
2. ~~**Consolidation:** route `cli.run_rescan` through `storage_indexing.rescan_local_account`, deleting the duplicated loop. Keep `--force` semantics by passing through a flag that disables the mtime skip.~~ **Done** — `rescan_local_account` gained `reproject_existing` / `force_reproject`; CLI now uses the shared engine, the folder-mtime cache, and prunes orphan rows.
3. **mbox single-pass read** in storage layer, only if profiling after #2 still shows mbox dominating.
4. Defer #4 and #5 until #1–#3 are measured; they may not be needed.

## Critical files

- `src/pony/cli.py` (rescan command)
- `src/pony/storage_indexing.py` (target path)
- `src/pony/index_store.py` (connection / transaction primitives)
- `src/pony/storage.py` (Maildir/mbox reads)
- `src/pony/message_projection.py` (header parsing)
- `src/pony/sync.py` (reference patterns: connection context, producer/consumer)

## Verification

- Microbench: time `pony rescan` against a representative account before and after each step (record message count + storage backend).
- `pytest` — full suite, including storage conformance.
- `ruff check`, `ruff format --check`, `mypy`, `basedpyright`.
- Manual: run rescan twice in a row; second run should be near-instant after step #2.
- Targeted: run on an mbox account specifically to validate step #3.
