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

Terminal-first, cross-platform. The delivered feature set — IMAP sync,
SMTP send, Maildir/mbox mirrors, SQLite+FTS5 index, Textual TUI,
Markdown composition, BBDB-compatible contacts, MCP server, and
PyInstaller-based standalone builds — is summarised in
`ai/STATUS.md` and detailed release-by-release in `CHANGELOG.md`.

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

See `ai/CONVENTIONS.md` for language, tooling, typing, style, testing,
dependency, and release rules.
