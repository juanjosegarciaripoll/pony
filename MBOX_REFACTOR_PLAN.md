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

### 3. Per-message storage reads, especially for mbox — **measured, not worth doing**
- **Where:** `MboxMirrorRepository.list_messages` and `get_message_bytes` (`src/pony/storage.py:352–366`).
- **Hypothesis:** mbox key enumeration parses every From-line, then `get_message_bytes` seeks again per message; streaming a single pass yielding `(storage_key, raw_bytes)` would be 2–3× faster.
- **Measured on a real 13.8 GB / 85 945-message mbox account** (12 folders, sizes 0–3 GB):
  - `list_messages` (toc generation, all folders): **156 s**
  - `get_message_bytes` per-key, summed across all messages: **7.8 s** (~91 µs/call)
  - Best-case raw sequential read of every mbox file: **6.7 s**
  - Per-key path is only **1.2× slower than a raw `cat`** — the toc cache makes individual reads near-optimal already.
- **Conclusion:** the **20×** cost is `list_messages`/toc generation, not per-key reads. Phase 3's optimistic ceiling saves ~1.1 s out of 164 s on a *full* re-projection. Phase 2's folder-mtime skip already brings unchanged folders to **0 s**, which is the dominant win on a steady-state account. Phase 3 abandoned.

### 4. Projection check could short-circuit on unchanged file mtime
- **Where:** projection diff in `cli.py:1228` and `message_projection.py`.
- **Today:** even when nothing changed we still read bytes and parse headers to compare 7 fields.
- **Opportunity:** if the mirror file's mtime ≤ index `mtime` (already tracked per folder; could be tracked per message or just trusted at folder level), skip the read entirely. Layered on top of #2.
- **Estimated impact:** 2–5× on archive folders that rarely change. Effort: medium. Risk: medium — needs care around flag-only changes in Maildir filenames.

### 5. Pipeline projection and SQLite writes
- Projection is CPU-bound; SQLite writes are I/O-bound. A bounded queue between a parser thread and the writer (mirroring `sync.py`'s producer/consumer at `1499–1526`) would overlap them.
- **Estimated impact:** ~1.5–2×. Effort: higher. Risk: medium. Worth doing only after #1 and #2 land.

### 6. Persistent mbox TOC + faster builder — TUI folder-open freeze

- **Symptom:** Opening a large mbox-backed folder in the TUI freezes for 12–25 s. Occurs once per folder per session.
- **Diagnosis (Pilot-driven pyinstrument trace, Old/Archives-2015, 6 398 messages):**
  - `MessageListPanel.load_folder` itself is fast (~700 ms total).
  - The freeze is in the auto-preview path: adding the first row highlights it → `MessageSelected` → `MessageViewPanel.load_message` → `MboxMirrorRepository.get_message_bytes` → first access to that mbox file in this session → `mailbox.mbox._generate_toc` scans the entire file.
  - Profile slice: `_generate_toc` 24.7 s, of which `BufferedRandom.tell` 10.5 s and the Python loop 7.8 s.
- **Why it isn't covered by #3:** that finding measured the steady state where the TOC was already built. The TUI hits cold TOC every session because the mtime-skip in rescan correctly *avoids* generating the TOC for unchanged folders, so it's deferred to first read. Auto-preview makes "first read" happen on every folder click.
- **Why option (1) — suppress auto-preview — was rejected:** it would diverge from the Maildir behaviour where auto-preview is instant and intended UX. The fix should make mbox reads as cheap as Maildir, not change the panel's contract.
- **Plan: A + C, composed.**
  - **C — faster TOC builder.** Replace stdlib's `tell()`-per-line scan with an `mmap` + bytes scan for `\nFrom ` boundaries. Returns a `dict[int, tuple[int, int]]` matching `mbox._generate_toc` semantics. Expected: 5–10× speedup on the cold path (the profile shows `tell()` is ~40 % of the work). Verified against stdlib output on a representative mbox before adoption.
  - **A — sidecar TOC cache.** After the TOC is built (by C, by rescan, by any path), persist it next to the mbox as a small struct-packed binary file (header: `(size, mtime_ns, count)`; body: `[key, start, stop]*`). On `_open_mbox`, if the sidecar's `(size, mtime_ns)` matches the mbox file, load it directly into `mbox._toc` and `mbox._next_key` and skip generation entirely. Invalidate (delete or rewrite) on every mutating call already wired through this module: `set_flags`, `delete_message`, `move_message_to_folder`, and any path that calls `mbox.flush()`.
  - Together: cold first build is 5–10× faster (C); every subsequent session, every untouched folder, and steady state across rescans is **0 s** (A).
- **Effort:** small. ~30–50 LOC for A in `storage.py`, ~20–30 LOC for C plus a conformance test that compares its output to `_generate_toc` on a fixture mbox.
- **Risk:** low. Touches `mbox._toc` (private stdlib attr) — already accessed by `list_messages` here. Sidecar invalidation hinges on `(size, mtime_ns)`; external editors that rewrite the file change both, so cache becomes stale safely.
- **Verification:**
  - Unit/conformance: `_build_mbox_toc(path)` output matches `mailbox.mbox._generate_toc` byte-for-byte on a multi-message fixture.
  - Sidecar round-trip test: write/read produces identical `_toc`; mismatched `(size, mtime_ns)` is rejected.
  - Mutation invalidation: after `set_flags`/`delete`/`move`, sidecar reflects the new TOC or is removed; next open does not load stale offsets.
  - Targeted measurement: open Old/Archives-2015 in the TUI on a clean session — first open after rescan should drop from ~12 s to <500 ms; subsequent opens unchanged (already cached in process).

## Already optimized — don't redo

- IMAP fetch batching (25-UID groups) in `sync.py:1499–1526` and `imap_client.py:370–400`.
- STATUS-gated fast/medium/slow sync path (`sync.py:653–700`).
- Lean projection list via `list_folder_storage_keys` (`index_store.py:698`).
- Folder-mtime skip in `rescan_local_account`.

## Recommended sequencing

1. ~~**Quick win:** wrap `run_rescan`'s loop in `with index.connection():`. Verify with a stopwatch on a known mailbox.~~ **Done** (`b333a7f`).
2. ~~**Consolidation:** route `cli.run_rescan` through `storage_indexing.rescan_local_account`, deleting the duplicated loop. Keep `--force` semantics by passing through a flag that disables the mtime skip.~~ **Done** (`e822ab6`) — `rescan_local_account` gained `reproject_existing` / `force_reproject`; CLI now uses the shared engine, the folder-mtime cache, and prunes orphan rows.
3. ~~**mbox single-pass read** in storage layer.~~ **Abandoned after profiling** — see finding #3. Per-key reads are already near-optimal; the dominant cost is toc generation, which phase 2's mtime skip already bypasses for unchanged folders.
4. **Persistent mbox TOC + faster builder (#6).** Land C (faster builder) and A (sidecar cache) together. Eliminates the 12–25 s TUI freeze on first open of a large mbox folder per session. See #6 for plan.
5. Defer #4 (projection mtime short-circuit) and #5 (pipelining) until a future regression motivates them. After phases 1–2, the steady-state rescan on a 13.8 GB mbox account skips most folders entirely; the remaining cost is paid only on folders that actually changed.

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
- Targeted (mbox): one-off micro-benchmark using the real configured Old archive (12 folders, 13.8 GB, 85 945 messages). Compares `list_messages` cost, summed `get_message_bytes` cost, and a raw-read floor. Re-run only if a regression is suspected — the storage layer is read-only here.
