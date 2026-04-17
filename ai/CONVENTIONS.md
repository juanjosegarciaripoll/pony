# Engineering Conventions

## Language and runtime

- Python 3.13
- `uv` for dependency and environment management
- `hatchling` build backend

## Quality gates (all must pass)

```bash
uv run ruff check src/ tests/        # lint
uv run ruff format --check src/ tests/ # formatting
uv run mypy src/                      # type check (strict)
uv run basedpyright src/              # type check (strict)
uv run python -m pytest tests/        # tests
```

## Typing

- `mypy` strict mode, `basedpyright` strict mode
- Prefer `Protocol` classes over abstract base classes
- Use frozen `@dataclass` for domain objects
- Textual's generic `Screen.app` property causes `reportUnknownMemberType`
  warnings -- suppress with `# pyright: ignore[reportUnknownMemberType]`
  on public API calls (`push_screen`, `notify`). Never suppress on private
  method access.

## Testing

- Framework: `unittest` (stdlib), run via `pytest`
- Test files: `tests/test_*.py`
- Sync tests: `FakeImapSession` in `tests/test_sync.py`
- Storage tests: shared conformance suite across Maildir and mbox backends
- Contacts tests: real SQLite via `SqliteIndexRepository`
- Fixture messages: `tests/corpus.py` (15 programmatic RFC 5322 types)
- All test email addresses use `@example.com`

## Code style

- `ruff` with rules: E, F, I, B, UP, N, ARG, SIM
- Line length: 88
- Imports sorted by `ruff` (isort-compatible)
- No emojis in code or docs unless explicitly requested
- Prefer editing existing files over creating new ones
- No docstrings on obvious methods; comments only where logic isn't self-evident
- No error handling for scenarios that can't happen

## Dependencies

Approved runtime:

| Package | Purpose |
|---|---|
| `imapclient` | IMAP protocol |
| `textual` | Terminal UI |
| `markdown-it-py` | Markdown rendering for compose |

Approved dev: `ruff`, `mypy`, `basedpyright`, `pytest`, `pyinstrument`,
`mkdocs-material`.

New runtime dependencies require explicit approval.

## Configuration

- Single TOML file (`config.toml`)
- Parsed directly into domain objects (no intermediate model layer)
- `config-sample.toml` must stay synchronized with the config model
- Path values support `~`, `$VAR`, `%VAR%` expansion via `_expand_path`
- All test/sample configs use `@example.com` addresses and `example.com` hosts

## Version management

- Version string in `pyproject.toml` and `src/pony/version.py`
- Both updated atomically by the release GitHub Action
- `pony --version` reads from `version.py` (works in PyInstaller bundles)
- CHANGELOG.md follows Keep a Changelog format
- Release workflow: manually dispatched. CHANGELOG.md is the source of truth
  for the release version — write a new undated `## [X.Y.Z]` heading, then
  trigger the workflow. It reads X.Y.Z from the changelog, overwrites
  `pyproject.toml` and `version.py` with that value, stamps the date, tags,
  and creates the GitHub release. The only guard is that the tag `vX.Y.Z`
  must not already exist.

## TUI conventions

- Each screen owns its own `BINDINGS` list
- Footer shows only the current screen's bindings
- Screens use `self.app.push_screen()` and `self.app.notify()` (public API)
- Screens never call `self.app._private_method()`
- Sync workflow lives in `MainScreen`, not `PonyApp`
- `SyncConfirmScreen` receives `on_confirm` callback, not app reference
