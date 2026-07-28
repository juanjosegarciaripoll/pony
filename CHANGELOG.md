# Changelog

All notable changes to Pony Express are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0]

First stable release. Pony Express is a terminal-first mail client: IMAP sync
into a Maildir or mbox mirror you own, a SQLite index over it, a three-pane
Textual reader, and SMTP out. Local accounts let it read and send from a tree
another tool already manages.

The 1.0 line commits to the on-disk shapes: the `config.toml` schema (version
2), the mirror layout, and the SQLite index. Changes to any of them from here
will come with a migration rather than a rebuild.

**Not in 1.0**, so you know before you install: authentication is password-based
(`plaintext`, `env`, `command` or `encrypted` credential backends) — there is no
OAuth, which Gmail and Outlook now require for IMAP, so those accounts need an
app password. There is no POP support. mbox mirrors rewrite the whole file on
flush and are best kept for archives; prefer Maildir where durability matters.

### Added

- **Background synchronization.** `Ctrl-G` starts a sync that does not block
  the reader: it auto-confirms every folder, still honours the mass-deletion
  guard, and shows a spinner on the folder pane's border title as its only
  visual footprint. It can also run on a timer — `background_sync_enabled`
  and `background_sync_interval_seconds` (default off, 600s). Starting a
  foreground sync while one is running, or vice versa, is refused rather than
  overlapping two IMAP sessions on one account.

- **New-mail indicator.** An icon appears against any account whose INBOX has
  unread mail, and in the terminal window title, so a minimised Pony still
  tells you something arrived.

- **Print a message to PDF**: press `Ctrl-P` in the message view (or the standalone
  `.eml` viewer) to render the current email to a PDF in a folder you pick.
  Pony reuses the same self-contained HTML as the browser view (`w`) and shells
  out to whichever HTML-to-PDF converter is installed — Chromium/Chrome,
  `wkhtmltopdf`, WeasyPrint, or LibreOffice — so no new dependency is bundled.
  A guidance notification is shown when none is found.

- **Resizable panes**: `Ctrl-←` / `Ctrl-→` move the boundary between the folder
  list and the rest of the window, and `Ctrl-↑` / `Ctrl-↓` move the one between
  the message list and the reader. The same boundaries can be dragged with the
  mouse — the handle is the border each pane already draws, so no screen space
  is given up to it. Sizes are saved to `ui_state.json` in the data directory
  and restored on the next launch.

### Security

- **`pony message attachment` could write outside the working directory.**
  With no `-o`, the destination was the working directory joined with the
  filename from the message's `Content-Disposition` header — a value chosen
  by the sender. A crafted `filename="../../.bashrc"` escaped the directory
  the user was in. Only the final path component is used now. The TUI save
  paths were already guarded; this was the CLI equivalent, missed when the
  0.7.0 traversal fix landed.

### Fixed

- **`pony some-message.eml` opens the viewer.** The shortcut had never once
  worked: argparse rejects an unrecognised subcommand while parsing, so the
  check that looked for a leftover filename was never reached and every such
  invocation exited with `invalid choice`. A leading argument that names an
  existing file is now routed through `pony view`. A file whose name happens
  to match a subcommand does not hijack it, and a path that does not exist
  still reports itself rather than being silently swallowed.

- **Flag changes now reach the mirror.** Read, flagged and answered state
  lived only in the SQLite index, so another MUA sharing the same Maildir or
  mbox tree — the arrangement `[[local]]` accounts exist for — saw everything
  as unread no matter what was done in Pony. Marking read, marking unread,
  flagging and replying now write the flags onto the message file too. The
  index stays authoritative: a mirror file that a sync moved underneath the
  action is logged and skipped rather than surfaced as an error.

  Not yet applied to the bulk paths — `mark all read`, and the flags sync
  applies when it ingests or merges from the server. Writing flags per
  message costs a rename on Maildir (~0.05 ms) but a full rewrite of the
  mailbox on mbox (~3.7 ms and growing with folder size), so those need to
  set the flags at store time rather than re-writing afterwards.

- **The `env` credential backend works for hyphenated account names.** The
  variable name replaced spaces but nothing else, so an account named
  `work-email` mapped to `PONY_PASSWORD_WORK-EMAIL` — which no POSIX shell
  can export, making the backend unusable for that account. Every character
  that cannot appear in a shell variable name now becomes an underscore.

- **Forwarded messages arrive readable.** Two defects compounded. The
  forwarded `.eml` was attached as raw bytes, so it was base64-encoded —
  RFC 2046 §5.2.1 allows only 7bit/8bit/binary for `message/*`, and a
  compliant reader descends into the part without decoding it, showing an
  unopenable blob. Separately, the forwarded body kept the CRLF line endings
  it had on the wire, which the composer's editor then adopted for the whole
  buffer; both of the composer's body splits are LF-anchored, so they failed
  silently and the entire message went through the Markdown renderer,
  collapsing the quoted text into paragraphs.

- **Contact autocomplete offers the best match first.** Both the
  frequency-ranked search and the alphabetical listing loaded their rows with
  `WHERE id IN (…)`, which returns them in rowid order — so whichever contact
  was created first won, and the least-used match was offered ahead of the
  one you write to daily. Suggestions can also be selected properly now.

- **The terminal is left as it was found.** Opening an attachment or a
  message in an external viewer could return to a terminal with its title and
  modes still altered.

- **`Ctrl-G` re-arms its repeat timer.** The periodic background sync stopped
  rescheduling after a manual trigger.

- **Attachment selection in the picker.** Choosing an attachment by number
  was unreliable for entries the list had scrolled past.

- **mbox files are closed before exit,** and the CLI's summary commands close
  their database connections rather than relying on interpreter shutdown.

- **The TUI keybindings in the documentation match the program.** Four keys
  were documented wrongly — reply-all was listed as "mark as read", the flag
  toggle as `F` rather than `!`, and trash as `d` rather than `D` — and
  mark-all-read, copy, move, edit-draft and the row-marking keys were absent
  entirely.

- **Replies now thread.** Pony sent no `In-Reply-To` or `References` header,
  so every reply arrived in the recipient's client as the start of a new
  conversation rather than as part of the one it was answering. Replies and
  reply-alls now chain onto the parent's `References`, unfolding the header
  when it arrives split across lines and keeping the thread root when a long
  chain has to be trimmed. Saving a reply as a draft and resuming it later
  keeps the thread. Forwards deliberately start their own thread.

- **Accounts that do not store their password in `config.toml` can send
  again.** The composer read the literal `password` field instead of asking
  the credentials provider, so any account using the `env`, `command` or
  `encrypted` backend was offered in the From dropdown and then refused at
  send time, asking for a password it was configured never to store. Sending
  now resolves credentials the same way syncing does.

- **Local accounts with an `[smtp]` block could not resolve a password at
  all.** Every credentials backend filtered for IMAP accounts, so a local
  account configured to send fell through to the plaintext backend
  regardless of the backend it named, and reported a missing `password`
  field.

- **Forwarding no longer leaves a copy of the message in the temp
  directory.** Each forward wrote the original to a permanent temporary file
  that was never removed, whether or not the forward was sent. The copy now
  lives in a private directory the composer deletes when it closes, and is
  named after the message's subject rather than arriving at the recipient as
  `forwarded-message-dn1nwh3p.eml`.

- **Harvested contacts no longer depend on where they were harvested from.**
  Names were split into first/last by three separate implementations that
  disagreed for any name of three or more words, so the same correspondent
  became a different contact record depending on whether they arrived via
  sync, via the composer after a send, or via the reader's harvest action.
  Compound family names ("Juan José García Ripoll" → "Juan José" /
  "García Ripoll") are now handled the same way everywhere, and an address
  echoed as its own display name is discarded on every path rather than only
  during sync.

- **`pony folder mirror` keeps read and flagged state.** It re-projected each
  message instead of copying the indexed row, and a fresh projection starts
  with empty flag sets — so mirroring a folder marked everything in the copy
  unread. The TUI's copy action already preserved them.

- **Creating a folder the sync would never push is now refused.** The planner
  only issues `CREATE` for folder names that pass the account's sync policy, so
  a folder made under an `exclude` pattern stayed local forever and anything
  moved into it was stranded — with nothing said at any point.

- **The browser view and the PDF export now show the same message as the reader.**
  They decided what counted as an attachment with their own rule, so an inline
  part carrying a filename was renamed, silently dropped from the list, or —
  when it was HTML — rendered *as the message body*, meaning `w` and `Ctrl-P`
  could show different text than the pane they were opened from.

- **Every message action now acts on the highlighted message.** `w`, `O`, `S`
  and the number keys read their bytes from the reader pane — the message last
  opened with Enter — while `s` and `ctrl+p` used the highlighted row. With the
  reader on one message and the cursor on another they operated on different
  mail without saying so.

- **Local accounts work with the read commands again.** `message
  body/get/attachment/mime` and `folder list` resolved the account with an
  IMAP-only filter, so a configured mbox or Maildir account reported
  "No account named 'x' in config." — while `folder mirror`, `folder dedup`,
  the TUI and the MCP tools handled the same account fine.

- **Trashing from the reader now records when it happened.** `trashed_at`
  was left unset, so a trashed message with no server UID was dropped by the
  next sync instead of being kept for the retention period, and retention
  could not see it either. `pony folder dedup` always recorded it.

- **A Message-ID can be given with or without its angle brackets everywhere.**
  The CLI accepted both; the MCP server accepted only the bracketed form and
  silently found nothing otherwise. The lookup itself normalises now.

- **`pony search` now understands its own query language.** The parser lived
  under `tui/`, so `pony search "from:alice"` searched for the literal text
  `from:alice` while typing the same thing at `/` in the TUI did a sender
  search. Both go through one parser now.

- **Recipients with a comma in their name are no longer split in two.** A
  display name containing a comma is quoted by the sending client
  (`"Doe, John" <j@example.com>`). Replying to such a message split the
  recipient at the comma, producing two broken half-addresses, and accepting
  a contact completion after one could overwrite it.

### Changed

- **The composer no longer decides how a reply is assembled.** Which
  identity sends it, how the subject is prefixed, which recipients survive a
  reply-all and how the message threads were inlined at each of the five
  entry points that open the composer, so the same identity fallback and
  Markdown-default lookup were repeated five times and a rule added to one
  was missing from the others. The composer now asks `pony.composer` for a
  `DraftSpec` and renders it. No behaviour changes.

- **The local-mutation sync contract has one implementation.** Recording a
  local change means rewriting the message's index row in a specific way —
  there is no pending-operations table, so the exact field set *is* the
  contract the sync planner reads. The "arrived in a folder" rewrite was
  open-coded in three places and the "moved to a folder" rewrite in two, so
  the contract held only as well as the least careful copy. Both now live in
  `pony.mailbox_ops`. No behaviour changes.

- **Account lookup and mirror construction have one implementation.** The
  CLI and the MCP server carried byte-identical mirror factories under
  different names, and "the IMAP account called X" was written three
  different ways across the CLI, the sync engine and the TUI. All of it now
  lives in `pony.accounts`. No behaviour changes; the duplication is the
  same shape as the bugs that came from splitting one rule across several
  copies.

- **Saved attachment filenames are cleaned the same way everywhere.** The
  reader, the save dialog and `pony message attachment` each had their own
  rules, so the same attachment could be written under different names
  depending on which one you used. Characters Windows rejects (`<>:"|?*`)
  are now replaced on every route, not just some, and a very long filename
  is capped.

- **`pony local-summary` no longer shows a Pending column.** It queried a
  `pending_operations` table that the current design removed — pending work
  is `uid IS NULL` on the index row — so the column was always empty.

- **A bare search word now matches the subject as well as the body.** It
  previously searched the body alone in the TUI, so a message titled
  "Invoice #3" did not come up for `invoice`. Use `body:invoice` to search
  the body only — which is what that prefix is for.

- **Messages now open on command, not on cursor movement.** Moving the row
  cursor in the message list — with the arrow keys or `n`/`p` — used to open
  the reader, take focus, and mark the message read. That made it impossible to
  browse a folder without reading every message you passed over, and because
  the reader took focus, the arrow keys then scrolled it instead of moving the
  cursor. The reader now starts closed and opens only on `Enter`; `Esc`/`q`
  closes it and returns focus to the list. With a message open, `n`/`p` still
  step through messages and keep the reader open.

## [0.8.0] - 2026-06-24
### Fixed

- **Folder `include` no longer acts as a global whitelist**: a non-empty
  `[accounts.folders] include` list previously restricted sync to *only* the
  listed folders, silently freezing every other folder (INBOX included).
  `include` is now correctly an exception list for `exclude` — folders matched
  by neither list still sync. To sync only a chosen set, exclude everything
  (`exclude = [".*"]`) and list the wanted folders in `include`.

## [0.7.0] - 2026-06-04

### Security

- **Terminal escape injection via email headers**: `_escape()` in the message
  view stripped Rich markup openers (`[`) but not C0/C1 control characters.
  A `Subject` or `From` header containing ANSI escape sequences (e.g.
  `ESC[2J`) could corrupt terminal state. All control characters except tab,
  LF, and CR are now stripped before display. The same sanitisation applies to
  `Subject`, `Date`, `Content-Type`, and all fields in the contact detail
  screen.
- **Path traversal in attachment save**: `save_attachment()` used the raw
  `Content-Disposition` filename to build the output path without sanitisation.
  A crafted filename such as `../../.bashrc` could write outside the downloads
  directory. Filenames are now stripped to their base component via `Path.name`
  with control characters removed, and every output path is verified with
  `is_relative_to(dest)` before writing.
- **NUL-byte format-sentinel injection**: the renderer uses `\x00` as an
  internal delimiter for bold / italic / underline sentinels. A plain-text body
  with embedded NUL bytes, or an HTML body using `&#0;` entities, could inject
  spurious formatting into the TUI. NUL bytes are now stripped at both decode
  sites (text/plain payload and HTML character data).

### Added

- **Standalone EML file viewer**: `pony view <file.eml>` opens any RFC 5322
  file in a full-screen message viewer. Invoking Pony with a bare filename
  (`pony message.eml`) does the same without an explicit subcommand. When an
  email attachment is of type `message/rfc822`, opening it pushes a nested
  viewer on the same stack — `q` returns to the parent message — rather than
  delegating to the OS.
- **Forward as attachment**: forwarding a message now seeds the draft with the
  source message attached as a removable `.eml` file, so the original arrives
  intact for the recipient.
- **Save message to disk** (`s`): a two-step dialog lets you choose what to
  save (body as Markdown, individual attachments, with editable filenames) and
  where. A `New Folder` button in the directory picker creates directories on
  the fly.
- **`pony folder dedup`**: find and eliminate duplicate messages in a folder
  (same `Message-ID` header). Without `--apply` it is a dry-run; with
  `--apply` it marks losers `TRASHED` for the next sync to expunge. The winner
  in each group is the copy with the most informative flag set.
- **`pony folder mirror`**: copy all messages from one folder into another,
  replacing its contents. Writes `uid=NULL` index rows so the next
  `pony sync` uploads them to the destination account via `APPEND`.
- **Rich-text formatting from HTML emails**: bold (`<b>`/`<strong>`), italic
  (`<i>`/`<em>`), underline (`<u>`), and strikethrough (`<s>`/`<del>`) are
  preserved in the TUI message view. Plain text, CLI, and MCP paths see clean
  text with no sentinels.
- **Terminal window title**: the terminal emulator's title bar reflects the
  current context — `Pony Express — account/folder` while reading, the app
  name while composing. The original title is restored on exit.
- **Clickable links in message body**: web links (`http://`, `https://`) render
  as a `[link↗]` token; clicking opens a dialog (Open / Copy / Cancel).
  `mailto:` links render as `[✉]` and prefill the composer's To field.
- **Composer `Alt+letter` shortcuts**: `Alt+S` send, `Alt+A` attach, `Alt+E`
  external editor, `Alt+M` toggle Markdown mode. `Ctrl+X`, `Ctrl+C`, and
  `Ctrl+V` now work as cut / copy / paste in the body and address fields.
- **Mass-deletion confirmation**: when sync detects >20% server-side deletions
  in a folder the CLI and TUI show a `[CONFIRM: would delete N of M (Z%)]`
  annotation and a warning. The plan is held until the user explicitly confirms.
- **Textual theme selection**: set `theme = "nord"` (or any Textual theme name)
  in `config.toml`. `--theme NAME` overrides for a single session;
  `--list-themes` prints all available names.

### Fixed

- **Cc recipients split at commas inside RFC 2047 display names**: reply and
  forward were calling `getaddresses()` on a decoded string, which split
  `"García Ripoll, Juan José" <addr>` into two phantom recipients. The
  structured header API (`field.addresses` on raw bytes) is used instead.
- **Compose: missing display name prompts the user**: when the selected account
  has no `full_name` in config and no matching contact, the contact editor
  opens on mount so the user can set their name before the first send.
- **Contacts harvest stores email address as display name**: when a mail client
  emits the address as its own display name (`"alice@example.com"
  <alice@example.com>`), the harvested contact now gets a blank display name
  so a later harvest with a real name can overwrite it.
- **Contacts FTS index lost after editing name**: a double-tombstone bug in the
  `contacts_au` trigger and `CREATE TRIGGER IF NOT EXISTS` silently blocking
  corrected trigger bodies caused updated contacts to vanish from autocomplete.
  Triggers are dropped and recreated on every startup.
- **Sync resurrects locally-trashed messages on server flag changes**: the C-2
  conflict path was restoring `TRASHED` rows whenever the server's flags
  changed since the last sync (e.g. `\Seen` set by an IMAP FETCH). `PushDeleteOp`
  now runs unconditionally for `TRASHED` rows in writable folders.
- **mbox: O(N) rewrites on bulk delete**: each `delete_message` call was
  immediately flushing the mbox, causing N full rewrites for N deletions in one
  sync cycle. Writes are now batched and flushed once at the end of the cycle.
- **Dedup TOCTOU**: `run_dedup_folder` now holds a single SQLite transaction
  across both the read snapshot and the mark-trashed writes, closing the window
  where a concurrent sync could alter rows in between.
- **Ctrl-C safety**: Maildir's `ThreadPoolExecutor` is drained on exit so
  in-flight async writes are never abandoned. Config writes use a
  tmp-then-replace pattern to prevent truncation on interrupt.
- **Cold-start local rescan**: `rescan_local_account` was materialising full
  `IndexedMessage` rows just to read each `storage_key`. A lean
  `list_folder_storage_keys` two-column query replaces the full hydration.

### Changed

- **Message list: async streaming**: folder contents load in batches of 200
  rows via a background worker, keeping the UI responsive during the SQL fetch.
  Opening a second folder cancels any in-flight load.
- **HTML-to-text rendering**: paragraph-level tags (`p`, `div`, `h1`–`h4`,
  `ul`, `ol`, `table`) insert blank separators so consecutive block elements
  produce readable spacing. Inline `<br>` and list items are handled
  separately. Whitespace runs collapse to a single space.
- **Composer attachment bar**: the single-line attachments bar is replaced with
  per-file rows, each with a remove button.
- **Unread counts 30× faster at startup**: a new covering index on
  `(account_name, local_status, folder_name, local_flags)` lets
  `unread_counts_by_folder` serve from the index B-tree without heap reads.
  Measured: 0.44 s → 0.019 s on a 115 k-row account.
- **Sync plan formatting unified**: `format_plan_detail` / `format_plan_summary`
  in `sync.py` replace the previously duplicated logic in `sync_confirm_screen.py`
  and `cli.py`. Both surfaces now share one implementation with consistent
  labels and full op coverage.
- **Silent no-activity accounts and folders**: the CLI post-sync summary and
  TUI notification omit accounts and folders where nothing changed.

## [0.6.0] - 2026-04-20
### Fixed

- **Folder-list open is 4× faster on big folders**: the message-list
  panel was calling ``list_folder_messages`` — which materialises a
  full ``IndexedMessage`` per row (three datetime parses, three
  flag-set reconstructions, plus recipients / cc / body_preview /
  base_flags / server_flags / extra_imap_flags / trashed_at /
  synced_at that the list never displays).  The panel now stores a
  new narrow ``FolderMessageSummary`` projection populated by
  ``IndexRepository.list_folder_message_summaries``, which selects
  only the ten columns the list renders and pushes the ACTIVE filter
  and ORDER BY into SQL.  On a 17,504-row folder this took the
  DB+parse portion of ``load_folder`` from ~1268 ms to ~300 ms.
  Action paths (flag / trash / archive / copy / move / reply /
  forward) re-fetch the full ``IndexedMessage`` on demand via
  ``get_message``, so nothing round-trips a stripped row back into
  ``upsert_message``.
- **Folder-panel unread counts computed in SQL**: the tree was
  calling ``list_folder_messages`` once per folder and materialising
  every ``IndexedMessage`` row (datetime parsing, flag-set
  construction) just to count unread.  For a 113k-row account this
  took ~6 s per refresh.  Replaced with a single ``GROUP BY`` query
  (``IndexRepository.unread_counts_by_folder``) — same information,
  ~900 ms for that same account, no Python row construction.
- **Garbled sync progress output**: the CLI progress callback used
  ``\r<msg>`` to overwrite the running per-message counter but never
  cleared the line before printing the per-folder completion message,
  so output like ``Listas.quinfog: 2559/2559`` and
  ``Listas.quinfog: 2559 fetched, 0 flag updates`` would concatenate
  into one line.  Both branches now prepend ``\r\033[K``
  (carriage-return + ANSI erase-to-end-of-line) so each message starts
  from a clean slate.

### Added

- **Scoped `pony reset --account NAME`**: reset can now target a single
  account instead of wiping the whole index and every mirror.  The
  scoped path drops only that account's index rows (via
  ``IndexRepository.purge_account``; FTS rows follow through the delete
  triggers), removes its mirror directory, and clears its entry from
  ``local_scan_state.json`` so the next startup re-scans from scratch.
  Credentials and other accounts are left intact.  Useful when one
  account's sync state has gone out of sync with its mirror (e.g. an
  interrupted sync that updated ``folder_sync_state`` before writing
  mail) and you want to rebuild it without re-entering passwords or
  losing data for your other accounts.
- **Centered keyboard-shortcut help panel (F1)**: Textual's built-in
  command palette (the side panel at `ctrl+p`) is disabled; `F1` now
  opens a compact, centered modal listing every TUI keybinding
  grouped by category (Navigation / Compose / Folders / Contacts /
  Messages / Attachments).  Dismiss with `F1`, `Esc` or `q`.
- **Local accounts appear in the TUI folder tree**: `FolderPanel`
  was discovering folders from `folder_sync_state` rows, which the
  sync engine never writes for local accounts — so local-account
  subtrees were invisible even though the mirror was fully populated.
  The panel now discovers folders from `mirror.list_folders` first
  (the single source of truth for both IMAP and local accounts) and
  still honours sync-state entries as a secondary source so empty
  remote folders show up before any mail has arrived.
- **mtime-cached local-mirror rescan**: startup was scanning every
  folder of every local account even when nothing had changed — a big
  cost for mbox archives, where ``list_messages`` walks the whole
  file.  The rescan now stats each folder's mtime and skips folders
  whose mtime hasn't advanced since the last scan.  Maildir uses
  ``max(cur.mtime, new.mtime)``; mbox uses the ``.mbox`` file's mtime.
  The per-account, per-folder mtime cache lives in
  ``<data_dir>/local_scan_state.json``; a missing or corrupt file
  self-heals by falling back to a full scan on the next run.  When all
  folders are cache-hits the startup line becomes ``[acc] Local mirror
  unchanged (all folders cached).``  A new
  ``MirrorRepository.folder_mtime_ns`` method backs the check.
- **Send-capable local accounts**: local accounts can now carry an
  optional `[smtp]` block (plus their own `username` /
  `credentials_source` / `password` / `password_command`) to send
  outgoing mail without needing a paired IMAP account.  The composer's
  "From:" dropdown now shows any account whose `can_send` predicate
  returns True — IMAP accounts (always), or local accounts with SMTP
  configured.  Filters that previously keyed off `isinstance(a,
  AccountConfig)` now use the semantic `account.can_send` instead.
- **Folder creation on local accounts**: the TUI `N` action no longer
  refuses on local accounts — the mirror backends implement
  `create_folder` regardless of account type.  The post-create
  notification is "Folder … created locally; run sync to propagate."
  for IMAP accounts and just "Folder … created locally." for local
  accounts (there is nothing to push).

### Changed

- **Breaking: TOML schema version 2.** The config file must now
  declare `config_version = 2` at the top level — `load_config`
  rejects files that omit it or carry a different value, rather than
  silently migrating.  SMTP settings move from the flat
  `smtp_host` / `smtp_port` / `smtp_ssl` keys into a nested
  `[accounts.<name>.smtp]` table with `host` / `port` / `ssl` keys.
  Updating existing configs is mechanical:

  ```toml
  # before
  smtp_host = "smtp.example.com"
  smtp_port = 465
  smtp_ssl  = true

  # after
  config_version = 2

  [accounts.personal.smtp]
  host = "smtp.example.com"
  port = 465           # optional; defaults to 465 when ssl=true, 587 otherwise
  ssl  = true          # optional; defaults to true
  ```

  See `config-sample.toml` for the full shape.  The same nested form is
  used for optional SMTP blocks on local accounts.
- **`pony.smtp_sender.send_message` signature**: now takes explicit
  keyword arguments `smtp: SmtpConfig`, `username: str`, `password: str`,
  `msg: EmailMessage` instead of an opaque `AccountConfig`.  The
  composer resolves these from whatever account is selected in the
  "From:" dropdown, so both IMAP and local-with-SMTP accounts share
  one send path.

## [0.5.0] - 2026-04-19
### Added

- **Accent- and case-insensitive full-text search via FTS5**: message
  and contact search was broken for any locale with non-ASCII letters
  because SQLite's built-in case folding only covers ASCII — so `maria`
  never matched `María`. The LIKE-based query paths are gone; search
  now runs against FTS5 virtual tables tokenised with
  `unicode61 remove_diacritics 2`, which folds both case and diacritics
  by construction. Applies to `pony search`, the TUI search dialog, the
  contacts browser, and the MCP `search_messages` / `search_contacts`
  tools.
- **Guided recovery flow on index-schema mismatch**: opening an older
  index (schema v1) with a newer binary used to abort with a bare
  error. `pony` now explains the three recovery steps (export contacts,
  delete index + mirrors, resync) and offers to perform the first two
  automatically, defaulting to **No**. Contacts are snapshotted to
  `<data_dir>/contacts-backup-<UTC-timestamp>.bbdb` via a new
  `load_contacts_for_backup()` entry point that reads directly from the
  mismatched DB.
- **Folder browser: unread indicator + hierarchical display**:
    - Folders whose messages are all read are rendered dim; folders
      with unread messages are rendered bright.  A synthetic parent
      (see below) follows the same rule based on whether any
      *descendant* has unread, so you can tell an account has
      something new without expanding its subtree.
    - Dotted / slashed folder names like ``Archives.2026`` or
      ``Lists/Unions`` are displayed as nested subtrees
      (``Archives`` → ``2026``).  The delimiter (``.`` or ``/``) is
      detected per-folder-name, which handles both Dovecot and Cyrus
      server conventions without configuration.  The stored name on
      the server, in the mirror, and in the index is unchanged — this
      is purely a display-side transformation.  When both a parent
      folder (e.g. ``Archives``) and its nested child
      (``Archives.2026``) exist on the server, the parent is
      selectable and shows its own unread count with the child nested
      beneath it.
- **Per-attachment retrieval on CLI and MCP**: both surfaces now let
  you pull a single attachment's bytes, not just see that attachments
  exist.
    - `pony message get` now lists every attachment (index, filename,
      content-type, size) after the metadata block when the message
      body is locally available — previously only an `Attach.: yes/no`
      line.
    - New `pony message attachment <account> <folder> <message-id>
      <index>` writes the bytes to the attachment's own filename in
      cwd, or to `-o PATH`, or to stdout via `--stdout`.  Refuses to
      clobber an existing file unless `-f/--force` is passed.
    - MCP `get_message` now carries the same `attachments` array as
      `get_message_body` (when the mirror holds the bytes) so MCP
      clients can discover what's available without pulling the full
      body.
    - New MCP `get_attachment(account, folder, message_id, index)`
      tool returns `{filename, content_type, size_bytes, data_base64,
      text?}`.  `data_base64` is always present (transport-safe for
      any attachment type); `text` is added for `text/*` attachments
      so agents can read them without base64-decoding on the client
      side.  Tool docstrings steer callers to `get_attachment` from
      `get_message` / `get_message_body`.
    - Internals: the MIME-walking logic that used to live in the TUI
      (`MessageViewPanel.save_attachment`) is extracted into a shared
      `extract_attachment(raw, index)` helper in `message_renderer`;
      CLI, MCP and TUI now share one indexing contract.
- **Move a message to another folder (`M`)**: new TUI action.  For
  same-account moves the mirror file is renamed in place and the index
  row switches folders with `uid=NULL` — Message-ID is preserved and
  the next sync emits `UID MOVE` server-side.  For cross-account moves
  there's no atomic IMAP primitive, so the operation decomposes into
  cross-account copy (MID preserved) + retire source: IMAP sources are
  marked `TRASHED` so the next sync `EXPUNGE`s them, local sources are
  deleted outright.  Target-first ordering means an interruption
  leaves a duplicate, not a loss.  Guards refuse moves out of or into
  read-only folders, and into folders excluded from sync.
- **Copy a message to another folder (`C`)**: new TUI action, modelled
  on archive. Opens a folder picker spanning every account in the
  config (including local accounts, which the main folder panel hides),
  copies the raw bytes into the chosen target's mirror, and inserts a
  `uid=NULL` index row so the next sync pushes the copy server-side
  via `APPEND`. Multi-select works: every marked row is copied.
  Same-account copies rewrite `Message-ID` to a synthetic
  `<pony-copy-*@pony.local>` id so the sync planner doesn't mistake
  the duplicate for a move (cross-folder identity is keyed on MID and
  multi-folder identity is a deferred feature). Cross-account copies
  preserve the original `Message-ID` — accounts are independent
  identity namespaces, so a true copy keeps IMAP thread integrity
  intact.
- **Automatic mirror rescan for local accounts on TUI startup**: local
  accounts (`account_type = "local"`) have no sync step, so files added
  or removed in the mirror by external tools (offlineimap, getmail,
  procmail, Emacs/Gnus) never reached the SQLite index. `pony tui` now
  reconciles the delta before the reader opens — new files are
  projected and indexed, rows whose files vanished are pruned, and
  pending-append rows with empty `storage_key` are preserved so the
  sync engine can still push them upstream. A per-folder liveness
  line, an announcement of the planned work, and a per-item progress
  bar are rendered on stderr so startup is never silent, even on
  large mbox archives.

### Changed

- **Index schema bumped to version 2. Existing v1 databases refuse to
  open.** Detected via `PRAGMA user_version` with a legacy-table
  sentinel so DBs that predate schema stamping are still flagged. The
  in-place migration is intentionally deferred; the guided reset above
  is the recovery path. Users upgrading from 0.4.x will be prompted on
  first run.
- **`body_preview` removed from CLI and MCP responses.** `pony message
  get` no longer prints a `Preview:` block, and MCP `search_messages` /
  `get_message` no longer include `body_preview` in the returned dict.
  The index is now pure metadata; callers that need body text fetch it
  from the mirror via `pony message body` or MCP `get_message_body`.
  This keeps the FTS5 index lean and removes a redundant, lossy copy
  of the body. The 4000-byte per-MIME-part cap on projected previews
  is also gone; a 256 KB byte-safe cap is applied once on the final
  collapsed text.

### Fixed

- **RFC 2047 encoded-words with unknown charset labels** (e.g.
  `=?x-unknown?Q?…?=`) no longer abort mbox import. Python's
  `bytes.decode` raises `LookupError` on unregistered codec names —
  `errors="replace"` only handles bad bytes inside a *known* codec —
  so one malformed header could tank the whole ingest.
  `_decode_header` now falls back to latin-1 (a total mapping that
  never fails) when the declared charset is unknown, yielding a
  lossless best-effort string instead of crashing.

## [0.4.0] - 2026-04-18
### Fixed

- **Mirror retrieval for IMAP-synced mail**: the `MirrorRepository`
  protocol silently required `MessageRef.message_id` to equal the
  backend's on-disk storage key. For messages ingested from IMAP,
  that field holds the RFC 5322 `Message-ID` header instead; the real
  storage key lives on `IndexedMessage.storage_key`. Every caller that
  did not apply the kludge of rebuilding a `MessageRef(message_id=
  storage_key)` silently failed:
    - `pony rescan` counted every synced message as "missing" and never
      refreshed its projection.
    - `pony message body` and MCP `get_message_body` returned *"not
      found in mirror"* / `null` for every synced message.
    - `PushDeleteOp` silently no-opped the mirror delete; sync removed
      the index row but left the file behind, so mirrors grew
      unbounded on every archive.
    - The retention-based trash purge hit the same bug.
- **HTML `<style>` / `<script>` content in body previews** ([0.3.x
  regression]): the previous regex-only tag stripper left the CSS rules
  from `<style>` blocks and Outlook conditional comments as literal
  preview text. New `pony.html_sanitize` module strips comments (including
  conditional comments), `<head>`, `<style>`, `<script>`, and `<noscript>`
  blocks before tag removal, and decodes HTML entities.
- **Sync deadlock on folder transitions**: `_execute_folder_plan` ran a
  background producer thread that called `session.fetch_messages_batch`
  while the main consumer thread concurrently called other
  `session.*` methods on the same `imapclient` / `imaplib` session.
  `imaplib` is not thread-safe; two threads racing one socket
  occasionally interleaved commands (e.g. two `SELECT INBOX`s with
  consecutive tags) and deadlocked `imaplib`'s tag dispatcher. The
  TUI would then freeze with `Q` unable to cancel. Fixed by
  restructuring per-folder execution into two phases: Phase 1 runs
  session-free ops (`FetchNewOp`, `ServerDeleteOp`, `ServerMoveOp`,
  `PullFlagsOp`, `MergeFlagsOp`, `LinkLocalOp`, `RestoreOp`) with the
  producer holding exclusive use of `session`; Phase 2 runs
  session-touching ops (`PushFlagsOp`, `PushDeleteOp`, `PushMoveOp`,
  `PushAppendOp`, `ReUploadOp`) serially on the main thread after the
  producer has joined. The producer is now the sole thread that ever
  touches the session while it is running.

### Changed

- **`MirrorRepository` protocol**: now keys every method off the
  backend's own `storage_key` (the maildir filename or mbox integer)
  instead of a `MessageRef`. `store_message`, `list_messages`, and
  `move_message_to_folder` return `str` storage keys rather than
  synthetic `MessageRef`s. The layering is now honest: RFC 5322
  identity is an index-side concern; mirror methods do not see it.
- **`SqliteIndexRepository.purge_expired_trash`** now returns
  `list[tuple[FolderRef, str]]` so callers can clean the mirror.
- **TUI `R` / `u` are now explicit set/clear**: pressing `R` always
  marks the target(s) as read; `u` always marks them unread.  (`R`
  previously toggled `SEEN`, which is inconsistent with its label
  and would produce chaotic results on mixed-state bulk selections.)
  `!` still toggles `FLAGGED`.
- **Shared TUI key bindings**: `src/pony/tui/bindings.py` now holds
  `MARK_BINDINGS` (`m` / `Shift+Down` / `Shift+Up`) and
  `MOTION_BINDINGS` (`n` / `p` / `<` / `>`), used by both the contacts
  browser and the messages panel.

### Added

- **Embedded MCP server**: add `[mcp]` to `config.toml` to have the MCP
  HTTP server start automatically in a background thread when `pony tui`
  launches. A TUI notification shows the URL on startup. Recommended for
  users who keep the TUI open and want simultaneous MCP client access
  without managing a separate process.
- **`pony rescan [account]`** CLI command: re-project every indexed
  message from local mirror bytes. Refreshes cached fields (sender,
  recipients, subject, body_preview, has_attachments, received_at)
  without re-downloading from IMAP. Preserves all sync state.
- **`pony folder list [account]`**, **`pony message get`**, **`pony
  message body`**: CLI counterparts to the existing read-only MCP
  tools. `folder list` shows indexed counts and last-sync status per
  folder.
- **Richer MCP `list_folders` output**: each folder entry now includes
  `message_count`, `highest_uid`, and `synced_at` (matching what the
  CLI displays).
- Regression tests pinning the mirror identity contract: a conformance
  case verifies the returned storage_key is distinct from the RFC 5322
  Message-ID; sync tests round-trip a synced message through
  `mirror.get_message_bytes` and verify `PushDeleteOp` plus the
  retention purge actually remove mirror files.
- **Multi-select in the messages panel**: `m` / `Shift+Down` /
  `Shift+Up` mark rows (the icon cell shows `*` while marked,
  replacing the normal `!` / `+` / blank glyph — no new column).
  The existing action keys `R` / `u` / `!` / `d` / `A` act on every
  marked row when any are marked, falling back to the cursor row
  otherwise.  Marks clear on folder switch, search entry/exit, and
  after any bulk action.  Bindings mirror the contacts browser.

## [0.3.0] - 2026-04-17
### Added

- **Standalone executables with bundled documentation**: each release now
  ships two artifacts per platform — a platform installer and a portable
  archive suitable for Homebrew, Scoop, or similar package managers.
    - **Windows**: Inno Setup `.exe` installer (with optional PATH
      registration) and a portable `.zip`.
    - **macOS**: drag-to-Applications `.dmg` and a portable `.tar.gz`.
    - **Linux**: self-contained `.AppImage` and a portable `.tar.gz`.
- **Offline documentation**: the pre-built MkDocs HTML site is bundled
  inside the binary via PyInstaller's `--add-data`. `pony docs` opens
  the bundled docs in the default browser; falls back to the GitHub Pages
  URL when running from source.
- **`pony docs` command**: open the documentation without leaving the
  terminal.
- **MCP server** (`pony mcp-server`): exposes read-only mail operations as
  MCP tools for use with any local or networked MCP client. Tools:
  `search_messages`, `list_folders`, `list_messages`, `get_message`,
  `get_message_body`, `search_contacts`, `get_sync_status`. Runs over
  stdio by default (local use); pass `--port N` for Streamable HTTP
  (Docker / remote deployments). HTTP mode is compatible with running
  `pony tui` in a separate process. New runtime dependency: `mcp>=1.0`.
- **`scripts/build.py`**: cross-platform local build script. Run with
  `uv run python scripts/build.py [--installer] [--skip-tests]
  [--skip-docs]`. Artifacts land in `artifacts/`.
- **`pony.spec`**: PyInstaller spec file controlling what is bundled
  (docs, config sample, platform icon). Replaces the ad-hoc command
  previously generated inline by the CI workflow.

### Changed

- **`release-build.yml`** modernised: switched from bare `pip install` to
  `uv sync`; MkDocs site is built before PyInstaller so docs are always
  bundled; Inno Setup is installed via Chocolatey on Windows runners;
  deprecated `actions/upload-release-asset@v1` replaced with
  `gh release upload`.
- **`pyproject.toml`**: added `[dependency-groups] build` group containing
  `pyinstaller>=6.0`. Install with `uv sync --group build`.

## [0.2.0] - 2026-04-17

### Added

- **Archive action**: press ++shift+a++ in the TUI to move the selected
  message into the account's archive folder. Configure with
  `archive_folder = "..."` on any IMAP account. The move is applied
  immediately in the local mirror and index; the next sync pushes it to
  the server. The archive folder is created on the server automatically
  on first use via the same machinery that handles manual folder
  creation.
- **New folder action**: press ++shift+n++ in the TUI to create a folder
  in the current account's local mirror. The next sync compares local
  mirror folders against server folders and issues `IMAP CREATE` for any
  folder that exists only locally. Deletion of folders is intentionally
  not supported.
- **Generalised local-move sync**: `uid IS NULL` on an index row is now
  the canonical signal that a row must be pushed to the server. The sync
  planner introduces three new operation types that cover archive and
  any future local mutation that round-trips through sync:
    - `PushMoveOp` — run `UID MOVE` (RFC 6851) or `UID COPY` +
      `\Deleted` + `EXPUNGE` when a local pending row is in a different
      folder than the server's current location.
    - `PushAppendOp` — `APPEND` the mirror bytes when the server has no
      copy of the message anywhere.
    - `LinkLocalOp` — adopt a freshly-assigned server UID into the
      existing pending row, no refetch, no duplicate mirror file.
- **Local-only folders propagate upstream**: the planner diffs mirror
  folders against server folders at the top of the execution pass; any
  folder present only locally and passing the sync policy gets a
  server-side `CREATE` before per-folder ops run. `AccountSyncPlan`
  gains a `creates` field.
- `MirrorRepository.move_message_to_folder()` for cross-folder relocation
  of mirror bytes (rename in Maildir; copy-and-delete in mbox).
- `MirrorRepository.create_folder()` for creating empty mirror folders
  (idempotent).
- `ImapClientSession.move_message()` in the session protocol, with RFC
  6851 `UID MOVE` fast path and a compatible fallback.
- `ImapClientSession.create_folder()` (idempotent).
- **Application icon**: a coral pony-head + envelope mark ships under
  `icons/` as `.png`, `.svg`, `.ico` (Windows), and `.icns` (macOS).
  Release builds embed the platform-appropriate icon via PyInstaller's
  `--icon` flag; the MkDocs site uses it as the header logo and
  favicon; the README displays it above the title.

### Changed

- Release workflow: CHANGELOG.md is now the source of truth for the
  release version. Write a new undated `## [X.Y.Z]` heading and trigger
  the workflow; it propagates the version to `pyproject.toml` and
  `version.py`, stamps the date, and tags. The only guard is that the
  tag `vX.Y.Z` must not already exist.
- Sync algorithm documentation (`ai/SYNCHRONIZATION.md` and
  `docs/synchronization.md`) updated to describe the `uid IS NULL`
  signal, the new operation types, and the local-move flow. Conflict
  taxonomy gains C-9 for pending local moves.

## [0.1.0] - 2026-04-17
First feature-complete release of Pony Express.

### Added

- **IMAP sync engine**: two-pass plan/execute architecture with three-way flag
  merge, mass-deletion protection (20% threshold), UIDVALIDITY reset handling,
  and per-folder SSL/port configuration.
- **Maildir and mbox storage**: per-account configurable local mirrors with
  shared conformance tests across both backends.
- **SQLite index**: unified message table with full-text search across sender,
  recipients, subject, and body; case-sensitive and case-insensitive modes;
  sync checkpoints and pending operations.
- **Batched SQLite transactions**: `connection()` context manager with
  thread-local reuse and reentrant nesting for efficient bulk operations.
- **Terminal UI (Textual)**: three-pane reader (folder list, message list,
  message preview) with screen-specific keybinding isolation.
- **Composer**: reply, forward, compose from scratch; `ctrl+x` prefix chord
  for send, attach, external editor, Markdown toggle, cancel.
- **Markdown composition**: `ctrl+x m` toggle per message; produces
  `multipart/alternative` (plain Markdown source + rendered HTML) via
  `markdown-it-py`.
- **Search**: query parser supporting `from:`, `to:`, `cc:`, `subject:`,
  `body:`, `case:yes`, bare words, and quoted phrases; search dialog in TUI
  scoped to folder or account.
- **SMTP sending**: SSL and STARTTLS support; sent/draft folder auto-discovery;
  failure recovery with draft save option.
- **Person-centric contacts**: multiple emails per contact, aliases, affix,
  organization, notes; auto-harvest from To/Cc during sync; ranked
  autocomplete in composer.
- **Contacts browser/editor**: DataTable with search, mark, delete, merge;
  edit screen with all fields; detail view with `Enter`.
- **BBDB import/export**: bidirectional sync with Emacs BBDB v3 files;
  `bbdb_path` config option for auto-sync on `pony sync`; smart merge by
  email matching.
- **Sync progress reporting**: `ProgressInfo` dataclass with per-folder and
  per-operation callbacks; TUI progress bar; CLI counter line.
- **Diagnostics**: `pony doctor` checks Python version, config, index DB,
  mirror paths, mirror integrity (orphan files, stale index rows), and
  optional dependencies.
- **Four credential backends**: plaintext, environment variable, external
  command, OS-encrypted blob (DPAPI on Windows, PBKDF2+SHAKE-256 on
  Linux/macOS).
- **Cross-platform support**: `pathlib.Path` throughout, XDG/APPDATA/
  LOCALAPPDATA resolution, `_sanitize_for_path` for unsafe characters.
- **CLI commands**: `pony tui`, `pony sync`, `pony compose`, `pony search`,
  `pony doctor`, `pony server-summary`, `pony local-summary`, `pony reset`,
  `pony config edit`, `pony account add`, `pony account set-password`,
  `pony contacts browse/search/show/export/import`, `pony --version`.
- **HTML rendering**: style and script block stripping for clean plain-text
  display of HTML-only emails; `w` key opens full HTML in browser.
- **Documentation**: MkDocs Material site with configuration reference, CLI
  reference, TUI guide, composer guide, contacts guide, sync overview,
  architecture overview, and development guide; automated GitHub Pages
  deployment.
- **Release automation**: PyInstaller-based multi-platform builds (Linux,
  macOS, Windows) triggered by GitHub releases.
[0.1.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.1.0
[0.2.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.2.0
[0.3.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.3.0
[0.4.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.4.0
[0.5.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.5.0
[0.6.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.6.0
[0.7.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.7.0
[0.8.0]: https://github.com/juanjosegarciaripoll/pony/releases/tag/v0.8.0
