# Architecture

## Package layout

```
src/pony/
  __init__.py
  __main__.py          # python -m pony entrypoint
  version.py           # __version__ string (updated by release workflow)
  cli.py               # argparse command dispatch
  config.py            # TOML config loader and validator
  domain.py            # typed core data models (frozen dataclasses)
  protocols.py         # repository and service interfaces (Protocols)
  paths.py             # platform-specific directory resolution
  storage.py           # Maildir and mbox mirror repositories
  index_store.py       # SQLite metadata index + contacts repository
  storage_indexing.py  # mirror-to-index projection pipeline
  message_projection.py# RFC 5322 parsing and metadata extraction
  sync.py              # IMAP sync engine (plan/execute)
  imap_client.py       # ImapSession wrapper around imaplib
  smtp_sender.py       # SMTP submission (SSL + STARTTLS)
  bbdb.py              # BBDB v3 reader/writer (Emacs interop)
  credentials.py       # four credential backends
  services.py          # doctor diagnostics, mirror integrity scan
  folder_utils.py      # sent/draft folder auto-discovery
  fixture_flow.py      # deterministic fixture ingest for development
  tui/
    app.py             # PonyApp, ComposeApp, ContactsApp
    compose_utils.py   # reply/forward quoting, signature handling
    message_renderer.py# RFC 5322 -> plain text / browser HTML
    search_parser.py   # query language parser (from:, to:, subject:, etc.)
    screens/
      main_screen.py          # three-pane mail reader (owns all mail bindings)
      compose_screen.py       # email composer (ctrl+x chord)
      sync_confirm_screen.py  # sync plan confirmation + progress bar
      search_dialog_screen.py # search query input dialog
      contact_browser_screen.py  # contacts list with mark/merge/delete
      contact_detail_screen.py   # read-only contact detail view
      contact_edit_screen.py     # contact editor form
      confirm_screen.py       # generic yes/no dialog
      save_draft_screen.py    # draft save confirmation
      add_attachment_screen.py # file picker (DirectoryTree + typeahead)
    widgets/
      folder_panel.py        # collapsible per-account folder tree
      message_list.py        # sortable message DataTable
      message_view.py        # scrollable message reader
      contact_suggester.py   # autocomplete dropdown for address fields
```

## Subsystems

### Domain layer (`domain.py`, `protocols.py`)

Frozen dataclasses for all domain objects. Protocol classes define contracts
for repositories and services. This layer has no protocol-specific or
UI-specific logic.

Key types: `AppConfig`, `AccountConfig`, `IndexedMessage`, `MessageFlag`,
`MessageStatus`, `FolderRef`, `MessageRef`, `Contact`, `SearchQuery`.

### Configuration (`config.py`, `paths.py`)

TOML parsing directly into domain objects (no intermediate model layer).
`AppPaths` resolves platform-specific directories (XDG on Linux/macOS,
APPDATA/LOCALAPPDATA on Windows). Path values support `~`, `$VAR`, `%VAR%`
expansion.

### Storage (`storage.py`)

`MaildirMirrorRepository` and `MboxMirrorRepository` implement the
`MirrorRepository` protocol. Both store/retrieve/list/delete raw RFC 5322
bytes. Connected to the index via `storage_key`.

### Index (`index_store.py`)

`SqliteIndexRepository` implements both `IndexRepository` and
`ContactRepository`. All message state lives in a single unified `messages`
table. The `connection()` context manager provides batched transactions with
thread-local connection reuse and reentrant nesting.

Tables: `messages`, `contacts`, `contact_emails`, `contact_aliases`,
`folder_sync_state`, `pending_operations`, `encrypted_passwords`.

### Sync (`sync.py`, `imap_client.py`)

Two-pass IMAP sync: plan (read-only) then execute (apply changes). Three-way
flag merge with union policy. Mass-deletion protection at 20% threshold.
Progress callbacks via `ProgressInfo` dataclass. See `ai/SYNCHRONIZATION.md`
for the full algorithm.

### Send (`smtp_sender.py`, `compose_utils.py`)

`SMTPSender` handles SSL and STARTTLS. Reply/forward quoting preserves
existing quote levels. Markdown mode builds `multipart/alternative` via
`markdown-it-py`. Sent/draft folders are auto-discovered by fuzzy name match.

### TUI (`tui/`)

Three separate `App` subclasses (see `ai/AGENTS.md` for details). Each screen
owns its own bindings. `MainScreen` owns all mail-specific bindings and the
sync workflow. Screens use `self.app.push_screen()` and `self.app.notify()`
(public Textual API) but never call private App methods.

### HTML rendering (`message_renderer.py`)

`render_message()` extracts plain text for TUI display. HTML-only messages
have `<style>` and `<script>` blocks stripped before tag removal.
`build_browser_html()` creates self-contained HTML files with CID-resolved
inline images for the `w` (web view) key.

## Data flow

```
config.toml -> AppConfig -> sync -> MirrorRepository -> IndexRepository
                              |                              |
                              v                              v
                        IMAP server                   SQLite index
                                                          |
                                                          v
                                                   TUI queries
                                                   (lists, search)
                                                          |
                                                          v
                                                MirrorRepository
                                                (raw bytes, attachments)
                                                          |
                                                          v
                                                 compose / send (SMTP)
```

## Dependencies

| Dependency | Purpose |
|---|---|
| `imapclient` | IMAP protocol |
| `textual` | Terminal UI framework |
| `markdown-it-py` | CommonMark rendering for compose |

Dev: `ruff`, `mypy`, `basedpyright`, `pytest`, `mkdocs-material`.
