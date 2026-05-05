# Pony Express — Agent Instructions

Terminal-first Python 3.13 MUA: IMAP sync → Maildir/mbox mirror → SQLite index → Textual TUI; SMTP out, optional Markdown compose.

## Docs

| File | Purpose |
|---|---|
| `ai/SPECIFICATIONS.md` | Goals, deferred scope |
| `ai/ARCHITECTURE.md` | Package layout, subsystems, data flow |
| `ai/SYNCHRONIZATION.md` | Sync algorithm, schema, conflicts |
| `ai/CONVENTIONS.md` | Quality gates, style, build |
| `ai/STATUS.md` | Delivered + queued work |
| `config-sample.toml` | Config reference |
| `CHANGELOG.md` | Release history |

## Rules

1. **Read first.** Use `ai/ARCHITECTURE.md` to locate the right module.
2. **Quality gates after every change:** `ruff check`, `ruff format --check`, `mypy`, `basedpyright`, `pytest`. Coverage gate is 85 % branch; use `--no-cov` only while below baseline (~56 %). New code must not lower coverage.
3. **No speculative complexity.** No feature flags, compat shims, unused abstractions.
4. **Runtime deps:** `imapclient`, `textual`, `markdown-it-py` — new ones need approval.
5. **Keep docs in sync:** `config-sample.toml` ↔ config model; `ai/ARCHITECTURE.md` ↔ subsystem layout.
6. **Never touch version strings.** Release workflow stamps `pyproject.toml` + `version.py` from `CHANGELOG.md`.
7. **Tests:** `unittest` run via `pytest`. Sync: `FakeImapSession`. Storage: shared conformance suite.

## Local mutations

TUI actions that round-trip to the server (archive, compose, folder create) set `uid IS NULL` on the index row. Sync planner is the sole observer — emitting `PushMoveOp` or `PushAppendOp`. No parallel queues or status flags. See `ai/SYNCHRONIZATION.md`.

## Build

```bash
uv sync --group build --group docs
uv run python scripts/build.py             # portable archive
uv run python scripts/build.py --installer # + platform installer
```

Artifacts → `artifacts/`. `pony.spec` controls bundling. `paths.bundled_docs_path()` detects frozen execution.

## Do NOT

- Mock the database — use real SQLite via `SqliteIndexRepository`.
- Add `# type: ignore` without a diagnostic code.
- Call `self.app._private_method()` from screens.
- Add a pending-mutations table — set `uid=NULL` on the index row.
- Commit `site/` — generated at build time.
- Add write/mutating MCP tools without approval.
- Create docs files unless asked.
- Add emojis unless asked.
