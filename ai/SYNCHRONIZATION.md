# Synchronization Design

Authoritative description of the IMAP sync model.  The sync engine
(`src/pony/sync.py`) must implement this; deviations require updating
this document first.

## Guiding principles

- **State, not history.** Correctness from comparing current state on
  each side, not replaying user actions.
- **Idempotency.** Running sync twice on unchanged state produces no
  mutations.
- **Non-destructive by default.** No permanent deletion without
  explicit user confirmation or trash retention expiry.
- **Per-row identity.** Identity is the SQLite autoincrement `id`,
  scoped by `(account, folder)`.  `Message-ID` is a *display
  attribute*, not a key; multiple rows in one folder may share it.
- **`uid IS NULL` means "needs a server-side push."** A local row with
  a null UID is the sole signal that sync must round-trip the row.
  When `local_status = PENDING_MOVE`, `source_folder` and `source_uid`
  carry the server-side handle for the move.  Otherwise it is a
  compose draft awaiting `APPEND`.

## Identity model

The local index keys rows by an autoincrement `id`:

```sql
CREATE TABLE messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name     TEXT    NOT NULL,
    folder_name      TEXT    NOT NULL,
    uid              INTEGER,                       -- NULL = pending push
    uid_validity     INTEGER NOT NULL DEFAULT 0,    -- epoch tag for uid
    message_id       TEXT    NOT NULL DEFAULT '',   -- display only
    sender, recipients, cc, subject, body_preview,
    storage_key      TEXT    NOT NULL DEFAULT '',
    has_attachments  INTEGER NOT NULL DEFAULT 0,
    local_flags, base_flags, server_flags, extra_imap_flags,
    local_status     TEXT    NOT NULL,   -- ACTIVE | TRASHED | PENDING_MOVE
    received_at      TEXT    NOT NULL,
    trashed_at, synced_at,
    source_folder    TEXT,                -- PENDING_MOVE only
    source_uid       INTEGER               -- PENDING_MOVE only
);

CREATE UNIQUE INDEX ux_messages_uid
    ON messages (account_name, folder_name, uid)
    WHERE uid IS NOT NULL;
```

`MessageRef = (account_name, folder_name, id: int)` carries the row
identity through the API.  Display Message-IDs live as
`IndexedMessage.message_id`; they may be empty.

`folder_sync_state` records UIDVALIDITY / UIDNEXT / MESSAGES /
HIGHESTMODSEQ per folder; sync uses it as the watermark for the
fast/medium/slow path gate.

## Pipeline

Sync is a two-pass plan/execute over each account:

1. **Plan** (`ImapSyncService.plan`) — connect, fetch metadata, emit a
   typed `SyncPlan`.  No mirror or index writes (except clearing stale
   UIDs on a UIDVALIDITY reset).

2. **Execute** (`ImapSyncService.execute`) — apply each op, updating
   the local mirror, index, and per-folder watermarks.

`ImapSyncService.sync` runs both in one shot and implicitly confirms
all mass-deletion folders.

## Per-folder path selection

For each folder the planner picks one of three paths from a single
`STATUS` round-trip:

| Path     | Trigger                                                     | Cost |
| ---      | ---                                                         | --- |
| **Fast** | UIDVALIDITY, UIDNEXT, MESSAGES, HIGHESTMODSEQ all match     | no `FETCH`; emits only push-side ops |
| **Medium** | UID set stable but HIGHESTMODSEQ advanced                 | `UID FETCH 1:* (FLAGS) CHANGEDSINCE` for the changed subset |
| **Slow** | UID set changed (anything else)                             | `UID FETCH 1:* (FLAGS Message-ID-header)` full scan |

Fast and medium paths skip new-UID detection by design — by
construction the UID set has not changed.

## Slow-path planner (per folder)

For each folder in scope:

1. **STATUS gate.** As above.
2. **Per-folder UID diff** against the local index:
   - `new_uids = remote_uids − local_uids`
   - `gone_uids = local_uids − remote_uids`
3. **Pending push rows** (rows with `folder = F` and one of:
   `uid IS NULL`, `local_status = TRASHED`, `local_status =
   PENDING_MOVE`, or flag drift):
   - `PENDING_MOVE` with `source_*` set → `PushMoveOp` (or
     APPEND+EXPUNGE fallback).
   - `ACTIVE` with `uid IS NULL` → `PushAppendOp`.
   - `TRASHED` with `uid` set → `PushDeleteOp`.
   - `TRASHED` with `uid IS NULL` → `PurgeLocalOp`.
   - Flag drift on a UID-bearing row → `PushFlagsOp`.
4. **For each `uid in new_uids`:** emit `FetchNewOp`.
5. **For each `uid in gone_uids`:** look up the row by `(folder, uid)`.
   - If `local_flags != base_flags` (C-1 path, locally modified) and
     the folder is writable → `ReUploadOp`.
   - Otherwise → `ServerDeleteOp` (mark TRASHED locally).
6. **For `uid in remote ∩ local`:** flag reconciliation — `PullFlagsOp`,
   `PushFlagsOp`, or `MergeFlagsOp` per the three-way rule.
7. **C-6 mass-deletion safety halt.** If more than 20 % of
   locally-known UIDs disappeared in one pass (and at least 5 UIDs are
   known), the folder is flagged for confirmation.

There is no cross-folder Message-ID map, no synthetic Message-ID
fallback, and no `LinkLocalOp` / `ServerMoveOp` — duplicate
`Message-ID` rows are simply two rows; cross-folder server-side moves
are detected as a delete in the source folder and a fetch in the
target folder.

## Local actions

| Action            | Index mutation                                                                                             | Sync op |
| ---               | ---                                                                                                        | --- |
| Archive           | Same row; set `folder = T`, `uid = NULL`, `local_status = PENDING_MOVE`, `source_folder = F`, `source_uid` | `PushMoveOp` |
| Move (same acct)  | Same as archive but to a user-chosen folder                                                                | `PushMoveOp` |
| Move (cross acct) | Insert new row in the target account, mark source `TRASHED`                                                | `PushAppendOp` on the target account; `PushDeleteOp` on the source |
| Trash (`D`)       | Set `local_status = TRASHED`; keep `uid`                                                                   | `PushDeleteOp` |
| Compose / send    | Insert new row with `uid = NULL`, `folder = Sent`                                                          | `PushAppendOp` |
| Flag change       | Update `local_flags`                                                                                       | `PushFlagsOp` if `local_flags != base_flags` |

## UID recovery from APPEND / MOVE

`PushAppendOp` and `PushMoveOp` capture the new UID from the IMAP
response code (RFC 4315 / UIDPLUS): `APPENDUID` for APPEND, `COPYUID`
for COPY/MOVE.  When the response carries the code the row's `uid`
updates immediately.  When the server omits it (no UIDPLUS), the row
keeps `uid = NULL` and the next sync's per-folder diff picks up the
new UID via `FetchNewOp` — correct but more expensive.

## Conflict cases

- **C-1: server lost a locally-modified message.**  The UID is in
  `gone_uids` AND `local_flags != base_flags`.  Re-uploads via
  `ReUploadOp` instead of trashing.
- **C-2: server still has a row trashed locally in a read-only folder.**
  Restore the row to `ACTIVE` and pull the server's flags.
- **C-4: UIDVALIDITY reset.**  All UIDs for the folder are NULLed and
  the next sync re-fetches from scratch.
- **C-6: mass deletion.**  More than 20 % of UIDs gone in one pass
  flags the folder for explicit confirmation before applying.

## Trash retention

`local_status = TRASHED` rows are retained for
`account.mirror.trash_retention_days` (default 30) before the index
row and mirror file are reaped by the sync's cleanup pass.

## What the rewrite removed

- `_synthetic_message_id` and the duplicate-APPEND loop it caused.
- Cross-folder `remote_mid_map`, `_local_mid_folders`,
  `local_pending_mid_folder`.
- `LinkLocalOp` (replaced by COPYUID/APPENDUID-driven row update).
- `ServerMoveOp` (replaced by trash-on-source + fetch-on-target).
- `pending_operations` table and the `OperationType` /
  `PendingOperation` API (never wired up to anything).

The previous identity model (`PRIMARY KEY (account, folder,
message_id)`) is documented in commits up to the rewrite for
historical context.
