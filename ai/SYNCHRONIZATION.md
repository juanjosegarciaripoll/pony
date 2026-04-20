# Synchronization Design

This document describes the sync model. It is authoritative -- the IMAP sync
module must implement this model. Deviations require updating this document
first.

## Guiding principles

- **State, not history.** Correctness from comparing current state on each
  side, not replaying user actions.
- **Idempotency.** Running sync twice on unchanged state produces no mutations.
- **Non-destructive by default.** No permanent deletion without explicit user
  confirmation or trash retention expiry.
- **Explicit conflict surface.** Unresolvable divergences are presented to the
  user, not silently resolved.
- **Message-ID as stable identity.** IMAP UIDs are ephemeral; `Message-ID`
  headers are the durable cross-folder identity.
- **`uid IS NULL` means "expected on server in this folder, not yet confirmed."**
  A local row with a null UID is the sole signal that sync must push the row
  to the server — either by `UID MOVE` (from wherever the server currently
  holds the message) or by `APPEND`.  No separate pending-operations table,
  no status marker: the uid column carries the intent.

## Message identity

IMAP UIDs identify messages within a folder at a given UIDVALIDITY epoch.
`Message-ID` headers are stable across copies, moves, and re-deliveries.
UIDs are used for efficient IMAP access (`UID FETCH`); the local index is
keyed by `(account, folder, message_id)`.

Duplicate `Message-ID` within a folder: second occurrence gets a synthetic ID.
Missing `Message-ID`: deterministic synthetic from SHA256(account + folder +
uid + date + from + subject).

## Schema

```sql
CREATE TABLE folder_sync_state (
    account_name   TEXT    NOT NULL,
    folder_name    TEXT    NOT NULL,
    uid_validity   INTEGER NOT NULL,
    highest_uid    INTEGER NOT NULL,
    uidnext        INTEGER NOT NULL DEFAULT 0,
    highest_modseq INTEGER NOT NULL DEFAULT 0,
    synced_at      TEXT    NOT NULL,
    PRIMARY KEY (account_name, folder_name)
);

CREATE TABLE messages (
    account_name     TEXT    NOT NULL,
    folder_name      TEXT    NOT NULL,
    message_id       TEXT    NOT NULL,
    sender           TEXT    NOT NULL,
    recipients       TEXT    NOT NULL,
    cc               TEXT    NOT NULL,
    subject          TEXT    NOT NULL,
    body_preview     TEXT    NOT NULL,
    storage_key      TEXT    NOT NULL DEFAULT '',
    has_attachments  INTEGER NOT NULL DEFAULT 0,
    local_flags      TEXT    NOT NULL,
    base_flags       TEXT    NOT NULL,
    local_status     TEXT    NOT NULL,
    received_at      TEXT    NOT NULL,
    uid              INTEGER,
    server_flags     TEXT    NOT NULL DEFAULT '',
    extra_imap_flags TEXT    NOT NULL DEFAULT '',
    trashed_at       TEXT,
    synced_at        TEXT,
    PRIMARY KEY (account_name, folder_name, message_id)
);

CREATE UNIQUE INDEX ix_messages_uid
ON messages (account_name, folder_name, uid)
WHERE uid IS NOT NULL;
```

## Folder sync scope

Per-account config controls which folders are synced:

| Key | Default | Meaning |
|---|---|---|
| `include` | `[]` (all) | If non-empty, only matched folders are synced |
| `exclude` | `[]` | Never synced (beats include and read_only) |
| `read_only` | `[]` | Server-to-local only; auto-included when include is set |

All values are Python `re.fullmatch()` patterns.

### Read-only folders

- New messages fetched, flag updates pulled
- Local flag changes NOT pushed to server
- Local deletions restored on next sync (RestoreOp)
- Typical use: Sent, [Gmail]/All Mail, shared mailboxes

## Account-level pre-phase: folder creation

Before any per-folder reconciliation runs, the planner compares the set
of folders the local mirror exposes (``MirrorRepository.list_folders``)
with the set of folders the server returns (``LIST``). Every folder
present locally, passing the sync policy (``folders.should_sync``), and
missing on the server is queued for a server-side ``CREATE``.

This is the sole signal sync needs for locally-created folders to
propagate upstream — whether the user created them explicitly via the
TUI ``N`` action or implicitly by archiving into a folder that didn't
exist yet. Creating is idempotent (skipped when the folder already
exists server-side). ``CREATE`` runs before any ``PushMoveOp``, so a
move that targets a freshly-created folder always has a live
destination.

## STATUS-gated path selection

Every per-folder sync begins with one ``STATUS folder (UIDVALIDITY
UIDNEXT MESSAGES HIGHESTMODSEQ)`` roundtrip — the HIGHESTMODSEQ field
is requested only when the server advertises ``CONDSTORE`` (RFC 7162),
otherwise it is ``None``.  Three conditions on the result select the
path:

**Base stability** (UID set matches the local snapshot):

- ``UIDVALIDITY`` matches the stored ``folder_sync_state``,
- ``UIDNEXT`` matches the stored ``uidnext`` exactly (no new UIDs
  have been assigned server-side — not even "burned" ones that were
  delivered and then expunged or moved away), and
- ``MESSAGES`` equals the count of local rows with ``uid IS NOT NULL``
  (no server-side deletions).

Comparing server ``UIDNEXT`` against the stored ``uidnext`` watermark
— rather than against ``highest_uid + 1`` as in the original Phase 1
draft — is essential: IMAP ``UIDNEXT`` is monotonic and MUST advance
for every delivery, even if the message is subsequently expunged.
Mailboxes with active sieve rules, LMTP fan-out, or server-side moves
routinely accumulate a gap between ``UIDNEXT`` and ``max_surviving_uid
+ 1``, so the derived comparison never matches and the folder
slow-paths forever.

**CONDSTORE condition** (server flags have not changed either):

- ``HIGHESTMODSEQ`` is ``None`` (server does not advertise CONDSTORE —
  we can't tell, so assume match), or
- stored ``highest_modseq`` is ``0`` (first sync after schema
  migration — no watermark yet, same treatment), or
- ``HIGHESTMODSEQ`` equals stored ``highest_modseq``.

### The three paths

**Fast path** — base stability + CONDSTORE condition both hold.
Skip every server FETCH.  Synthesize ``uid_to_mid`` from
``list_folder_uid_to_mid`` so cross-folder move detection still
works.  ``_plan_fast_path_folder`` pushes whatever the user changed
locally using only local state:

- ``PushFlagsOp`` for rows with ``local_flags != base_flags``,
- ``PushDeleteOp`` for trashed rows,
- ``PushAppendOp`` for pending ``uid=NULL`` ACTIVE rows (unless the
  Message-ID is on the server in another folder, in which case that
  folder's plan emits ``PushMoveOp``),
- ``RestoreOp`` for trashed rows in read-only folders.

For a 17k-message quiescent folder this is one roundtrip total.

**Medium path** — base stability holds but CONDSTORE condition fails
(UID set stable, flags changed server-side).  Issue
``UID FETCH 1:* (FLAGS) CHANGEDSINCE stored_modseq`` — returns only
messages whose ``MODSEQ`` has advanced past the stored watermark
(typically a handful, even in a huge folder).  Synthesize
``uid_to_mid`` from the local index; feed to the normal planner.
Step 3 sees fresh server flags and emits ``PullFlagsOp`` /
``MergeFlagsOp`` as needed.

**Slow path** — base stability fails (new UIDs arrived, a UID gone,
or UIDVALIDITY reset).  Full
``FETCH 1:* (FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])`` scan as
described below.

After executing any path, ``folder_sync_state.highest_modseq`` is
updated to the STATUS-time value (from the pre-execute STATUS call)
so the next sync has a watermark.

### Known limitation

Without CONDSTORE support the medium path cannot be taken: the
planner falls back to the Phase 1 behavior, so silent server-side
flag changes are overwritten by concurrent local pushes (or missed
on quiescent folders) until a UID-set change triggers the slow
path.  Tests covering three-way flag merge in this regime force the
slow path by bumping UIDNEXT server-side.

## Per-folder reconciliation algorithm

The six steps below describe the **slow path** in full.  The fast and
medium paths (see *STATUS-gated path selection* above) reuse Steps 4-6
unchanged, but take shortcuts at the top:

- **Fast path** skips Steps 1-3 entirely — the STATUS gate already
  proved the server is unchanged, so there are no deletions, no new
  UIDs, and no flag updates to reconcile.
- **Medium path** skips Steps 1-2 (UID set is stable) and enters
  Step 3 with ``remote`` sourced from the ``CHANGEDSINCE`` response
  for UIDs whose flags advanced, plus the cached ``base_flags`` for
  UIDs whose ``MODSEQ`` has not moved.

```
remote  <- IMAP: SELECT folder, FETCH UID ALL FLAGS
local   <- index: messages WHERE folder = ? AND uid IS NOT NULL
pending <- index (account-wide): messages WHERE uid IS NULL AND status=ACTIVE
           — map msgid -> folder; built once per account before per-folder work

Step 1: Deletions on server
  for uid in local - remote:
    if message_id found in another folder -> move (update local folder)
    else -> server deleted: trash locally (or re-upload if locally modified)

Step 2: New UIDs on server in this folder
  for uid in remote - local:
    mid = message_id for this uid
    if pending[mid] == this folder:
      -> LinkLocalOp: adopt the UID onto the existing uid=NULL row
    elif pending[mid] is another folder G and this folder is writable:
      -> PushMoveOp: UID MOVE on the server from this folder to G
    else:
      -> FetchNewOp: fetch body, ingest into mirror + index
         (handles both genuinely new messages and cross-folder copies —
          duplicate Message-IDs get a synthetic ID via C-7.)

Step 3: Flag reconciliation for surviving messages
  for uid in remote AND local:
    Compare: base_flags (last sync), local_flags (user intent), remote_flags
    - Neither changed -> skip
    - Only server changed -> pull (PullFlagsOp)
    - Only local changed -> push if not read-only (PushFlagsOp)
    - Both changed -> union merge (MergeFlagsOp); push if not read-only
    Special: trashed in read-only folder -> RestoreOp

Step 4: Push local deletions (writable folders only)
  for trashed messages with known uid:
    STORE +FLAGS (\Deleted), EXPUNGE

Step 5: Push local-pending rows (writable folders only)
  for rows in this folder with uid=NULL and status=ACTIVE:
    if mid is on the server in another folder -> skip; that folder's
      Step 2 will emit PushMoveOp and place it here.
    else -> PushAppendOp: APPEND the mirror bytes to this folder.
    (The resulting server UID is adopted on the next sync via
     Step 2's LinkLocalOp branch.)

Step 6: Commit
  Update folder_sync_state: uid_validity, highest_uid, uidnext,
                            highest_modseq, synced_at (uidnext and
                            highest_modseq from the STATUS observed
                            at the start of this folder's sync — not
                            derived from max(remote_uids))
```

### Local moves (archive)

The TUI `A` action archives the selected message by applying a purely
local move:

1. Rename (Maildir) or copy-and-delete (mbox) the mirror file into the
   archive folder's directory.
2. Delete the source index row.
3. Insert a new index row at ``(account, archive_folder, message_id)``
   with ``uid=NULL``, ``local_status=ACTIVE``, and the same ``local_flags``
   / ``base_flags`` as the source.

The message disappears from the source folder and appears in the archive
folder immediately in the TUI.  On the next sync:

- The source folder's Step 2 sees the remote UID, the pending row in the
  archive folder, and emits a ``PushMoveOp`` that runs ``UID MOVE``
  server-side.
- The following sync's archive-folder Step 2 picks up the resulting UID
  and emits ``LinkLocalOp`` to adopt it — no refetch, no duplicate row.

If the server loses the message between archive and sync (another client
purged it), the archive folder's Step 5 emits ``PushAppendOp`` and uploads
the mirror bytes.  The message is never destroyed locally.

## Performance properties

These are load-bearing properties of the sync planner, not just
current numbers.  Code changes that regress them should be treated
as bugs.

- **Fast path is zero-I/O beyond the single STATUS roundtrip.**  When
  the STATUS gate matches (see *STATUS-gated path selection*), the
  planner issues no ``FETCH``, no ``SELECT`` on the server, no full
  folder read of the local index.  A 17k-message quiescent folder
  costs one roundtrip.  Any future fast-path code that calls
  ``fetch_*`` on the session, or that iterates ``list_folder_messages``
  on the index, is a regression.

- **Narrow projections on the hot paths.**  The fast-path planner
  reads rows via ``list_folder_push_candidates`` — a SQL-filtered
  ``WHERE`` that returns only rows needing a push (``PendingPush`` is
  the narrow domain type).  A quiescent folder returns zero rows from
  this query; a folder with three flag changes returns three rows.
  Using the broad ``list_folder_messages`` here reintroduces
  ``_indexed_message_from_row`` cost — three datetime parses + three
  flag-CSV splits per row — and has been measured at 4-5s on a 100k
  mirror.  The slow-path planner (``_plan_folder``) follows the same
  rule: it reads ``list_folder_slow_path_rows`` (``SlowPathRow``)
  rather than ``list_folder_messages`` so the per-row cost scales
  with the seven columns Steps 1/3/4/5 actually consult, not all
  nineteen.  The medium path's baseline-flag seed goes through
  ``list_folder_base_flags`` for the same reason.  Any future sync
  planner call that iterates ``list_folder_messages`` or
  ``list_folder_messages_with_uid`` is a regression.

- **Account-wide maps are lazy and gated.**  The cross-folder
  mid→folders map (``list_mid_folders_for_account``) is the only
  account-scoped query on the plan path and is only needed by the
  slow-path's Step 1 move-detection branch *when an aggregate folder
  (Gmail-style ``[Gmail]/All Mail``) is synced*.  Without a synced
  aggregate, the same message cannot legitimately appear in two
  folders' previous-sync snapshots, so the planner skips the call
  entirely and treats ``prev_folders`` as empty.  When the call is
  required, it is built by a cached closure on first use.  The
  cross-folder ``remote_mid_map`` is also gated: fast- and
  medium-path folders skip their ``_merge_mid_map`` contribution on
  quiescent accounts (no pending ``uid=NULL`` rows), since those
  folders' UIDs cannot be reached by any slow-path folder's Step 1
  (a server-side move bumps both folders' ``MESSAGES`` count onto
  the slow path).  New account-wide maps added to the planner should
  follow the same lazy + gated pattern.

- **Server state is read via STATUS, not FETCH, whenever possible.**
  One ``STATUS folder (UIDVALIDITY UIDNEXT MESSAGES HIGHESTMODSEQ)``
  replaces a ``FETCH 1:*`` on every quiescent folder.  The STATUS
  watermarks persisted in ``folder_sync_state`` — ``uid_validity``,
  ``uidnext``, ``highest_modseq`` — must be kept as the server's
  observed values (not derived from ``max(remote_uids)``); a derived
  ``uidnext`` breaks the gate on any mailbox where UIDs have been
  burned.

## Flag merge policy

Three-way merge with **union** policy: any flag set on either side is set on
both. Exception: `\Deleted` is escalated to conflict resolution, not merged.

Custom server flags (`$Important`, `$Junk`, etc.) are preserved in
`extra_imap_flags` and included in every `STORE FLAGS` call.

## Conflict taxonomy

| ID | Scenario | Resolution |
|---|---|---|
| C-1 | Server deleted, locally modified | Re-upload via APPEND (ReUploadOp) |
| C-2 | Locally trashed, server changed flags | Cancel deletion, restore with new flags (RestoreOp + PullFlagsOp) |
| C-3 | Both sides changed flags | Union merge (MergeFlagsOp) |
| C-4 | UIDVALIDITY reset | Clear UIDs, full resync by Message-ID |
| C-5 | Partial sync interrupted | Resume safely (state-based, idempotent) |
| C-6 | Mass deletion (>20% gone) | Halt folder, require confirmation |
| C-7 | Duplicate Message-ID | Synthetic ID for second occurrence |
| C-8 | Missing Message-ID | Deterministic synthetic from content hash |
| C-9 | Local move (archive) pending | `uid=NULL` row in target; Step 2 emits PushMoveOp in source or PushAppendOp if server has no copy |

## Trash workflow

1. **Trashed** (`local_status = 'trashed'`): marked locally, retained in
   mirror. Next sync pushes EXPUNGE to server (writable) or restores (read-only).
2. **Purged**: after server ACK or `trash_retention_days` expiry, index row
   and mirror file are removed.

## Garbage collection (runs at start of each sync)

- Stale accounts (removed from config): index data purged
- Stale folders (gone from server): sync state cleaned
- Expired trash: messages trashed > `trash_retention_days` permanently deleted

## Progress reporting

`ProgressInfo(message, current, total)` callbacks for:
- Planning: per-folder scanning progress
- Execution: per-operation progress
- TUI: ProgressBar widget; CLI: `\r`-overwriting counter line

## Known limitations

Behaviours that are understood and intentionally deferred. New work in
the sync engine should not regress these without a plan to fix them.

- **ReUploadOp is not idempotent under a crash between APPEND and
  commit.** `_execute_reupload` calls `session.append_message(...)`
  before the index transaction clears the old `uid`. If the process
  dies after the APPEND lands on the server but before the enclosing
  `with self._index.connection()` block commits, the next sync sees
  the same C-1 state (server-deleted, locally-modified) and re-emits
  ReUploadOp, producing a duplicate on the server. A proper fix needs
  UIDPLUS-based dedup on retry or an explicit two-phase marker on the
  row.

- **Messages in excluded server folders are invisible to dedup.**
  `remote_mid_map` is built only from folders that pass
  `folder_policy.should_sync`. If a message with Message-ID `M` lives
  on the server in an excluded folder (typically `[Gmail]/All Mail`)
  and a pending `uid=NULL` row for `M` exists locally in a synced
  folder, Step 5 sees `M` as "not on the server" and emits
  `PushAppendOp`, producing a second copy on the server. Aggregate
  folders included in the sync already trigger a related warning.

- **Per-folder sync is not a single transaction.** Reconciliation is
  idempotent, so a mid-folder crash is recovered by re-running sync.
  Batched writes via `connection()` shrink the failure window but do
  not eliminate partial-folder state.

- **Move detection and `FetchNewOp` can double-ingest under specific
  interleavings.** The index stays correct; the old mirror file is
  orphaned and picked up by the mirror integrity scan.

- **Same message in multiple folders (Gmail labels).** Full multi-folder
  support is deferred; aggregate folders should be excluded. A warning
  is emitted when one is detected.

- **No exclusive write lock during sync.** The TUI blocks on the `G`
  sync action today; the constraint needs to be revisited when
  background sync is added.

- **Stale plan applied after a confirmation delay.** State-based
  reconciliation self-corrects on the next pass; per-op try/except
  keeps stale operations from cascading.

- **SQLite write contention** (single-process v1, 5-second lock
  timeout, per-op try/except) is covered by the existing retry layer.
  Revisit when a second writer is introduced.

- **Silent server flag changes without CONDSTORE.** On servers that
  do not advertise ``CONDSTORE`` (RFC 7162), the STATUS fast-path
  cannot observe server flag changes, so a concurrent remote flag
  change on a locally-modified message is overwritten by the local
  ``PushFlagsOp`` (and pure server-side changes on quiescent folders
  are missed until a UID-set change forces the slow path).  On
  CONDSTORE-capable servers the medium path closes this gap.  See
  *STATUS-gated path selection* above.
