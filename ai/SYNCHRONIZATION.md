# Synchronization

Authoritative spec for `src/pony/sync.py`. Update this doc before deviating.

## Principles

- **State-based.** Correctness from current state on each side, not action history.
- **Idempotent.** Sync on unchanged state → no mutations.
- **Non-destructive.** No permanent deletion without confirmation or retention expiry.
- **Per-row identity.** Key is SQLite autoincrement `id` scoped to `(account, folder)`. `Message-ID` is display-only; duplicates are allowed.
- **`uid IS NULL` = needs push.** `PENDING_MOVE` rows carry `source_folder`/`source_uid`; otherwise a draft awaiting `APPEND`.

## Schema

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

`MessageRef = (account_name, folder_name, id)`. `folder_sync_state` stores UIDVALIDITY/UIDNEXT/MESSAGES/HIGHESTMODSEQ as watermarks.

## Pipeline

1. **Plan** (`ImapSyncService.plan`) — fetch metadata, emit `SyncPlan`. No writes except clearing UIDs on UIDVALIDITY reset.
2. **Execute** (`ImapSyncService.execute`) — apply ops, update mirror + index + watermarks.

`ImapSyncService.sync` runs both and auto-confirms mass-deletion folders.

## Per-folder path selection

| Path | Trigger | Cost |
|---|---|---|
| **Fast** | UIDVALIDITY, UIDNEXT, MESSAGES, HIGHESTMODSEQ all match | No `FETCH`; push-side ops only |
| **Medium** | UID set stable, HIGHESTMODSEQ advanced | `UID FETCH 1:* (FLAGS) CHANGEDSINCE` |
| **Slow** | UID set changed | `UID FETCH 1:* (FLAGS Message-ID-header)` full scan |

## Slow-path steps

1. STATUS gate (path selection above).
2. UID diff: `new_uids = remote − local`, `gone_uids = local − remote`.
3. Pending push rows:
   - `PENDING_MOVE` + `source_*` → `PushMoveOp` (APPEND+EXPUNGE fallback if no MOVE support).
   - `ACTIVE` + `uid IS NULL` → `PushAppendOp`.
   - `TRASHED` + uid set → `PushDeleteOp`.
   - `TRASHED` + `uid IS NULL` → `PurgeLocalOp`.
   - Flag drift on UID-bearing row → `PushFlagsOp`.
4. `new_uids` → `FetchNewOp` each.
5. `gone_uids`: `local_flags != base_flags` + folder writable → `ReUploadOp`; else → `ServerDeleteOp` (mark TRASHED).
6. `remote ∩ local`: flag reconciliation → `PullFlagsOp`, `PushFlagsOp`, or `MergeFlagsOp`.
7. **C-6:** >20% of local UIDs gone (≥5 known) → flag for confirmation.

No cross-folder Message-ID map. Cross-folder server moves = delete in source + fetch in target.

## Local actions

| Action | Index mutation | Sync op |
|---|---|---|
| Archive | `folder=T`, `uid=NULL`, `local_status=PENDING_MOVE`, `source_folder=F`, `source_uid` | `PushMoveOp` |
| Move (same acct) | Same, user-chosen folder | `PushMoveOp` |
| Move (cross acct) | New row in target; source → `TRASHED` | `PushAppendOp` (target) + `PushDeleteOp` (source) |
| Trash (`D`) | `local_status=TRASHED`; keep `uid` | `PushDeleteOp` |
| Compose/send | New row `uid=NULL`, `folder=Sent` | `PushAppendOp` |
| Flag change | Update `local_flags` | `PushFlagsOp` if drift |

## UID recovery

`PushAppendOp`/`PushMoveOp` capture new UID from APPENDUID/COPYUID (RFC 4315). If server omits it, row stays `uid=NULL` and next sync picks it up via `FetchNewOp`.

## Conflicts

- **C-1:** UID in `gone_uids` AND `local_flags != base_flags` → `ReUploadOp`.
- **C-2:** Locally trashed but server has it in read-only folder → restore `ACTIVE`, pull flags.
- **C-4:** UIDVALIDITY reset → NULL all UIDs, re-fetch next sync.
- **C-6:** >20% UIDs gone → confirmation required.

## Trash retention

`TRASHED` rows kept for `account.mirror.trash_retention_days` (default 30), then reaped by sync cleanup.
