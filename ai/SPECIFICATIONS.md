# Product Specifications

## Mission

Pony Express is a standalone open source mail user agent written in Python
3.13. It provides a clear, dependable mail workflow with flexible local
storage, strong offline behavior, and interfaces that can evolve over time
without rewriting the core application logic.

## Product direction

Four major capabilities, sharing core services:

1. Mail synchronization
2. Mail reading
3. Mail composing (with attachments)
4. Mail search

## V1 scope (delivered)

Terminal-first, cross-platform. Includes:

- IMAP synchronization with two-pass plan/execute
- SMTP sending with SSL and STARTTLS
- Password and app-password authentication (four credential backends)
- Local mirrors per account in Maildir or mbox format
- SQLite-backed index for search and control-plane state
- Three-pane TUI (Textual) for reading, searching, replying, forwarding,
  deleting, and composing
- Markdown composition (multipart/alternative)
- Person-centric contacts with BBDB import/export
- Sync progress reporting
- Mirror integrity diagnostics
- Cross-platform path handling
- Per-account archive folder (`A` key) — local move reconciled via UID MOVE
  on next sync

## Deferred scope

- POP support
- OAuth authentication flows
- Browser-based reader/composer UI
- Advanced multi-machine conflict handling
- Aggressive automatic remote mutation policies
- Background / periodic sync

## User experience goals

Keyboard-centric pane-oriented workflow:

- Folder view grouped by account
- Message list view (sortable, searchable)
- Message body view with attachment awareness
- Screen-specific keybindings (each screen shows only its own bindings)

## Storage and indexing

- Per-account configurable mirror format (Maildir or mbox)
- Raw message content stays in mirror storage
- SQLite is the searchable metadata layer and control plane
- Search supports: sender, recipients, subject, body, combined queries,
  case-sensitive/insensitive matching
- Unified `messages` table (no separate server-state table)
- Batched transactions via `connection()` context manager

## Engineering rules

- Python 3.13, strict typing (`mypy` strict + `basedpyright` strict)
- Runtime dependencies minimal and explicitly approved
- `uv`-managed project with TOML configuration
- Tests as first-class (`unittest` framework, run via `pytest`)
- `ruff` for linting and formatting
- Keep `config-sample.toml` synchronized with the config model
- Version in `pyproject.toml` and `src/pony/version.py` (release workflow
  updates both)
