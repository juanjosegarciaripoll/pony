---
title: Architecture
---

# Architecture

## Overview

Pony Express is organized as a layered application that separates user-facing
workflows from protocol, storage, and indexing concerns. Multiple interfaces
share the same core, and two of them are different kinds of interface: a
terminal UI built on Textual (`pony tui`) and a browser UI built on Starlette
(`pony web`). Both are optional extras, both read the same keymap, and both
call the same actions — see [Actions and projections](#actions-and-projections)
and [The browser front end](#the-browser-front-end). Within the terminal, the
TUI, standalone composer and contacts browser are separate Textual `App`
subclasses that push their own screens.

Mail is one of two subsystems. The other is the calendar (`src/chronos`), a
CalDAV client with its own mirror, index, sync engine and screens, which
Pony Express hosts rather than launches: ++f2++ puts the agenda in front of
the mail reader and ++f2++ again brings the mail back. One process, one
configuration file, one notification space. See
[Mail and calendar as one program](#mail-and-calendar-as-one-program).

## Package layout

```
src/
  pony/     # mail
  chronos/  # calendar
```

```
src/pony/
  __init__.py
  __main__.py          # python -m pony entrypoint
  version.py           # __version__ (stamped by the release workflow)
  cli.py               # argparse command dispatch, both halves of it
  cli_calendar.py      # the calendar's commands (`pony calendar ...`)
  config.py            # TOML config loader and validator
  domain.py            # typed core data models
  protocols.py         # repository and service interfaces
  paths.py             # application directory resolution
  accounts.py          # account lookup and mirror construction
  credentials.py       # plaintext / env / command / encrypted backends
  storage.py           # Maildir and mbox mirror repositories
  index_store.py       # SQLite metadata index repository
  storage_indexing.py  # mirror-to-index projection: rescan, reindex, ingest
  message_projection.py# RFC 5322 parsing and metadata projection
  message_copy.py      # byte-faithful RFC 5322 duplication for copy actions
  mailbox_ops.py       # index-row rewrites for local moves, copies and flags
  mailbox_actions.py   # archive / move / copy / trash / flags, over the stores
  delivery.py          # filing a message that has been written or sent
  contact_actions.py   # harvest, list, the form's fields, delete, merge
  contact_merge.py     # what one record holds when two are combined
  invitation_actions.py# file an invitation, answer its organizer
  message_export.py    # the parts of a message (0 it, T its text, 1..N), saved
  keymap.py            # which key does what in the mail reader
  views.py             # what a row and the folder tree are, before drawing
  message_renderer.py  # RFC 5322 -> plain text / browser HTML / attachments
  html_policy.py       # the headers a message's own HTML is shown under
  browser_preview.py   # serves that HTML to the browser over loopback
  handoff.py           # files written only for an external program to open
  authentication.py    # what the receiving server said about the sender
  compose_utils.py     # quoting, address lists, MIME assembly
  composer.py          # what a reply/forward/new message starts as
  search_parser.py     # query language parser
  contact_naming.py    # display name -> contact first/last name
  folder_utils.py      # which folder is which, by configuration or by name
  html_sanitize.py     # shared HTML→text helpers (preview + renderer)
  sync.py              # IMAP sync engine (plan/execute) + plan formatters
  imap_client.py       # ImapSession wrapper around imaplib
  smtp_sender.py       # SMTP submission
  bbdb.py              # BBDB v3 reader/writer
  calendar.py          # the [calendar] config section + opening its stores
  invitation.py        # iTIP: reading invitations, writing replies/requests
  notifications.py     # the notification space mail and calendar share
  pdf_export.py        # message HTML -> PDF via an external converter
  services.py          # doctor diagnostics, mirror integrity
  fixture_flow.py      # deterministic fixture ingest flow
  mcp_server.py        # MCP server (stdio + TCP bridge via tinymcp)
  tui/
    app.py             # PonyApp, ComposeApp, ContactsApp, EmlViewerApp
    bindings.py        # shared mark/motion Binding tuples
    calendar_host.py   # where the mail app meets the calendar's screens
    pdf_export.py      # thread-worker wrapper around pony.pdf_export
    terminal.py        # OSC sequences for window-title push/pop/set
    ui_state.py        # persisted pane sizes (ui_state.json)
    screens/
      main_screen.py             # three-pane mail reader
      compose_screen.py          # email composer
      sync_confirm_screen.py     # sync plan confirmation
      search_dialog_screen.py    # search query input
      contact_browser_screen.py  # contacts list
      contact_detail_screen.py   # contact detail view
      contact_edit_screen.py     # contact editor
      confirm_screen.py          # generic yes/no dialog
      dialog_screen.py           # base class for modal yes/no dialogs
      link_action_screen.py      # Open / Copy / Cancel dialog for body links
      floating_input_screen.py   # base class for bottom floating-input bars
      save_draft_screen.py       # draft save confirmation
      save_folder_picker_screen.py # directory picker for saving files
      add_attachment_screen.py   # file picker
      attachment_picker_screen.py# pick parts by number (0 = message, T = text)
      eml_viewer_screen.py       # standalone .eml viewer
      invitation_screen.py       # i — accept / decline / file an invitation
      goto_folder_screen.py      # G — fuzzy jump to folder
      new_folder_screen.py       # N — create new folder
      pick_folder_screen.py      # modal (account, folder) target picker
      help_screen.py             # F1 — keybinding cheatsheet
    widgets/
      folder_panel.py        # collapsible folder tree
      message_list.py        # async-streamed message table
      message_view.py        # scrollable message reader
      contact_suggester.py   # autocomplete dropdown
      address_input.py       # address field that keeps pasted lists whole
      edge_drag.py           # mouse-draggable pane borders
  web/                   # the browser front end (extra: pony[web])
    app.py               # Starlette routes over the same actions
    layout.py            # the chrome: tabs, pane frames, footer, overlays
    mail.py              # folder tree, list, reader, the mail actions
    compose.py           # the compose form, sending, staged attachments
    contacts.py          # the contacts list, its form, its bulk actions
    calendar.py          # agenda, timeline and month views, the event form
    syncing.py           # the server's one sync slot
    actions.py           # what a POSTed action is handed
    urls.py              # every link in the front end
    html.py              # escaping element builder, in place of templates
    static/              # one stylesheet, one keymap dispatcher
```

## Subsystems

### Domain layer (`pony.domain`, `pony.protocols`)

Typed domain models and protocol interfaces. Models are frozen dataclasses;
protocols define the contracts for repositories and services. This layer is
free of protocol-specific and UI-specific logic.

Key types: `AppConfig`, `AccountConfig`, `IndexedMessage`, `MessageFlag`,
`MessageStatus`, `FolderRef`, `MessageRef`, `Contact`, `SearchQuery`,
`SyncPlan`, `SyncResult`.

### Configuration (`pony.config`, `pony.paths`)

TOML configuration loading and validation. The config is parsed directly into
domain objects with no intermediate model layer.

`AppPaths` holds two paths — the config file and the `data_dir` that
config declares — and derives every other from the second:
`index_db_file`, `log_dir`, `scan_state_file`, `mcp_state_file` and
`calendar_dir`, so none of them can point at a different directory than
the rest. `paths.resolve()` is the single entry point. A loaded config always
declares `data_dir` — the loader refuses a file without one, and refuses a
relative one — so the platform-convention fallback (XDG on Linux/macOS,
`APPDATA`/`LOCALAPPDATA` on Windows) is what a run uses before or without a
loadable config: where the default `config.toml` is looked for, and where
`pony doctor`, `pony config edit` and `pony reset` work when there is none.
`PONY_ROOT`, when set, contains all of it: `graft` strips a path's anchor
and re-adds it as an ordinary component, because joining an absolute or
drive-qualified path onto a root discards the root.

Path values in the config support `~`, `$VAR`, and `%VAR%` expansion via
`_expand_path`.

### Storage (`pony.storage`)

Mirror repository implementations for Maildir and mbox. Both implement the
same `MirrorRepository` protocol: store, retrieve, list, and delete raw
RFC 5322 message bytes. Storage location mapping connects mirror records to
the SQL index via `storage_key`.

Flag changes are written through to the mirror as well as the index, so
another MUA sharing the same tree sees read, flagged and answered state.
Maildir encodes them in the filename suffix and mbox in the `Status` /
`X-Status` headers. The index remains authoritative: a mirror write that
fails is logged and skipped rather than failing the action.

### Index (`pony.index_store`)

SQLite-backed metadata store implementing `IndexRepository` and
`ContactRepository`. All message state lives in a single unified `messages`
table (no separate server-state table). The `connection()` context manager
provides batched transactions: one connection and one transaction per thread,
reentrant nesting where an inner block is a savepoint-fenced unit of work, and
a commit every 500 writes so a long rebuild stays bounded and resumable. See
[Synchronization → Performance](synchronization.md#performance) for what that
does and does not promise.

Tables: `messages`, `messages_fts`, `contacts`, `contacts_fts`,
`contact_emails`, `contact_aliases`, `folder_sync_state`, `credentials`.

### Sync (`pony.sync`, `pony.imap_client`)

Two-pass IMAP sync engine: plan (read-only comparison) then execute (apply
changes). Three-way flag merge with union policy. Two safety
halts per folder: mass deletion at 20% of a folder's UIDs, and mass upload at
half the folder or 50 messages. Progress callbacks (`ProgressInfo`)
report per-folder scanning and per-operation execution.

There is no pending-operations queue. A local mutation is recorded by
rewriting the message's index row — `uid IS NULL` is what marks it as needing
a push — and the planner is the sole observer, emitting `PushMoveOp` or
`PushAppendOp`. New UIDs are captured from `APPENDUID` / `COPYUID`. Folder
creation works the same way: the mirror exposes a new directory and the
execute phase issues `IMAP CREATE`. The row rewrites themselves live in
`pony.mailbox_ops`.

Connections are bounded on both ends. `ImapSession` sets an explicit
connect timeout (`imap_connect_timeout_seconds`, default 10 s) and a read
timeout, because otherwise the kernel's SYN retry ceiling applies — roughly
130 s per attempt on Linux — and a server that silently drops packets stalls
the sync instead of failing it. Setting the timeout is not sufficient on its
own: imapclient 3.1.0 accepts a connect timeout for TLS connections and then
discards it, so `imap_client` subclasses its TLS transport to apply the value
the way imapclient's own non-TLS transport already does.

A timed-out connect is then retried (`DEFAULT_CONNECT_ATTEMPTS`), including
mid-session reconnects. This is not the same concern as the read-side
`_retry`: networks that spread flows over several paths select the path by
hashing the connection's addresses and ports, so one blackholed path drops a
reproducible share of connections while the rest stay healthy. Each retry
opens a new socket with a new ephemeral source port and so re-rolls that
hash, making it an independent attempt rather than a repeat of the same one.
A single failure therefore says nothing about whether the server is up, which
is why the connect timeout is short — a handshake that will succeed takes
milliseconds.

Since several accounts often share one server, the engine also keeps a
per-host breaker, but it fires only once those attempts are exhausted: the
host is then marked unreachable and the remaining accounts on it fail
immediately for the rest of the run, rather than each paying its own
timeouts. The breaker lives on the service instance, which is rebuilt per
run, so the next sync retries the host from scratch.

Full algorithm: [Synchronization](synchronization.md).

### Send (`pony.smtp_sender`, `pony.compose_utils`, `pony.composer`)

SMTP submission with SSL and STARTTLS. Reply/forward quoting preserves
existing quote levels. Markdown mode builds `multipart/alternative` messages
via `markdown-it-py`. Replies carry `In-Reply-To` and `References` so they
thread in the recipient's client.

`composer.py` decides what a draft *is* — the sending identity, the subject
prefix, which recipients survive a reply-all, the threading headers — and
returns a `DraftSpec` for the UI to render. It does no I/O and imports
nothing from `tui/`, so the same answers serve the TUI composer and anything
else built on top later.

Connecting is bounded and retried on the same terms as IMAP, and for the same
reason: `smtplib` leaves the socket blocking unless given a timeout, so a
dropped SYN costs the kernel's full retry ceiling. `smtp_connect_timeout_seconds`
(default 10 s) bounds one attempt and `DEFAULT_CONNECT_ATTEMPTS` decides how
many are made, each on a fresh source port. Only the connect is retried —
past `DATA` a retry risks delivering the message twice. Every failure leaves
`smtp_sender` as an `SMTPError` whose text names the host and repeats what
the server said, so the composer can report a rejected recipient differently
from an unreachable server.

The compose screen runs the send in a Textual worker, keeping only the
blocking SMTP call off the message pump while the surrounding widget work
stays on it. An unreachable server can otherwise hold the event loop for the
timeout times the attempt count, which freezes the whole app rather than just
the send. A `_sending` guard rejects a second `ctrl+s` while one is in
flight, since the binding stays live throughout.

### Actions and projections

There is one line through both halves of the program: **what an operation
does** is decided over the stores, and **how it looks** is decided by
whatever is on screen.

The *actions* are the operations a user asks for — archive, move, copy,
trash, a flag change, a new folder, saving an event, filing a sent
message, merging two contacts. Each takes the stores it needs and
returns a `chronos.outcome.Outcome`: a count of what changed and the
`Notice`s to show, each carrying its own severity. None of them imports
Textual, calls `app.notify`, or touches a widget.

| Module | Operations |
|---|---|
| `pony.mailbox_actions` | archive, move, copy, trash, flags, folder creation, retiring a message, planning a deduplication |
| `pony.delivery` | filing a draft or a sent copy, and sending: the password, the pre-send draft, SMTP, the Sent copy |
| `pony.contact_actions` | harvesting an address, listing, the form's fields, deleting, planning and applying a merge, importing an address book |
| `pony.invitation_actions` | filing an invitation, answering its organizer, inviting an event's attendees |
| `pony.message_export` | the parts of a message, saved to disk or handed to a viewer |
| `chronos.event_actions` | saving, rescheduling, trashing and importing an event, and what a blank form starts with |

The *projections* are what a view is of: `pony.views` turns an indexed
message into the four fields and the one state a list row shows, or a
contact into its five columns, and `chronos.views` holds the calendar's
window arithmetic, the index query each view needs, the strings its rows
are made of, and the grid geometry — which month cell a span covers,
which day columns a trip occupies, how wide the hour axis has to be.
Neither knows how a row will be drawn. Where a row needs emphasis — unread mail, an event
in progress, one already over — it says so as data (`MessageRow.unread`,
`RowEmphasis`) and leaves bold, dim or a CSS class to the renderer.

A screen is then the part that is genuinely terminal-specific: it
gathers what the cursor and the marks point at, calls one action, turns
each notice into a toast, repaints, and owns the workers and modal
dialogs. `MainScreen._act_on_targets` is that shape written once for
every list action.

Two consequences are the point of the arrangement. A second front end
offers the same operations without reimplementing any of them — it
renders `Notice`s and `MessageRow`s its own way. And the operations are
tested directly, against real SQLite and real maildirs, with no
application running: `tests/test_mailbox_actions.py`,
`tests/test_contact_actions.py`, `tests/test_views.py` and
`tests_calendar/test_event_actions.py` finish in seconds, where reaching
the same code through a `Pilot` costs a second an assertion.

`chronos.outcome` lives on the calendar's side of the dependency arrow
for the same reason `chronos.scheduler` does: `pony` imports `chronos`,
never the reverse, so anything both halves share has to sit there.
Nothing in it knows about calendars, and `pony.notifications` takes its
`Severity` from it rather than spelling the three levels out twice.
`chronos.atomic` (replacing a file without leaving half of one),
`chronos.alarms` (which reminders are due) and `chronos.tui.reporting`
(an `Outcome` as toasts, and the hop onto the UI thread) are there for
the same reason: both halves ask them.

### TUI (`pony.tui`)

Three separate Textual `App` classes, each minimal:

- **`PonyApp`** (`pony tui`): pushes `MainScreen` on mount. Owns only the
  ++shift+q++ (quit) and ++f1++ (help) bindings. All mail-specific bindings (sync, compose, flags,
  attachments, search, contacts) live on `MainScreen`.
- **`ComposeApp`** (`pony compose`): pushes `ComposeScreen` on mount, exits
  on send or cancel.
- **`ContactsApp`** (`pony contacts browse`): pushes `ContactBrowserScreen`
  on mount, exits on dismiss.
- **`EmlViewerApp`** (`pony view FILE`): pushes `EmlViewerScreen` for a single
  `.eml` file, with no index or account involved.

Each screen owns its own bindings and shows only its relevant keybindings in
the footer. Screens communicate upward via Textual messages or callbacks
passed at construction time, not by calling private App methods.
`SyncConfirmScreen` takes an `on_confirm` callback rather than reaching back
into the app.

What a screen does *not* do is decide what an action means: that lives in
the action modules above, and the screen reports the outcome. The
message list is the same split one level down — `pony.views` decides the
fields of a row, and the widget pads them into fixed-width columns,
which is the only part of it that means anything in a terminal.

Alongside the modal `g` sync flow there is a non-blocking background sync that
shows a spinner on the `FolderPanel` border title. It has nobody to ask, so it
confirms every folder that is safe and skips one that tripped a safety halt
(see [Synchronization → Safety halts](synchronization.md#safety-halts)). It
does not belong to the screen: `PonyApp` owns a `PeriodicSync`
(see [Periodic sync](#periodic-sync)) that runs it on a thread of its own,
config-gated by `background_sync_enabled` / `background_sync_interval_seconds`.
`ctrl+g` asks that scheduler for a run now, which also starts the cadence if
the config gate is off. The same border title carries the schedule: when a
periodic sync is scheduled the panel shows a clock and a live countdown to
the next run (`Folders ◷ 9:30`), read straight off the scheduler's deadline,
so a run that was deferred or triggered by hand is reflected rather than
guessed. The countdown repaints every 30 s and is rounded down to the same
step, so the title never shows precision it has not got. A sync in flight
outranks the countdown, so the spinner replaces it and it returns when the
sync ends.
`MessageListPanel.load_folder` makes one SQL call for the folder's row
summaries and then streams their insertion into the `DataTable` in batches, so
a 10k-row folder paints progressively instead of in one blocking burst. Both
halves are one Textual worker: the query runs in a thread
(`asyncio.to_thread`), which the index supports because it opens one
connection per thread. It is not paginated.

Between the call and the rows the panel is **awaiting rows** — nothing
selected, nothing to act on, and a border that says so — and it posts
`RowsReady` when the query answers, before the first row is inserted.
Anything that has to read the panel back after reloading it waits for that
message: the reader pane follows the cursor onto whatever took a trashed
message's place, and an action pressed in the gap is told the list is still
loading rather than silently doing nothing. A read that *fails* ends the
state too — an empty list, a border saying so, and one notice — because a
panel still awaiting rows that are not coming answers every key that way
for the rest of the session. A search is the same worker and the same
state; the screen owns the query and hands the panel the read
(`load_search(..., fetch=…)`) rather than its result.

The correspondent column shows the sender, except in a Sent folder, where it
shows the recipients: there the sender is the user on every row, and the To
address is the only thing that tells the rows apart. `MainScreen` decides
which, from the account's `sent_folder` setting when it is configured and
otherwise from `folder_utils.is_sent_folder`, which matches the last path
segment against the names servers localise the folder into (`Sent`,
`Enviados`, `Envoyés`, `Gesendet`, …) case- and diacritic-insensitively.
Search results span folders and always show the sender.

`tui/terminal.py` updates the host terminal title via OSC 2 and restores it on
exit, and opens attachments: `resolve_viewer_command` looks the part's content
type up in the `[viewers]` config table and `launch_file` runs the match with
the saved path appended, falling back to the OS handler (`xdg-open` / `open` /
`ShellExecute`) when nothing matches.  The association therefore travels with
`config.toml` instead of depending on the desktop MIME database. `tui/bindings.py` holds the mark/motion `Binding` tuples shared by the
message list and the contact browser. `tui/ui_state.py` persists pane sizes to
`ui_state.json`. Theme selection comes from `theme` in `config.toml`, the
`--theme NAME` flag, or `--list-themes`.

### The browser front end

`pony web` serves the same stores over HTTP (`pony.web`, Starlette and
Uvicorn, installed by the `web` extra). It is a second front end, not a
second program: every operation is a call into the action modules, every
row comes from `pony.views` or `chronos.views`, and every key comes from
`pony.keymap` or `chronos.keymap`. What is new in the package is HTTP,
HTML and the keyboard dispatch.

What it offers is what the terminal offers: the folder tree, the message
list, the reader, archive/trash/flag/mark-read, move, copy, new folder,
goto folder, search, harvest contacts, compose and send (new, reply,
reply-all, forward, resumed draft, attachments), answering an
invitation, syncing, the contacts browser with its four operations, and
the calendar's four views with its event form. Each page is one pane
layout filling the window, and each pane scrolls on its own: the window
is the application, not a document to scroll.

| Module | What it holds |
|---|---|
| `pony.web.app` | the routes, the token gate, the action table |
| `pony.web.layout` | the chrome: tabs, pane frames, footer, overlays |
| `pony.web.mail` | folder tree, list, reader, the mail actions |
| `pony.web.compose` | the compose form, sending, the attachment staging area |
| `pony.web.contacts` | the contacts list, its form and its bulk actions |
| `pony.web.calendar` | agenda, timeline and month views, the event form |
| `pony.web.syncing` | the server's one sync slot |
| `pony.web.html` | the escaping element builder |
| `pony.web.urls` | every link in the front end, built in one place |

**One keymap, two front ends.** `chronos.keymap` holds the vocabulary —
`KeyBinding(key, action, description, surface)`, `for_surface`,
`web_key` — and the calendar's table; `pony.keymap` holds the mail
reader's. `pony.tui.bindings.to_textual` and
`chronos.tui.bindings.to_textual` turn a slice into Textual `Binding`s —
which is where `MainScreen.BINDINGS`, `PonyApp.BINDINGS`,
`ComposeScreen.BINDINGS`, `ContactBrowserScreen.BINDINGS` and the
calendar's screens come from — and every page carries its own
surface's table inline, so a key pressed before the script settles is
not dropped; `/keymap.json` still serves them for anything that asks. Each
entry carries `available`, so a key the terminal has and the browser has
not (editing the body in `$EDITOR`) is listed in the help and marked,
rather than appearing to work. `tests/test_web_dispatch.py` fails if
the server calls an action available that the dispatcher does not
implement, if a key is spelled so that no press could ever match it, or
if the keys served for a surface stop being the terminal's own table for
it. With `PONY_BROWSER_UI=1` and a Chromium on the machine,
`tests/test_web_browser.py` presses every one of them at the real page
in a headless browser.

++shift+q++ stops Pony from either front end. A browser cannot end a
process, so the page asks the server to end itself: `POST /action/quit`
reaches `WebServices.shutdown`, which `pony web` wires to uvicorn's
exit flag — set rather than signalled, so the request that asked is
answered before the socket closes. The page then closes its own tab,
and says so instead if the browser refuses (a tab the user opened
cannot be closed by script).

**A phone has no keyboard.** Every key that changes something has a
button or a link too — Archive, Trash, Flag, Unread and Write beside the
list and the reader, Delete marked and Merge marked on the contacts
list, Sync in the title bar, New event and the view switcher in the
calendar's pane, and a search field in each list head. A tap on a row's
mark column is what ++m++ is. The buttons carry `data-action`, so they
go through the same dispatcher and the same `POST /action/<name>` as the
keys; `tests/test_web_dispatch.py` fails if one names an action the
server does not have, and names the operations that have no button yet,
so that lengthening the list is a deliberate act rather than an
oversight.

**Addressing.** Account, folder and message travel as query parameters,
not path segments: a folder may be called `INBOX/2024`, which no path
segment can hold, and percent-encoding does not help because the ASGI
scope carries a decoded path. `pony.web.urls` builds every link, so a
route and the links to it cannot drift apart.

**No template engine and no CDN.** `pony.web.html` builds markup from
Python with escaping that a template author cannot forget; `Raw` is the
only way past it, which makes injection auditable by grep. The client
side is one stylesheet and one script with no framework, because a
tailnet has no reason to reach the public internet.

**A message body is text, and the HTML version is a page of its own.**
The reader shows what `render_message` produced — the same text the CLI
prints and the MCP tools return, with URLs and addresses linkified and
nothing to execute. `w` opens `/message/body`, which returns
`build_browser_html` output — `cid:` images already inlined as data
URIs — under the headers in `pony.html_policy`, as a top-level page
rather than a frame inside the application. Attachments are downloaded
with `X-Content-Type-Options: nosniff`.

**Attachments go up as bytes.** A multipart form would mean a third
runtime dependency for the sake of one request, so the script posts the
file's bytes to `/compose/attachment` and the server keeps them in a
staging directory it made itself. The form then carries a 16-hex-digit
token, never a path, so nothing the page sends is ever opened as a file.
A forward is the same mechanism: the form carries the message reference
and the server materialises the `.eml` at send time.

**Syncing.** The browser syncs: `g` runs the engine `pony sync` runs and
reports the sentence `describe_sync_result` builds. The server holds one
sync slot of its own (`pony.web.syncing.SyncGate`), so two tabs — or a
tab and the periodic thread, whose slot the gate claims — cannot overlap
on one account. Across processes it is the user's call: the calendar
takes its own lock (`chronos.locking`), the mail half has never had one,
and a terminal left open elsewhere is not a reason for the phone to
refuse. With `background_sync_enabled` set, `pony web` runs the periodic
thread too, so a phone sees new mail without anyone pressing a key.

**Access.** Bound to `127.0.0.1` by default, with an access token that
arrives once in the URL and is then kept in an `HttpOnly` cookie, so a
phone needs the long link only the first time. Published through a
tailnet — either by binding the tailnet address or by pointing
`tailscale serve` at the loopback port — the device is already
authenticated and `--no-token` is reasonable. There is deliberately no
login form: Pony holds one user's credentials in one config file, so
serving it to several people is an instance each, not a session each.

The token is not what stops another web page from acting: a cookie
travels with a cross-site form post exactly as it travels with Pony's
own requests. Every state-changing request is refused unless the browser
says it came from Pony's own page — `Sec-Fetch-Site: same-origin` or
`none`, or an `Origin` naming the host the request was addressed to —
which covers `/action/*`, `/compose`, `/compose/attachment`,
`/contacts/edit` and `/calendar/event` from one place in front of them
all, and holds under `--no-token` as much as with a token. A client that
sends neither header (curl, a script, a phone shortcut) is let through;
it is not a browser being aimed at the mailbox by a page the user
happened to open. The token cookie is `SameSite=strict` for the same
reason, not for the phone's convenience.

### MCP (`pony.mcp_server`)

Read-only server built on `tinymcp` rather than the MCP SDK's HTTP stack. When
the TUI is running it serves on `127.0.0.1` over TCP behind a per-session token
held in a state file; `pony mcp` finds that file and proxies stdio↔TCP, so the
consumer never opens a competing SQLite handle. With no TUI running, `pony mcp`
opens its own connections and serves stdio directly.

### Message rendering (`pony.message_renderer`, `pony.tui.pdf_export`)

`render_message()` produces plain text, stripping `<style>` and `<script>`
first. `build_browser_html()` produces self-contained HTML with CID-resolved
inline images for the `w` key. `pony.pdf_export` feeds that same HTML to
whichever external converter is present (Chromium/Chrome, `wkhtmltopdf`,
WeasyPrint or LibreOffice).

**Who a message is from is the receiving server's answer, not Pony's.**
`pony.authentication` reads the first `Authentication-Results` header
(RFC 8601) into a verdict on `RenderedMessage`, so the terminal, the
browser and the printed page show one answer decided in one place. It
reaches the reader as one padlock on the `From` line, because a header
area is for the message's own headers; what the padlock means takes a
paragraph, so it is a click or `L` away — `SenderCheckScreen` in the
terminal, a scriptless `<details>` in the browser and on the printed
page. That paragraph is not decoration: a shut lock is read as "safe",
and the only honest place to say it means something narrower is
somewhere with room for a sentence. It
reads only the topmost header, because a message can arrive carrying
reports the sender wrote, and it compares the signing domain against
the `From` domain, because a signature that passes for some other
domain is what phishing looks like. Pony verifies no signature itself:
DKIM is a DNS check, and the key may have rotated by the time the
message is in the mirror.

**A message's HTML is never shown inside either front end, and never as
a file.** It is a page of its own, and both front ends serve it: the
browser at `GET /message/body`, the terminal through
`pony.browser_preview`, which binds `127.0.0.1` on a kernel-chosen port,
answers one unguessable path with the document held in memory, and
closes after two minutes or when the next message is opened. Nothing of
the message reaches the disk, and because a `file://` document carries
no headers while a served one does, the restrictions apply to both.
Those restrictions are one definition in `pony.html_policy`, which
neither transport owns, so the two pages cannot drift apart.

`browser_preview` is not the browser front end: `http.server` from the
standard library answers one GET, so the `w` key works on a
terminal-only install with no `web` extra and no Starlette.

Nothing about that conversion is terminal-specific, so it lives outside
`tui/`: `pony view --pdf` converts `.eml` files from the command line with
no UI at all, which is what makes converting a directory scriptable.
`tui/pdf_export.py` adds only what a running app needs — the blocking call
on a thread worker, with every outcome reported through the app instead of
raised at the caller — for `ctrl+p`.

One predicate decides what counts as an attachment, so the reader pane, the
browser view, the PDF export and the CLI extractor cannot disagree about a
message's contents.

## Mail and calendar as one program

The calendar is a complete client in its own right — CalDAV sync, an `.ics`
mirror, a SQLite index with a recurrence and alarm cache, and its own
Textual screens. Pony Express does not start it as a separate program; it
hosts its screens.

```
src/chronos/
  config.py domain.py paths.py protocols.py outcome.py
  atomic.py            # replacing a file without leaving half of one
  shell.py             # splitting a command line the way the platform quotes it
  alarms.py            # which reminders are due, and what each one says
  storage.py index_store.py storage_indexing.py locking.py scheduler.py
  ical_parser.py recurrence.py mutations.py ingest.py
  event_actions.py     # save / reschedule / trash / import, over the stores
  services.py          # what an operation works over, the sync runner, the doctor
  keymap.py            # the keyboard vocabulary, and the calendar's keys
  views.py             # what each view is of, grids included, before drawing
  caldav/ http/ oauth.py credentials.py authorization.py sync.py
  bootstrap.py         # the [calendar] template a first run writes
  mcp_server.py
  tui/
    app.py             # ChronosApp, TuiServices, CalendarHost, CALENDAR_CSS
    reporting.py       # an Outcome as toasts, and the hop onto the UI thread
    screens/           # agenda, day, grid, month, event edit, …
    widgets/           # calendar panel, timeline grid, month grid, …
```

### One application

`PonyApp` owns both screen stacks. The agenda is *pushed over* the mail
screen rather than replacing it, so returning finds the same folder, cursor
row and reader scroll position. Three things make this work:

- **`CalendarHost`** (`chronos.tui.app`) is the protocol the calendar's
  screens read their dependencies from. `ChronosApp` satisfies it when the
  calendar runs on its own; `PonyApp` satisfies it here. The calendar's
  screens were already decoupled from their app, so this is the only
  coupling point.
- **`CALENDAR_CSS`** is the calendar's stylesheet, lifted out of its app
  class so a host can adopt it. `App.CSS` is global, but its rules select
  calendar widgets, which exist only under the calendar's own screens.
- **`pony.tui.calendar_host`** is the single module that imports from both
  halves: it builds the calendar's `TuiServices` (including the OAuth flow,
  which has to run as a screen on the application it interrupts), formats
  the line each half shows about the other, and turns due reminders into
  announcements.

++f2++ is an application binding, so it works from either side. It is
ignored while one of the calendar's own dialogs is up: stacking a second
agenda on top would be nonsense, and popping back to the mail side would
throw the dialog away with whatever is half-typed in it.

The dependency arrow points one way — `pony` imports `chronos`, never the
reverse. What the calendar needs *from* a host arrives as injected
collaborators on `TuiServices`: `invitation_sender` and
`attendee_completer`. Both are optional, and without them the calendar
behaves exactly as it does standalone.

**Where the calendar's files are is one of those injected things.** The
calendar resolves no platform directory of its own: a `CalendarPaths`
(`chronos.paths`) is built from `AppPaths.calendar_dir` by
`pony.calendar.calendar_paths` and travels as an argument — on
`CalendarRuntime`, on `CliContext`, on `TuiServices`, into the credentials
provider. Nothing asks a global where the calendar is, which is why two
roots can exist in one process (the two test suites do exactly that) and
why a configuration file means the same thing under either: an account's
`mirror_path` is kept as the file declared it, and
`chronos.config.mirror_roots` resolves it against whichever root it is
given. `python -m chronos` therefore has no root to run under, and says
so rather than failing later.

### One notification space

`pony.notifications.NotificationCenter` takes every announcement from either
half, and the application renders it once — a toast, the terminal bell, and
an OSC 777 desktop notification. Going through the terminal is what makes a
reminder arrive on the machine the user is looking at rather than on the far
end of an SSH session.

Announcements come in two kinds:

- **Urgent** — a calendar reminder falling due. Shown wherever the user is,
  including while the calendar itself is on screen.
- **Ordinary** — mail a sync has just fetched. Shown only when the agenda is
  the screen in front of the user, because the mail reader reports its own
  sync results itself and a second toast saying the same thing is noise.

Nothing is persisted. Mail and calendar each already keep the state that
outlives a session (unread flags, an alarm's `fired_at`), so a second store
would be a second truth.

### Periodic sync

Both halves sync themselves on a timetable, and neither timetable belongs to
a screen. `chronos/scheduler.py` holds one small, subsystem-agnostic class,
`PeriodicSync`: a daemon thread that runs a sync runner every *interval*
seconds, hands it a cancel event, and reports each run through callbacks. It
lives in `chronos` because the dependency arrow points that way — the mail
half imports it for its own sync, over its own result type (`MailSyncScheduler`
in `pony/sync.py`, `CalendarSyncScheduler` in `chronos/protocols.py`).

The applications own the instances: `PonyApp` starts one per half on mount
and stops both on unmount, and `ChronosApp` starts the calendar's when the
calendar runs alone. That is what makes the cadence independent of what is on
screen, which is the whole point — Textual destroys a popped screen and its
timers with it, so the agenda's old screen-owned timer restarted from zero
every time the user pressed ++f2++, and the mail application needed a second
timer of its own to cover the gap. One thread each replaces all of that.

Screens are observers. They read `next_run_at` to paint their countdown, call
`request_now()` for the key the user pressed, and are told how a run went
through `on_sync_started` / `on_sync_finished`, which the application marshals
onto the UI thread — the callbacks themselves arrive on the scheduler thread,
which is why the class stays free of Textual.

Two syncs against one account at the same time is what must not happen. The
foreground flows own their own worker, so they claim the scheduler's slot
(`try_claim` / `release`, or the `hold()` context manager) for as long as they
hold it, the user's thinking time in a confirmation dialog included. A
periodic turn that finds the slot taken is **postponed, not dropped**: it
tries again shortly. A run nobody asked for reports nothing on success and
logs its failures; its whole purpose is that the mail, the reminders and the
next event stay true without being talked about.

### One line about the other half

Both screens expose the same `set_companion_status(text)` call, filled by the
application: the mail reader carries the next event beside the open folder,
and the agenda carries the unread mail count beside its sync countdown.
Neither line appears when there is nothing to say.

### Invitations

`pony.invitation` is the shared vocabulary for iTIP (RFC 5546) in both
directions. It does no I/O — importing is `chronos.ingest`'s job and sending
is `pony.smtp_sender`'s — and it parses with the calendar's own iCalendar
reader rather than a second one, so what the reader pane shows and what the
calendar stores cannot disagree.

**Mail → calendar.** A message carrying a `text/calendar` (or
`application/ics`) part shows an invitation block above its body naming what
is proposed. ++i++ opens a dialog that settles which calendar it goes in and
what the organizer is told, because those are one decision for the user even
though they are two operations underneath: `chronos.ingest.ingest_ics_bytes`
files it — handling `METHOD:CANCEL` and a newer `SEQUENCE` as the calendar
already does — and then a `METHOD:REPLY` goes back to the organizer over
SMTP. Both halves are `pony.invitation_actions`, so the browser offers the
same three answers as a block in its reader. Filing is reported on its own:
an accepted invitation that reached the calendar but whose reply bounced is a
very different situation from one that never landed.

**Calendar → mail.** Saving an event that has attendees posts it to them as a
`METHOD:REQUEST` through the same SMTP path the composer uses. The event is
stored before the invitation is sent, so a failure to post is reported as
exactly that and never as a lost event. The organizer is dropped from the
recipient list — they are the one sending. The calendar writes its events
without a `METHOD`, since they are entries rather than scheduling messages;
posting one is what makes it a scheduling message, so the property is added
on the way out rather than stored.

**Attendees.** The event editor completes addresses from Pony's contact
index — the same source the composer's address fields use, which is the
point of the two halves sharing a process: an invitation goes to the people
already in the user's mail.

### Configuration and state

One file. Mail keys stay at the top level and the calendar's live under
`[calendar]`, with `[[calendar.accounts]]` holding its CalDAV accounts.
`use_utf8`, `editor` and `theme` are read from the top level unless
`[calendar]` overrides them; there is exactly one `config_version`, at the
top. `pony.calendar` is the bridge, and it stays clear of `chronos.tui` so
that importing it does not drag Textual into the CLI and MCP import graphs.

The calendar's mirror, index and OAuth tokens stay at the paths the calendar
already used, so an existing install keeps its synced data with nothing to
migrate. Only the configuration moved.

`pony calendar ...` runs any calendar command against that configuration.
The argument list is split before argparse sees it, because
`argparse.REMAINDER` still parses a leading flag itself — which would make
`pony calendar --help` print a wrapper's help and reject
`pony calendar --config f list` outright, when both belong to the calendar's
own parser.

### Known limitation

The MCP server a running TUI exposes is still the mail one. The calendar's
MCP tools are reachable through `pony calendar mcp`, not through the single
session endpoint.

## Data flow

```
config.toml
    |
    v
  AppConfig --> sync --> MirrorRepository --> IndexRepository
                  |                               |
                  v                               v
            IMAP server                    SQLite index
                                               |
                                               v
                                    front-end queries
                                   (lists, search) — the
                                   TUI or the browser UI
                                               |
                                               v
                                     MirrorRepository
                                     (raw message bytes,
                                      attachments)
                                               |
                                               v
                                      compose / send
                                         (SMTP)
```

1. The app layer loads configuration and resolves account/mirror state paths.
2. Sync populates or updates mirror storage.
3. Indexing projects searchable metadata into SQLite.
4. A front end — the TUI or the browser UI — queries the index for
   lists/search results and uses storage for raw message content and
   attachments. Neither queries differently: both go through the
   projections in `pony.views` / `chronos.views`.
5. Compose/send workflows write drafts and pending actions through shared
   service interfaces.

## Cross-cutting rules

- Keep interfaces strictly typed (mypy strict, basedpyright strict).
- Avoid hidden globals; pass dependencies explicitly.
- Prefer protocols and dataclasses for testability and clarity.
- Keep third-party dependencies minimal and explicit.
- Design for cross-platform path handling from the start (`pathlib.Path`
  throughout, `_sanitize_for_path` for unsafe characters).

## Dependencies

| Dependency | Purpose |
|---|---|
| `imapclient` | IMAP protocol |
| `textual` | Terminal UI framework |
| `markdown-it-py` | CommonMark rendering for compose |
| `tinymcp` | MCP server primitives (stdio JSON-RPC + TCP bridge) |

Dev tools: `ruff` (lint/format), `mypy` + `basedpyright` (type checking),
`pytest` (tests), `mkdocs-material` (documentation).
