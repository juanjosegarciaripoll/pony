---
title: Architecture
---

# Architecture

## Overview

Pony Express is organized as a layered application that separates user-facing
workflows from protocol, storage, and indexing concerns. Multiple interfaces
can share the same core: the TUI, standalone composer, and contacts browser
are all separate Textual `App` subclasses that push their own screens and
own their own keybindings.

## Package layout

```
src/pony/
  __init__.py
  __main__.py          # python -m pony entrypoint
  version.py           # __version__ (stamped by the release workflow)
  cli.py               # argparse command dispatch
  config.py            # TOML config loader and validator
  domain.py            # typed core data models
  protocols.py         # repository and service interfaces
  paths.py             # application directory resolution
  accounts.py          # account lookup and mirror construction
  credentials.py       # plaintext / env / command / encrypted backends
  storage.py           # Maildir and mbox mirror repositories
  index_store.py       # SQLite metadata index repository
  storage_indexing.py  # mirror-to-index projection (rescan_local_account)
  message_projection.py# RFC 5322 parsing and metadata projection
  message_copy.py      # byte-faithful RFC 5322 duplication for copy actions
  mailbox_ops.py       # index-row rewrites for local moves, copies and flags
  message_renderer.py  # RFC 5322 -> plain text / browser HTML / attachments
  compose_utils.py     # quoting, address lists, MIME assembly
  composer.py          # what a reply/forward/new message starts as
  search_parser.py     # query language parser
  contact_naming.py    # display name -> contact first/last name
  folder_utils.py      # sent/drafts folder auto-discovery
  html_sanitize.py     # shared HTML→text helpers (preview + renderer)
  sync.py              # IMAP sync engine (plan/execute) + plan formatters
  imap_client.py       # ImapSession wrapper around imaplib
  smtp_sender.py       # SMTP submission
  bbdb.py              # BBDB v3 reader/writer
  services.py          # doctor diagnostics, mirror integrity
  fixture_flow.py      # deterministic fixture ingest flow
  mcp_server.py        # MCP server (stdio + TCP bridge via tinymcp)
  tui/
    app.py             # PonyApp, ComposeApp, ContactsApp, EmlViewerApp
    bindings.py        # shared mark/motion Binding tuples
    pdf_export.py      # HTML -> PDF via a detected external converter
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
      save_message_screen.py     # save body + attachments item picker
      save_folder_picker_screen.py # directory picker for saving files
      add_attachment_screen.py   # file picker
      attachment_picker_screen.py# pick previously-attached files by number
      eml_viewer_screen.py       # standalone .eml viewer
      goto_folder_screen.py      # G — fuzzy jump to folder
      new_folder_screen.py       # N — create new folder
      pick_folder_screen.py      # modal (account, folder) target picker
      help_screen.py             # F1 — keybinding cheatsheet
    widgets/
      folder_panel.py        # collapsible folder tree
      message_list.py        # async-streamed message table
      message_view.py        # scrollable message reader
      contact_suggester.py   # autocomplete dropdown
      edge_drag.py           # mouse-draggable pane borders
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
domain objects with no intermediate model layer. `AppPaths` resolves
platform-specific directories (XDG on Linux/macOS, `APPDATA`/`LOCALAPPDATA`
on Windows) with environment variable overrides.

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
provides batched transactions with thread-local reuse and reentrant nesting.

Tables: `messages`, `contacts`, `contact_emails`, `contact_aliases`,
`folder_sync_state`, `encrypted_passwords`.

### Sync (`pony.sync`, `pony.imap_client`)

Two-pass IMAP sync engine: plan (read-only comparison) then execute (apply
changes). Three-way flag merge with union policy. Mass-deletion protection
triggers at 20% of a folder's UIDs. Progress callbacks (`ProgressInfo`)
report per-folder scanning and per-operation execution.

There is no pending-operations queue. A local mutation is recorded by
rewriting the message's index row — `uid IS NULL` is what marks it as needing
a push — and the planner is the sole observer, emitting `PushMoveOp` or
`PushAppendOp`. New UIDs are captured from `APPENDUID` / `COPYUID`. Folder
creation works the same way: the mirror exposes a new directory and the
execute phase issues `IMAP CREATE`. The row rewrites themselves live in
`pony.mailbox_ops`.

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

Alongside the modal `g` sync flow there is a non-blocking background sync
(`ctrl+g`, the `sync-bg` worker) that auto-confirms every folder and shows a
spinner on the `FolderPanel` border title; it can also run on a config-gated
periodic timer (`background_sync_enabled` / `background_sync_interval_seconds`).
`MessageListPanel.load_folder` runs the SQL fetch in a Textual worker and
streams rows back to the UI thread in batches, so opening a 10k-row folder
never freezes the event loop.

`tui/terminal.py` updates the host terminal title via OSC 2 and restores it on
exit. `tui/bindings.py` holds the mark/motion `Binding` tuples shared by the
message list and the contact browser. `tui/ui_state.py` persists pane sizes to
`ui_state.json`. Theme selection comes from `theme` in `config.toml`, the
`--theme NAME` flag, or `--list-themes`.

### MCP (`pony.mcp_server`)

Read-only server built on `tinymcp` rather than the MCP SDK's HTTP stack. When
the TUI is running it serves on `127.0.0.1` over TCP behind a per-session token
held in a state file; `pony mcp` finds that file and proxies stdio↔TCP, so the
consumer never opens a competing SQLite handle. With no TUI running, `pony mcp`
opens its own connections and serves stdio directly.

### Message rendering (`pony.message_renderer`, `pony.tui.pdf_export`)

`render_message()` produces plain text, stripping `<style>` and `<script>`
first. `build_browser_html()` produces self-contained HTML with CID-resolved
inline images for the `w` key. `pdf_export.py` feeds that same HTML to whichever
external converter is present (Chromium/Chrome, `wkhtmltopdf`, WeasyPrint or
LibreOffice) for `ctrl+p`, running the blocking conversion in a Textual thread
worker.

One predicate decides what counts as an attachment, so the reader pane, the
browser view, the PDF export and the CLI extractor cannot disagree about a
message's contents.

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
                                        TUI queries
                                        (lists, search)
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
4. The TUI queries the index for lists/search results and uses storage for raw
   message content and attachments.
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
