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
become meaningless. Pony detects this automatically, discards the stale UID
mapping, and performs a full resync of the affected folder by matching messages
via `Message-ID`. No messages are lost.

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

### No background sync

Pony does not sync in the background. Sync only runs when you explicitly
request it (++g++ in the TUI, or `pony sync` on the command line). This is a
deliberate design choice for v1: it keeps the sync model simple and
predictable, and avoids the complexity of concurrent database access.

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
