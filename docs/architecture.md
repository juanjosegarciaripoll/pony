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
  cli.py               # argparse command dispatch
  config.py            # TOML config loader and validator
  domain.py            # typed core data models
  protocols.py         # repository and service interfaces
  paths.py             # application directory resolution
  storage.py           # Maildir and mbox mirror repositories
  index_store.py       # SQLite metadata index repository
  storage_indexing.py  # mirror-to-index projection pipeline
  message_projection.py# RFC 5322 parsing and metadata projection
  sync.py              # IMAP sync engine (plan/execute)
  imap_client.py       # ImapSession wrapper around imaplib
  smtp_sender.py       # SMTP submission
  bbdb.py              # BBDB v3 reader/writer
  services.py          # doctor diagnostics, mirror integrity
  fixture_flow.py      # deterministic fixture ingest flow
  tui/
    app.py             # PonyApp, ComposeApp, ContactsApp
    compose_utils.py   # reply/forward quoting helpers
    message_renderer.py# RFC 5322 -> plain text / browser HTML
    search_parser.py   # query language parser
    screens/
      main_screen.py          # three-pane mail reader
      compose_screen.py       # email composer
      sync_confirm_screen.py  # sync plan confirmation
      search_dialog_screen.py # search query input
      contact_browser_screen.py  # contacts list
      contact_detail_screen.py   # contact detail view
      contact_edit_screen.py     # contact editor
      confirm_screen.py       # generic yes/no dialog
      save_draft_screen.py    # draft save confirmation
      add_attachment_screen.py # file picker
    widgets/
      folder_panel.py        # collapsible folder tree
      message_list.py        # sortable message table
      message_view.py        # scrollable message reader
      contact_suggester.py   # autocomplete dropdown
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

### Index (`pony.index_store`)

SQLite-backed metadata store implementing `IndexRepository` and
`ContactRepository`. All message state lives in a single unified `messages`
table (no separate server-state table). The `connection()` context manager
provides batched transactions with thread-local reuse and reentrant nesting.

Tables: `messages`, `contacts`, `contact_emails`, `contact_aliases`,
`folder_sync_state`.

### Sync (`pony.sync`, `pony.imap_client`)

Two-pass IMAP sync engine: plan (read-only comparison) then execute (apply
changes). Three-way flag merge with union policy. Mass-deletion protection.
Progress callbacks report per-folder scanning and per-operation execution.

### Send (`pony.smtp_sender`, `pony.tui.compose_utils`)

SMTP submission with SSL and STARTTLS. Reply/forward quoting preserves
existing quote levels. Markdown mode builds `multipart/alternative` messages
via `markdown-it-py`.

### TUI (`pony.tui`)

Three separate Textual `App` classes, each minimal:

- **`PonyApp`** (`pony tui`): pushes `MainScreen` on mount. Owns only the
  ++q++ (quit) binding. All mail-specific bindings (sync, compose, flags,
  attachments, search, contacts) live on `MainScreen`.
- **`ComposeApp`** (`pony compose`): pushes `ComposeScreen` on mount, exits
  on send or cancel.
- **`ContactsApp`** (`pony contacts browse`): pushes `ContactBrowserScreen`
  on mount, exits on dismiss.

Each screen owns its own bindings and shows only its relevant keybindings in
the footer. Screens communicate upward via Textual messages or callbacks
passed at construction time, not by calling private App methods.

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

Dev tools: `ruff` (lint/format), `mypy` + `basedpyright` (type checking),
`pytest` (tests), `mkdocs-material` (documentation).
