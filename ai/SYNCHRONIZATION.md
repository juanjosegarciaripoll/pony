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

## Per-folder reconciliation algorithm

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
    elif mid already in index (in any other folder, with a valid UID):
      -> copy/second folder: add an index row for this folder
    else:
      -> FetchNewOp: fetch body, ingest into mirror + index

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
  Update folder_sync_state: highest_uid, synced_at
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

Behaviours that are understood, surfaced by audit, and intentionally
deferred. New work in the sync engine should not regress these further
without a plan to fix them.

- **ReUploadOp is not idempotent under a crash between APPEND and
  commit.** `_execute_reupload` calls `session.append_message(...)`
  before the index transaction clears the old `uid`. If the process
  dies after the APPEND lands on the server but before the enclosing
  `with self._index.connection()` block commits, the next sync sees
  the same C-1 state (server-deleted, locally-modified) and re-emits
  ReUploadOp, producing a duplicate on the server. A proper fix needs
  UIDPLUS-based dedup on retry or an explicit two-phase marker on the
  row; both are out of scope for v1.

- **Messages in excluded server folders are invisible to dedup.**
  `remote_mid_map` is built only from folders that pass
  `folder_policy.should_sync`.  If a message with Message-ID `M` lives
  on the server in an excluded folder (typically `[Gmail]/All Mail`)
  and a pending `uid=NULL` row for `M` exists locally in a synced
  folder, Step 5 sees `M` as "not on the server" and emits
  `PushAppendOp`, producing a second copy on the server. Users who
  include an aggregate folder in their account already hit a related
  warning (see SYNC_AUDIT 7b). Clean fixes would require scanning
  excluded folders for dedup only, which conflicts with the intended
  meaning of the exclude policy.
