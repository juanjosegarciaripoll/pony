---
title: Synchronization
---

# Synchronization

This page explains how Pony Express synchronises your mail with an IMAP server,
how conflicts are resolved, and what you need to know to avoid surprises.

## How sync works

Sync is a two-pass process:

1. **Plan.** Pony connects to the IMAP server, fetches lightweight metadata
   (UIDs, flags, Message-IDs), and compares it against the local state stored
   in SQLite. The result is a list of operations: fetch new messages, pull flag
   changes, push local flag changes, delete, move, etc. No changes are made
   during planning. A progress bar tracks scanning progress per folder.

2. **Execute.** The plan is shown for confirmation (in the TUI or CLI). Once
   confirmed, Pony applies each operation: downloading new messages to the
   local mirror, updating the index, and pushing local changes back to the
   server. A progress bar shows per-operation progress during execution.

This design means you always see what will happen before it happens.

### What gets synced

For each account, Pony syncs the folders allowed by the folder policy in your
config (see [Configuration](configuration.md)). Within each folder:

- **New messages on the server** are downloaded and indexed.
- **Messages deleted on the server** are moved to local trash.
- **Flag changes** (read, flagged, answered, etc.) are reconciled in both
  directions using a three-way merge.
- **Messages you deleted locally** are expunged from the server.
- **Messages you flagged locally** have their flags pushed to the server.

### Performance

Sync operations use batched SQLite transactions -- all database writes for a
folder are grouped into a single transaction rather than committing after each
message. This significantly reduces I/O overhead when syncing large mailboxes.
The transaction batching is automatic; the `connection()` context manager
handles nesting and rollback on errors transparently.

---

## Message identity

Pony identifies messages by their `Message-ID` header, not by IMAP UIDs.
This is important because:

- IMAP UIDs are only valid within one folder and one UIDVALIDITY epoch. If the
  server rebuilds a mailbox, all UIDs change.
- `Message-ID` is set by the sending mail server and is stable across copies,
  moves, and re-deliveries.

All message state -- local flags, server flags, UID, and sync timestamp -- is
stored in a single unified `messages` table in the SQLite index. There is no
separate server-state table; each message row holds both the local desired state
and the last-known server state.

When a message has no `Message-ID` header (rare but possible), Pony generates
a deterministic synthetic ID from the message content.

---

## Flag reconciliation

Flags are recorded in the SQLite index and written onto the message in the
local mirror as well — the filename suffix for Maildir, the `Status` /
`X-Status` headers for mbox — so a second mail client reading the same tree
agrees with Pony about what has been read.

When both you and another client (e.g. your phone) change flags on the same
message between syncs, Pony uses a **three-way merge**:

- **Base**: the flags at the time of the last sync (the common ancestor).
- **Local**: the flags you set in Pony.
- **Remote**: the flags currently on the server.

The merge policy is **union**: any flag set on either side is set on both. For
example, if you marked a message as flagged on your phone and marked it as read
in Pony, after sync it will be both flagged and read everywhere.

If both sides made the exact same change independently (e.g. both marked it
read), no conflict is reported.

### Custom server flags

Some IMAP servers use custom flags like `$Important`, `$Junk`, or
`$Forwarded`. Pony does not display or manage these flags, but it preserves
them: when pushing flag changes to the server, Pony includes any custom flags
that were already present. Your server-side filters and other clients will not
lose their metadata.

---

## Conflict resolution: the safe path

Pony always chooses the path that preserves data. No message is permanently
lost without your explicit action.

### Server deleted a message you modified locally

If you changed the flags on a message (e.g. starred it) and the server deleted
it before the next sync, Pony **re-uploads** the message to the server via IMAP
APPEND. Your local changes are preserved and the message reappears on the
server.

If you had *not* modified the message locally, it is simply moved to local
trash.

### You deleted a message but the server changed its flags

If you trashed a message locally but another client changed its flags on the
server, Pony **cancels the deletion** and restores the message to active status
with the server's updated flags. The rationale: someone (or a server-side rule)
considered the message worth modifying, so deleting it might be premature.

If the server's flags are unchanged, the deletion proceeds normally.

### Read-only folders

Folders marked as `read_only` in your config are synced server-to-local only.
Local flag changes are not pushed back. If you trash a message in a read-only
folder, the next sync restores it (since the server still has it).

---

## Mass-deletion protection

If more than 20% of a folder's known messages disappear in a single sync
(indicating a possible accidental mass-delete or server-side filter gone wrong),
Pony **halts sync for that folder** and asks for explicit confirmation before
proceeding. Other folders are synced normally.

In the TUI, the sync confirmation screen will highlight the affected folder.
In headless mode (`pony sync --yes`), all folders are implicitly confirmed.

---

## Trash and garbage collection

When a message is deleted (either by you or by the server), it passes through
a two-stage lifecycle:

1. **Trashed**: the message is marked for deletion locally. The raw message
   and index row are retained. On the next sync with a writable folder, the
   deletion is pushed to the server.
2. **Purged**: after the server confirms the deletion (or after the configured
   retention period expires), the local copy is removed from both the index
   and the mirror.

The retention period is controlled by `trash_retention_days` in the mirror
config (default: 30 days). Garbage collection runs automatically at the start
of each sync.

---

## Creating folders

The TUI `N` action creates an empty folder in the local mirror. On the
next sync, the planner compares the set of folders the mirror exposes
against the set of folders the server returns; any folder present only
locally and passing the sync policy gets an `IMAP CREATE` at the top of
the execution pass.

This is the same machinery the archive action relies on: moving a message
into a folder that doesn't exist yet creates the mirror directory as a
side effect, and the next sync pushes the `CREATE` upstream before the
`UID MOVE` runs. You don't need to pre-create the archive folder on the
server — just set `archive_folder = "..."` and archive something.

`CREATE` is idempotent (no-op if the folder already exists on the
server). Deletion of folders is intentionally not supported.

---

## Archive and local moves

The `A` key in the TUI archives the selected message into the account's
`archive_folder`. The move is applied **locally** and immediately: the mirror
file is relocated and the index row's folder changes. The row's `uid` is set
to `NULL` — the marker that tells sync the row is waiting for the server to
catch up.

On the next sync:

- The **source folder's** planning step sees the server UID and the local
  `uid=NULL` row in the archive folder, and emits a `UID MOVE` to the archive
  folder (or `COPY` + `EXPUNGE` on servers without RFC 6851 MOVE support).
  Pony creates the archive folder on the server if it doesn't already exist.
- The **next** sync of the archive folder picks up the fresh UID the server
  assigned and adopts it into the existing row — no refetch, no duplicate.

If the server lost the message between archive and sync (deleted by another
client), the archive folder's planning step instead emits an `APPEND` so
the mirror bytes reach the server. **Archiving never destroys a message.**

`A` is a no-op — with a warning — when:

- the account has no `archive_folder` configured,
- the source folder is read-only (Pony can't remove the server-side copy),
- the archive folder is excluded from sync or is itself read-only.

---

## Periodic cleanup

Each sync pass also performs housekeeping:

- **Stale accounts**: if you remove an account from your config, its index
  data (messages, sync watermarks) is purged on the next sync.
- **Stale folders**: if a folder disappears from the server (renamed or
  deleted), its sync state is cleaned up.
- **Expired trash**: trashed messages older than `trash_retention_days` are
  permanently deleted from the index and mirror.

---

## Progress reporting

Both the planning and execution phases report progress through callbacks:

- **CLI**: a `\r`-overwriting counter line shows the current operation
  (e.g. `Scanning INBOX... 45/120`), with newline-terminated output for
  informational messages.
- **TUI**: a `ProgressBar` widget updates in real time. The bar appears when
  the total is known and hides for informational-only updates.

Progress is reported via a `ProgressInfo` dataclass carrying `message`,
`current`, and `total` fields.

---

## UIDVALIDITY reset

IMAP servers assign a `UIDVALIDITY` value to each folder. If this value
changes (e.g. after a server rebuild or mailbox migration), all cached UIDs
become meaningless. Pony detects this automatically during planning and, during
execution, drops stale UID-bearing rows before refetching the folder in the new
UID epoch. Local-only rows are preserved.

---

## Important caveats

### Single-machine, single-instance

Pony is designed for one user on one machine. Running two Pony instances
against the same account simultaneously is not supported and may cause
conflicting index updates. (Using Pony alongside other mail clients on
different machines is fine -- that's what the three-way merge handles.)

### Gmail label folders

Gmail exposes labels as IMAP folders. The same message appears in multiple
folders (e.g. INBOX and `[Gmail]/All Mail`). Pony warns if you sync aggregate
folders like `[Gmail]/All Mail` and recommends excluding them:

```toml
[accounts.folders]
exclude = ["\\[Gmail\\]/All Mail", "\\[Gmail\\]/Important"]
```

Without this exclusion, the same message is fetched multiple times, which
wastes bandwidth and storage. The sync engine handles the duplicates safely
(no data loss), but performance and clarity suffer.

### Background sync

In the TUI, ++ctrl+g++ starts a non-blocking background sync immediately. The
background path auto-confirms every folder, including folders that trip the
mass-deletion guard, and shows a spinner on the Folders panel title while it
runs.

That manual trigger also arms or restarts the periodic background-sync timer.
Set `background_sync_enabled = true` to arm the same timer at TUI startup; the
interval is controlled by `background_sync_interval_seconds` (default 600
seconds). Pony refuses overlapping syncs, so a timer tick during an active sync
is skipped with a notification rather than starting a second IMAP session.

### Plan-execute time gap

The sync plan is computed at time T1. If you review it in the TUI before
confirming, the server state may have changed by execution time T2. This is
harmless: new messages that arrived between T1 and T2 are simply picked up on
the next sync. Failed operations (e.g. fetching a UID that was expunged between
T1 and T2) are logged and skipped.

### mbox durability

The mbox mirror format rewrites the entire file on every flush. A hard kill
(power loss, `kill -9`) during a write can corrupt the file. **Prefer Maildir**
for accounts where durability matters. mbox is best suited for importing
existing archives managed by other tools.

### Folder name encoding

IMAP folder names may contain non-ASCII characters encoded in modified UTF-7.
Pony handles encoding and decoding automatically. On disk, special characters
in folder names (path separators, Windows-illegal characters) are replaced
with dots.

---

## Implementation reference

This section is the authoritative contract for `src/pony/sync.py`. Update it
before changing the algorithm.

### Principles

- **State-based.** Correctness comes from the current state on each side, not
  from a history of actions. There is no operation log to replay.
- **Idempotent.** Syncing unchanged state produces no mutations.
- **Non-destructive.** Nothing is permanently deleted without either explicit
  confirmation or retention expiry.
- **Per-row identity.** The key is the SQLite autoincrement `id` scoped to
  `(account, folder)`. `Message-ID` is display-only and duplicates are allowed
  — see [Message identity](#message-identity).
- **`uid IS NULL` means "needs push."** A `PENDING_MOVE` row carries
  `source_folder` / `source_uid`; otherwise it is a message awaiting `APPEND`.

### Schema

```sql
CREATE TABLE messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name     TEXT    NOT NULL,
    folder_name      TEXT    NOT NULL,
    uid              INTEGER,                       -- NULL = pending push
    uid_validity     INTEGER NOT NULL DEFAULT 0,
    message_id       TEXT    NOT NULL DEFAULT '',   -- display only
    sender, recipients, cc, subject, body_preview,
    storage_key      TEXT    NOT NULL DEFAULT '',
    has_attachments  INTEGER NOT NULL DEFAULT 0,
    local_flags, base_flags, server_flags, extra_imap_flags,
    local_status     TEXT    NOT NULL,   -- ACTIVE | TRASHED | PENDING_MOVE
    received_at      TEXT    NOT NULL,
    trashed_at, synced_at,
    source_folder    TEXT,               -- PENDING_MOVE only
    source_uid       INTEGER             -- PENDING_MOVE only
);
CREATE UNIQUE INDEX ux_messages_uid
    ON messages (account_name, folder_name, uid)
    WHERE uid IS NOT NULL;
```

`MessageRef = (account_name, folder_name, id)`. The `folder_sync_state` table
holds UIDVALIDITY, UIDNEXT, MESSAGES and HIGHESTMODSEQ as watermarks.

### Per-folder path selection

A folder takes the cheapest path its watermarks allow:

| Path | Trigger | Cost |
|---|---|---|
| **Fast** | UIDVALIDITY, UIDNEXT, MESSAGES and HIGHESTMODSEQ all match | No `FETCH`; push-side operations only |
| **Medium** | UID set stable, HIGHESTMODSEQ advanced | `UID FETCH 1:* (FLAGS) CHANGEDSINCE` |
| **Slow** | UID set changed | `UID FETCH 1:* (FLAGS Message-ID-header)` full scan |

### Slow-path steps

1. STATUS gate (path selection above).
2. UID diff: `new_uids = remote − local`, `gone_uids = local − remote`.
3. Pending push rows:
    - `PENDING_MOVE` + `source_*` → `PushMoveOp` (APPEND + EXPUNGE fallback when
      the server lacks MOVE).
    - `ACTIVE` + `uid IS NULL` → `PushAppendOp`.
    - `TRASHED` + uid set → `PushDeleteOp`.
    - `TRASHED` + `uid IS NULL` → `PurgeLocalOp`.
    - Flag drift on a UID-bearing row → `PushFlagsOp`.
4. `new_uids` → one `FetchNewOp` each.
5. `gone_uids`: `local_flags != base_flags` and folder writable → `ReUploadOp`;
   otherwise `ServerDeleteOp` (mark TRASHED).
6. `remote ∩ local`: flag reconciliation → `PullFlagsOp`, `PushFlagsOp` or
   `MergeFlagsOp`.
7. **C-6:** more than 20% of local UIDs gone (with at least 5 known) → flag for
   confirmation.

There is no cross-folder Message-ID map. A server-side move across folders is
seen as a delete in the source plus a fetch in the target.

### Local actions

There is no pending-operations table. A local mutation is recorded by
rewriting the message's index row, and the planner is the sole observer — so
the field sets below *are* the contract. They are implemented once, in
`pony.mailbox_ops`: `landed_in_folder` for a message arriving in a folder,
`moved_to_folder` for a move within one account.

| Action | Index mutation | Sync op |
|---|---|---|
| Archive | `folder=T`, `uid=NULL`, `local_status=PENDING_MOVE`, `source_folder=F`, `source_uid` | `PushMoveOp` |
| Move (same account) | Same, user-chosen folder | `PushMoveOp` |
| Move (cross account) | New row in target; source → `TRASHED` | `PushAppendOp` (target) + `PushDeleteOp` (source) |
| Trash (`D`) | `local_status=TRASHED`; keep `uid` | `PushDeleteOp` |
| Compose / send | New row `uid=NULL`, `folder=Sent` | `PushAppendOp` |
| Flag change | Update `local_flags` | `PushFlagsOp` if drift |

Flag changes also write through to the mirror (`mailbox_ops.mirror_flags`) so
another MUA sharing the tree sees them. The index stays authoritative: a failed
mirror write is logged, not raised. This is not yet applied to the bulk paths
(`mark_folder_read`, sync ingest and merge), because a per-message write costs
a rename on Maildir but a full mailbox rewrite on mbox.

### UID recovery

`PushAppendOp` and `PushMoveOp` capture the new UID from APPENDUID / COPYUID
(RFC 4315). If the server omits it, the row stays `uid IS NULL` and the next
sync adopts it via `FetchNewOp`.

### Conflict identifiers

| ID | Condition | Resolution |
|---|---|---|
| **C-1** | UID in `gone_uids` and `local_flags != base_flags` | `ReUploadOp` |
| **C-2** | Locally trashed, server has it in a read-only folder | Restore `ACTIVE`, pull flags |
| **C-4** | UIDVALIDITY reset | Reset op, drop stale UID-bearing rows, refetch in the new epoch |
| **C-6** | More than 20% of UIDs gone | Confirmation required |

### Trash retention

`TRASHED` rows are kept for `account.mirror.trash_retention_days` (default 30),
then reaped by sync cleanup.
