---
title: Mail Synchronization
---

# Mail Synchronization

This is the **reference** for Pony Express's IMAP engine: what it does in
which order, how it resolves conflicts, and which edges are sharp. It is
written for the reader who wants to know exactly what happens to their
mailbox, and is more detail than using the program requires.

!!! info "Scope: mail only"
    Everything below is about **mail** over IMAP. The calendar has a
    separate engine against CalDAV, with its own fast, medium and full
    reconciliation paths. It follows the same principle — the local files
    are authoritative, and nothing is destroyed to resolve a conflict —
    and the part of it you need in order to use the calendar is in
    [Calendar → Keeping in sync](calendar.md#keeping-in-sync). Its
    equivalent of the `uid IS NULL` marker below is `href IS NULL` on a
    component row.

!!! tip "If you just want to sync your mail"
    [Terminal UI → Sync from the TUI](tui.md#sync-from-the-tui) covers
    the keys and the dialogs, and [CLI Reference](cli.md#pony-sync)
    covers `pony sync`. Come back here when something has gone wrong, or
    when you want to know why it did what it did.

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

Sync operations use batched SQLite transactions rather than committing after
each message, which is most of the I/O a large mailbox costs. What the batch
promises is narrower than "the folder, all at once":

- A batch belongs to **one thread**. Each thread holds its own connection and
  its own transaction, so the sync can neither commit nor discard a write the
  UI made while it was running, and nor can the UI discard the sync's.
- A batch **commits as it goes**, every 500 writes. An unbounded transaction
  grows the write-ahead log until throughput collapses, and loses everything
  it has done if the process is killed. So an error part-way through rolls
  back the tail of the batch -- whatever the open transaction still holds --
  and not the part already committed. Each sync operation is independently
  meaningful, which is what makes that acceptable: the next pass re-plans
  from the index as it now stands.
- A **nested** `connection()` block is a unit of work inside the outer one,
  fenced by a savepoint. A single operation can fail, be logged, and be
  skipped -- which is what the execution loop does -- without taking the rest
  of the batch with it. Only the outermost block ends the transaction.
- One connection writes at a time. A reader never waits for a writer (that is
  what WAL mode buys), but a *write* that lands while a long batch holds the
  lock waits for it, and gives up with "database is locked" if the batch
  outlasts the busy timeout.

---

## Message identity

A message is identified by its row: the SQLite autoincrement `id`, scoped to
one account and folder, which is what a `MessageRef` carries. Neither of the
obvious alternatives can be the key:

- IMAP UIDs are only valid within one folder and one UIDVALIDITY epoch. If the
  server rebuilds a mailbox, every UID changes.
- `Message-ID` is not unique. Resends and mailing-list copies share one, and
  some messages carry none at all. It is display, threading and
  duplicate-detection data; two rows in one folder may hold the same value.

So the `Message-ID` is used to *recognise* a message, never to key it -- and
only where it names exactly one candidate. Where it does not, a fetched
message is compared against the rows still waiting for a UID on what it says
about itself: sender, subject and date. Several rows saying the same thing are
interchangeable, and one of them is taken rather than a copy being inserted
beside them all.

All message state -- local flags, server flags, UID, and sync timestamp -- is
stored in a single unified `messages` table in the SQLite index. There is no
separate server-state table; each message row holds both the local desired state
and the last-known server state.

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

**In a writable folder the deletion wins.** If you trashed a message locally
and another client changed its flags on the server before the next sync, Pony
still expunges it. Deleting is an explicit instruction and a flag change is
not, so the flag change does not override it — but it does mean a message can
be removed after another client has touched it, which is worth knowing before
you trash something you are unsure about.

**In a read-only folder the deletion is cancelled**, because Pony has no way
to expunge on the server: the message is restored to active status with the
server's current flags. See [Read-only folders](#read-only-folders).

### Read-only folders

Folders marked as `read_only` in your config are synced server-to-local only.
Local flag changes are not pushed back. If you trash a message in a read-only
folder, the next sync restores it (since the server still has it).

---

## Safety halts

Two guards can **halt sync for one folder** and ask for explicit confirmation
before it proceeds. Other folders are synced normally.

- **Mass deletion (C-6).** More than 20% of the UIDs a folder is known to
  hold disappeared in a single sync, in a folder known to hold at least five
  of them. That is what an accidental mass-delete, or a server-side filter
  gone wrong, looks like.
- **Mass upload (C-7).** The pass would `APPEND` more than half the folder, or
  50 messages outright, with at least five uploads either way. `uid IS NULL`
  is the signal to upload, and it cannot tell "composed here" from "on the
  server, handle lost", so a folder's worth of uploads is an index that lost
  its UIDs rather than a user who wrote that much mail. A mirror rebuilt by
  `pony reindex` whose filenames carried no handles looks exactly like this.

A halt is answered where there is somebody to ask: the TUI's confirmation
screen highlights the folder and **Proceed** applies it, the browser's Sync
button turns the halt into a question and applies it on confirm, and on the
command line `pony sync --yes`, or `y` at the interactive prompt, does the
same. The paths with nobody to ask — periodic sync in either front end, and
++ctrl+g++ — **skip** a halted folder and report it, so it stays unsynced
until one of the paths above answers the question once.

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

- **Accounts the config no longer declares**: their index rows are **kept**.
  An account missing from the config means the config changed, not that the
  mail should go, so the sync logs a warning naming the account and the
  `pony reset --account <name>` that removes it.
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
become meaningless. Pony detects this automatically during planning. The mail
is not meaningless, only its numbering: the bodies stay in the mirror and the
rows stay in the index, and execution clears the UID epoch rather than deleting
anything.

The server then reports the whole folder as new UIDs. Most of them are not new
mail, so the planner pairs them with the rows the folder already holds, using
the `Message-ID`s from the headers-only scan it has already done. A paired UID
is **re-keyed**: the row is given the new number without the body being
downloaded again. A folder of 15,000 archived messages costs one scan rather
than 15,000 bodies.

A `Message-ID` is not a key. Resends and mailing-list copies share one and some
messages carry none, so one is used only where it names exactly one row *and*
exactly one of the UIDs being planned -- the planner has only the headers, and
a `Message-ID` shared by two UIDs says nothing about which row each belongs to.
Everything else is fetched and matched on arrival against what the message says
about itself (see [Message identity](#message-identity)), which is the
comparison that can tell the planner's ambiguous cases apart.

Re-keying writes the new UID to the index. The mirror filename keeps the
handle it was stored with (`,V=<epoch>,U=<uid>`), which is a superseded epoch
until the message is stored again; `pony reindex` reads the epoch from the
filenames themselves, so a folder rebuilt from such a mirror comes back under
the old epoch and the next sync re-keys it again -- cheaply, by the same
path.

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

In the TUI, ++ctrl+g++ starts a non-blocking background sync immediately. It
shows a spinner on the Folders panel title while it runs. There is nobody to
ask on this path, so it confirms every folder that is safe and **skips** any
folder that tripped a safety halt, reporting it rather than waiving it. A
halted folder therefore keeps being skipped until one of the paths that can
ask confirms it once — see [Safety halts](#safety-halts).

Periodic sync runs on a thread the application owns, not on a timer belonging
to whichever screen is in front, so it keeps its cadence while you are looking
at the calendar. Set `background_sync_enabled = true` at the top level of the
config to start it when the TUI starts; the interval is controlled by
`background_sync_interval_seconds` (default 600 seconds), and the first
automatic run falls one interval after startup rather than at it. The calendar
has a thread, a key and a pair of settings of its own, under `[calendar]` and
with different defaults — see
[Calendar → When it runs](calendar.md#when-it-runs). ++ctrl+g++ asks for a run now and starts the cadence
if it was not running, so the manual trigger still arms repeats even with
`background_sync_enabled = false`.

Pony refuses overlapping syncs: only one sync holds the slot at a time, and the
foreground ++g++ flow keeps it for as long as its confirmation dialog is open,
so the periodic thread cannot open a second IMAP session on the same account
while you are deciding. A periodic turn that arrives then is **postponed, not
dropped** — it tries again shortly, instead of waiting out a whole interval.

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
    message_id       TEXT    NOT NULL DEFAULT '',   -- display only
    sender, recipients, cc, subject, body_text,
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
CREATE INDEX ix_messages_account_folder
    ON messages (account_name, folder_name);
CREATE INDEX ix_messages_folder_received          -- the folder list view
    ON messages (account_name, folder_name, received_at DESC);
CREATE INDEX ix_messages_message_id
    ON messages (account_name, message_id)
    WHERE message_id != '';
CREATE INDEX ix_messages_pending                  -- rows awaiting a push
    ON messages (account_name, folder_name, sender, subject, received_at)
    WHERE uid IS NULL;
CREATE INDEX ix_messages_unseen                   -- unread counts per folder
    ON messages (account_name, local_status, folder_name, local_flags);
```

`MessageRef = (account_name, folder_name, id)`. A row carries no epoch of its
own: a UID is only ever looked up for the folder it was found in, so the epoch
is the folder's, and the mirror filename keeps the pair (`,V=<epoch>,U=<uid>`)
for `pony reindex` to read when there is no index left. `body_text` is the
body as plain text -- the whole of it, which is what the FTS index is built
from. The `folder_sync_state` table holds UIDVALIDITY, the highest known UID,
UIDNEXT and HIGHESTMODSEQ as watermarks. It has no message count: the one the fast path compares against
`STATUS MESSAGES` is counted from the index rows that carry a UID
(`count_uids_for_folder`).

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
4. `new_uids` → one `FetchNewOp` each, except after a UIDVALIDITY change,
   where a UID whose `Message-ID` names exactly one row in the folder (and
   which is itself the only UID carrying that `Message-ID`) becomes an
   `AdoptUidOp` instead: the row is re-keyed, nothing is downloaded. The
   adoptions are emitted before the fetches, because any other op flushes the
   executor's fetch batch.
5. `gone_uids`: `local_flags != base_flags` and folder writable → `ReUploadOp`;
   otherwise `ServerDeleteOp` (mark TRASHED).
6. `remote ∩ local`: flag reconciliation → `PullFlagsOp`, `PushFlagsOp` or
   `MergeFlagsOp`.
7. **C-6:** more than 20% of local UIDs gone (with at least 5 known) → flag for
   confirmation.

C-7 is applied to every folder plan, whichever path produced it: more than
half of `known_total` plus this pass's fetches would be a `PushAppendOp` or a
`ReUploadOp`, or 50 of them outright, with at least 5 either way → flag for
confirmation.

There is no cross-folder Message-ID map. A server-side move across folders is
seen as a delete in the source plus a fetch in the target.

### Local actions

There is no pending-operations table. A local mutation is recorded by
rewriting the message's index row, and the planner is the sole observer — so
the field sets below *are* the contract. They are implemented once, in
`pony.mail.mailbox_ops`: `landed_in_folder` for a message arriving in a folder,
`moved_to_folder` for a move within one account.

| Action | Index mutation | Sync op |
|---|---|---|
| Archive | `folder=T`, `uid=NULL`, `local_status=PENDING_MOVE`, `source_folder=F`, `source_uid` | `PushMoveOp` |
| Move (same account) | Same, user-chosen folder | `PushMoveOp` |
| Move (cross account) | New row in target; source → `TRASHED` | `PushAppendOp` (target) + `PushDeleteOp` (source) |
| Trash (`D`) | `local_status=TRASHED`; keep `uid` | `PushDeleteOp` |
| Compose / send | New row `uid=NULL`, `folder=Sent` | `PushAppendOp` |
| Flag change | Update `local_flags` | `PushFlagsOp` if drift |

### UID recovery

`PushAppendOp` and `PushMoveOp` capture the new UID from APPENDUID / COPYUID
(RFC 4315). If the server omits it, the row stays `uid IS NULL` and the next
sync adopts it via `FetchNewOp`.

### Conflict identifiers

| ID | Condition | Resolution |
|---|---|---|
| **C-1** | UID in `gone_uids` and `local_flags != base_flags` | `ReUploadOp` |
| **C-2** | Locally trashed, server has it in a read-only folder | Restore `ACTIVE`, pull flags |
| **C-4** | UIDVALIDITY reset | Reset op clears the UID epoch; rows re-keyed from the headers where a `Message-ID` names one, the rest refetched |
| **C-6** | More than 20% of UIDs gone | Confirmation required |
| **C-7** | Uploads would rewrite over half the folder, or 50 messages | Confirmation required |

### mbox storage keys

A Maildir storage key is a filename and is stable by construction. An mbox key
is the message's ordinal position, which is not — removing a message shifts
every message after it, and the shift only becomes visible on the next open,
by which time the index has recorded the old numbers.

So an mbox mirror is append-only. Deleting marks the message with the mbox `D`
status flag and leaves it in the file; it is then absent from `list_messages`
and reads and flag writes against its key raise `KeyError`. Appends and flag
changes never disturbed the numbering, so with removal gone the ordinals are
durable.

`compact_folder` reclaims the space. It is the only operation that renumbers,
and it returns `{old_key: new_key}` for every survivor so the caller can update
the index in the same transaction rather than have the numbering change beneath
it.

### Trash retention

`TRASHED` rows are kept for `account.mirror.trash_retention_days` (default 30),
then reaped by sync cleanup.
