# Agent Instructions for Pony Express

You are working on **Pony Express**, a terminal-first mail user agent written
in Python 3.13. Before making changes, read this file and the other documents
in `ai/` to understand the project's architecture, constraints, and current
state.

## Project overview

Pony Express synchronises mail over IMAP, stores it locally in Maildir or mbox
format, indexes it in SQLite for fast search, and presents it through a
keyboard-driven terminal interface built with Textual. Outgoing mail is sent
over SMTP with optional Markdown rendering.

## Key files to read first

| File | Purpose |
|---|---|
| `ai/ARCHITECTURE.md` | Package layout, subsystem boundaries, data flow |
| `ai/SPECIFICATIONS.md` | Product goals, v1 scope, deferred scope |
| `ai/SYNCHRONIZATION.md` | Sync algorithm, conflict taxonomy, schema |
| `ai/SYNC_AUDIT.md` | Known issues, fixes, and deferrals in sync engine |
| `ai/CONVENTIONS.md` | Engineering rules, coding style, quality gates |
| `ai/STATUS.md` | Current version, what's done, what's next |
| `config-sample.toml` | Configuration reference (keep in sync with code) |
| `src/pony/mcp_server.py` | FastMCP server — 7 read-only mail tools (stdio + HTTP) |
| `pony.spec` | PyInstaller build spec (bundles `site/` + `config-sample.toml`) |
| `scripts/build.py` | Local standalone build script (tests + docs + binary + installers) |
| `installers/windows/pony.iss` | Inno Setup script for Windows installer |
| `installers/linux/pony.desktop` | Desktop entry for Linux AppImage |

## How to work

1. **Read before writing.** Understand existing code before suggesting changes.
   Use `ai/ARCHITECTURE.md` to locate the right module.

2. **Run quality gates.** After any code change:
   ```bash
   uv run ruff check src/ tests/
   uv run mypy src/
   uv run basedpyright src/
   uv run python -m pytest tests/
   ```

3. **Don't add unnecessary complexity.** No speculative abstractions, no
   feature flags, no backward-compatibility shims. If something is unused,
   delete it.

4. **Keep dependencies minimal.** Approved runtime deps: `imapclient`,
   `textual`, `markdown-it-py`. Any new dependency requires explicit approval.

5. **Maintain the docs.** When adding features, update `config-sample.toml`
   and the relevant `docs/` page. When changing architecture, update
   `ai/ARCHITECTURE.md`.

6. **Version management.** The version string lives in two places:
   `pyproject.toml` and `src/pony/version.py`. Both are updated by the
   release workflow. Don't change them manually.

7. **Test strategy.** Use `unittest` (not pytest fixtures). Tests live in
   `tests/`. Sync tests use `FakeImapSession`. Storage tests run the same
   conformance suite against both Maildir and mbox backends.

## TUI architecture (important)

The TUI uses three separate Textual `App` subclasses, each minimal:

- **`PonyApp`** (`pony tui`): pushes `MainScreen`, owns only `Q` (quit).
  All mail bindings live on `MainScreen`.
- **`ComposeApp`** (`pony compose`): pushes `ComposeScreen`, exits on
  send/cancel.
- **`ContactsApp`** (`pony contacts browse`): pushes `ContactBrowserScreen`,
  exits on dismiss.

Each screen owns its own keybindings. Screens communicate upward via Textual
messages or callbacks, **not** by calling private App methods. The
`SyncConfirmScreen` receives an `on_confirm` callback rather than reaching
into the App.

## Local mutations and sync

Any change the user makes in the TUI that should round-trip to the server
(archive, future: drag-to-folder, local compose) is expressed by leaving
``uid IS NULL`` on the relevant index row. The sync planner is the single
place that observes this and pushes the work — via ``PushMoveOp``,
``PushAppendOp``, or ``LinkLocalOp``. Don't invent parallel queues or
status flags for this.

## Building the standalone executable

The primary distribution artifact is a platform-native binary built with
PyInstaller. The `pony.spec` file controls what is bundled.

### Prerequisites

```bash
uv sync --group build --group docs
```

### Full local build

```bash
uv run python scripts/build.py             # portable archive only
uv run python scripts/build.py --installer # archive + platform installer
```

Options: `--skip-tests`, `--skip-docs`, `--version X.Y.Z`

### Artifacts produced

| Platform | Installer | Portable archive |
|---|---|---|
| Windows | `pony-windows-vX.Y.Z-setup.exe` | `pony-windows-vX.Y.Z.zip` |
| macOS | `pony-macos-vX.Y.Z.dmg` | `pony-macos-vX.Y.Z.tar.gz` |
| Linux | `pony-linux-vX.Y.Z.AppImage` | `pony-linux-vX.Y.Z.tar.gz` |

Artifacts land in `artifacts/`. An `artifacts.json` manifest lists all paths.

### Bundled resources

`pony.spec` adds two data trees to the binary:
- `site/` — pre-built HTML documentation (from `uv run mkdocs build --strict`)
- `config-sample.toml` — configuration reference

`src/pony/paths.py::bundled_docs_path()` returns the path to the `site/` tree
when running as a frozen binary; returns `None` when running from source.
The `pony docs` CLI command uses this to open docs offline or fall back to the
GitHub Pages URL.

### Spec file maintenance

When adding new data files that must ship in the binary, add them to the
`datas` list in `pony.spec`. Never commit the `site/` directory — it is
generated at build time and is already gitignored.

## What NOT to do

- Don't mock the database in tests -- use real SQLite via `SqliteIndexRepository`.
- Don't add `# type: ignore` without a specific diagnostic code.
- Don't use `self.app._private_method()` from screens.
- Don't create documentation files unless asked.
- Don't add emojis to code or docs unless asked.
- Don't add a separate "pending mutations" table for user actions that
  belong in the sync model — set ``uid=NULL`` on the index row instead.
- Don't commit the `site/` build artifact; it is generated at build time.
- Don't add write/mutating tools to the MCP server without explicit approval — read-only is the intended scope.
