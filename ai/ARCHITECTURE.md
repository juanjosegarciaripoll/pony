# Architecture

## Package layout

```
src/pony/
  __init__.py
  __main__.py          # python -m pony entrypoint
  version.py           # __version__ (release workflow)
  cli.py               # argparse command dispatch
  config.py            # TOML loader → domain objects
  domain.py            # frozen dataclasses (core types)
  protocols.py         # repository/service interfaces
  paths.py             # platform-specific dir resolution
  storage.py           # Maildir + mbox mirror repos
  index_store.py       # SQLite index + contacts repo
  storage_indexing.py  # mirror → index projection (rescan_local_account)
  message_projection.py# RFC 5322 parse → metadata
  message_copy.py      # byte-faithful RFC 5322 duplication for copy actions
  html_sanitize.py     # shared HTML→text helpers (preview + renderer)
  sync.py              # IMAP sync (plan/execute) + plan formatters
  imap_client.py       # ImapSession wrapper (imaplib)
  smtp_sender.py       # SMTP (SSL + STARTTLS)
  bbdb.py              # BBDB v3 reader/writer
  credentials.py       # four credential backends
  services.py          # doctor + mirror integrity
  folder_utils.py      # sent/draft auto-discovery
  fixture_flow.py      # deterministic dev fixtures
  mcp_server.py        # MCP server (stdio + TCP bridge via tinymcp)
  tui/
    app.py             # PonyApp, ComposeApp, ContactsApp
    bindings.py        # shared mark/motion Binding tuples
    compose_utils.py   # reply/forward quoting + signature
    message_renderer.py# RFC 5322 → plain text / browser HTML
    search_parser.py   # query parser (from:, to:, subject:…)
    terminal.py        # OSC sequences for window-title push/pop/set
    screens/
      main_screen.py             # three-pane reader (all mail bindings)
      compose_screen.py          # composer (ctrl+x chord)
      sync_confirm_screen.py     # plan confirmation + progress
      search_dialog_screen.py    # search input dialog
      contact_browser_screen.py  # contacts list/mark/merge/delete/create
      contact_detail_screen.py   # read-only contact view
      contact_edit_screen.py     # contact editor form
      confirm_screen.py          # yes/no dialog
      dialog_screen.py           # base class for modal yes/no dialogs
      link_action_screen.py      # Open / Copy / Cancel dialog for body links
      floating_input_screen.py   # base class for bottom floating-input bars
      save_draft_screen.py       # draft save confirmation
      add_attachment_screen.py   # file picker (DirectoryTree + typeahead)
      attachment_picker_screen.py# pick previously-attached files by number
      goto_folder_screen.py      # G — fuzzy jump to folder
      new_folder_screen.py       # N — create new folder
      pick_folder_screen.py      # modal (account, folder) target picker
      help_screen.py             # F1 — keybinding cheatsheet
    widgets/
      folder_panel.py        # collapsible per-account folder tree
      message_list.py        # async-streamed message DataTable
      message_view.py        # scrollable message reader
      contact_suggester.py   # address autocomplete
```

## Subsystems

**Domain** (`domain.py`, `protocols.py`): frozen dataclasses + Protocol interfaces; no I/O. Key types: `AppConfig`, `AccountConfig`, `IndexedMessage`, `MessageFlag`, `MessageStatus`, `FolderRef`, `MessageRef`, `Contact`, `SearchQuery`.

**Config** (`config.py`, `paths.py`): TOML → domain objects directly. `AppPaths` handles XDG/APPDATA. Paths expand `~`, `$VAR`, `%VAR%`.

**Storage** (`storage.py`): `MaildirMirrorRepository` + `MboxMirrorRepository` implement `MirrorRepository` — store/retrieve/list/delete raw RFC 5322 bytes, linked to index via `storage_key`.

**Index** (`index_store.py`): `SqliteIndexRepository` implements `IndexRepository` + `ContactRepository`. Single `messages` table. `connection()` provides batched transactions with thread-local reuse and reentrant nesting. Tables: `messages`, `contacts`, `contact_emails`, `contact_aliases`, `folder_sync_state`, `encrypted_passwords`.

**Sync** (`sync.py`, `imap_client.py`): two-pass plan/execute. Three-way flag merge (union policy). Mass-delete protection at 20%. Progress via `ProgressInfo`. TUI mutations set `uid IS NULL`; planner emits `PushMoveOp` or `PushAppendOp`; new UIDs captured via APPENDUID/COPYUID. Folder creation: mirror exposes new dir → `IMAP CREATE` at execute start. No pending-operations queue. Full algorithm: `ai/SYNCHRONIZATION.md`.

**Send** (`smtp_sender.py`, `compose_utils.py`): SSL + STARTTLS. Reply/forward preserves quote levels. Markdown → `multipart/alternative`. Sent/draft folders discovered by fuzzy name match.

**TUI** (`tui/`): three `App` subclasses. Each screen owns `BINDINGS`; the footer shows only that screen's bindings. `MainScreen` owns all mail bindings + sync workflow. Screens use public Textual API (`push_screen`, `notify`) only. `SyncConfirmScreen` takes `on_confirm` callback. `MessageListPanel.load_folder` runs the SQL fetch in a Textual worker and streams rows back to the UI thread in batches so opening a folder never freezes the event loop. Theme is selectable via `theme=` in `config.toml`, the `--theme NAME` CLI flag, or `--list-themes`. `tui/terminal.py` updates the host terminal title via OSC 2; `tui/bindings.py` exposes the shared mark/motion `Binding` tuples reused by the message list and contact browser.

**MCP** (`mcp_server.py`): read-only server built on `tinymcp` (no MCP-SDK HTTP stack). When the TUI is running it serves on `127.0.0.1` over TCP behind a per-session token in a state file; `pony mcp` checks for the file and proxies stdio↔TCP, so the SDK consumer never opens a competing SQLite handle. Without a running TUI, `pony mcp` opens its own connections and serves stdio directly.

**HTML rendering** (`message_renderer.py`): `render_message()` → plain text (strips `<style>`/`<script>` first). `build_browser_html()` → self-contained HTML with CID-resolved inline images (for `w` key).

## Data flow

```
config.toml → AppConfig → sync → MirrorRepository
                  |                      |
                  v                      v
            IMAP server           SQLite index
                                       |
                               TUI queries (list/search)
                                       |
                               MirrorRepository (raw bytes)
                                       |
                               compose / SMTP send
```

## Runtime deps

| Package | Purpose |
|---|---|
| `imapclient` | IMAP |
| `textual` | TUI framework |
| `markdown-it-py` | Markdown → HTML for compose |
| `tinymcp` | MCP server primitives (replaces the official `mcp` SDK) |

Dev: `ruff`, `mypy`, `basedpyright`, `pytest`, `mkdocs-material`.
