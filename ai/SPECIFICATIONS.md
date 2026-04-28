# Specifications

Standalone open-source Python 3.13 MUA. Clear mail workflow, flexible local storage, strong offline behavior.

## Capabilities

1. Mail synchronization (IMAP)
2. Mail reading (TUI)
3. Mail composing (SMTP + attachments + Markdown)
4. Mail search (FTS5)

## V1 (delivered)

IMAP sync, SMTP send, Maildir/mbox mirrors, SQLite+FTS5 index, Textual TUI, Markdown compose, BBDB contacts, MCP server, PyInstaller builds. Details in `ai/STATUS.md` and `CHANGELOG.md`.

## Deferred

- POP support
- OAuth
- Browser UI
- Multi-machine conflict handling
- Background/periodic sync
- Aggressive auto remote mutations

## Engineering

`ai/CONVENTIONS.md` — rules, quality gates, build. `ai/ARCHITECTURE.md` — design.
