# Agent Instructions for Pony Express

You are working on **Pony Express**, a terminal-first mail user agent written
in Python 3.13. Read this file first, then the sibling documents in `ai/`
before making changes.

## Project overview

Pony Express synchronises mail over IMAP, stores it locally in Maildir or mbox
format, indexes it in SQLite for fast search, and presents it through a
keyboard-driven terminal interface built with Textual. Outgoing mail is sent
over SMTP with optional Markdown rendering.

## Reading order

| File | Purpose |
|---|---|
| `ai/SPECIFICATIONS.md` | Product goals, v1 scope, deferred scope |
| `ai/ARCHITECTURE.md` | Package layout, subsystem boundaries, data flow |
| `ai/SYNCHRONIZATION.md` | Sync algorithm, conflict taxonomy, schema, known limitations |
| `ai/CONVENTIONS.md` | Engineering rules, coding style, quality gates |
| `ai/STATUS.md` | What's delivered, what's queued |
| `config-sample.toml` | Configuration reference (keep in sync with code) |
| `CHANGELOG.md` | Release history |

## How to work

1. **Read before writing.** Use `ai/ARCHITECTURE.md` to locate the right
   module; don't grep blindly.

2. **Run the quality gates** (defined in `ai/CONVENTIONS.md`) after every
   change — ruff, mypy strict, basedpyright strict, pytest.

3. **No speculative complexity.** No feature flags, no backward-compat
   shims, no abstractions without a caller. Delete what is unused.

4. **Keep dependencies minimal.** Approved runtime deps: `imapclient`,
   `textual`, `markdown-it-py`, `mcp`. New dependencies require approval.

5. **Maintain the docs.** Update `config-sample.toml` when the config
   model changes, `docs/` when behaviour changes, and `ai/ARCHITECTURE.md`
   when subsystem boundaries move.

6. **Don't touch the version string.** `pyproject.toml` and
   `src/pony/version.py` are updated atomically by the release workflow
   from a new heading in `CHANGELOG.md`.

7. **Test strategy.** `unittest` (not pytest fixtures), run via `pytest`.
   Sync tests use `FakeImapSession`. Storage tests run the same
   conformance suite against both Maildir and mbox backends.

## Local mutations and sync

Any change the user makes in the TUI that should round-trip to the server
(archive, local compose, folder create) is expressed by leaving
``uid IS NULL`` on the relevant index row. The sync planner is the single
observer of this signal — via `PushMoveOp`, `PushAppendOp`, or
`LinkLocalOp`. Never invent parallel queues or status flags for this; see
`ai/SYNCHRONIZATION.md` for the full model.

## Building the standalone executable

```bash
uv sync --group build --group docs
uv run python scripts/build.py             # portable archive
uv run python scripts/build.py --installer # + platform installer
```

Artifacts land in `artifacts/` with an `artifacts.json` manifest.
`pony.spec` controls bundling; `site/` (MkDocs output) and
`config-sample.toml` are bundled as data. `paths.bundled_docs_path()`
is the single place that distinguishes frozen from source execution.

## What NOT to do

- Don't mock the database in tests — use real SQLite via `SqliteIndexRepository`.
- Don't add `# type: ignore` without a specific diagnostic code.
- Don't call `self.app._private_method()` from screens.
- Don't add a separate "pending mutations" table for user actions — set
  `uid=NULL` on the index row instead.
- Don't commit the `site/` build artifact; it is generated at build time.
- Don't add write/mutating tools to the MCP server without explicit
  approval — read-only is the intended scope.
- Don't create documentation files unless asked.
- Don't add emojis to code or docs unless asked.
