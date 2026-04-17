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

## Per-folder reconciliation algorithm

```
remote  <- IMAP: SELECT folder, FETCH UID ALL FLAGS
local   <- index: messages WHERE folder = ? AND uid IS NOT NULL

Step 1: Deletions on server
  for uid in local - remote:
    if message_id found in another folder -> move (update local folder)
    else -> server deleted: trash locally (or re-upload if locally modified)

Step 2: New messages on server
  for uid in remote - local:
    if message_id already in index -> copy/second folder (add index row)
    else -> genuinely new: fetch body, ingest into mirror + index

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

Step 5: Commit
  Update folder_sync_state: highest_uid, synced_at
```

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
