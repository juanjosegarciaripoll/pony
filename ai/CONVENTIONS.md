# Engineering Conventions

## Stack

Python 3.13, `uv`, `hatchling`.

## Quality gates

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run basedpyright src/
uv run python -m pytest tests/
```

## Typing

- `mypy` + `basedpyright` strict.
- `Protocol` over ABCs; frozen `@dataclass` for domain objects.
- Textual `Screen.app` triggers `reportUnknownMemberType` — suppress with `# pyright: ignore[reportUnknownMemberType]` on public calls (`push_screen`, `notify`) only; never on private methods.

## Testing

- `unittest` (stdlib), run via `pytest`. Files: `tests/test_*.py`.
- Sync: `FakeImapSession`. Storage: conformance suite (Maildir + mbox). Contacts: real `SqliteIndexRepository`.
- Fixture messages: `tests/corpus.py` (15 RFC 5322 types). All addresses use `@example.com`.

## Style

- `ruff` rules: E, F, I, B, UP, N, ARG, SIM. Line length 88.
- No emojis, no docstrings on obvious methods, no error handling for impossible cases.

## Dependencies

Runtime (approved): `imapclient`, `textual`, `markdown-it-py`, `mcp`. New deps need approval.
Dev (approved): `ruff`, `mypy`, `basedpyright`, `pytest`, `pyinstrument`, `mkdocs-material`, `pytest-asyncio`.

## Config

Single `config.toml` → domain objects directly. `config-sample.toml` must mirror the model. Paths expand `~`, `$VAR`, `%VAR%`.

## Versions

`pyproject.toml` + `version.py` updated atomically by release Action. To release: add an undated `## [X.Y.Z]` heading to `CHANGELOG.md`, trigger the workflow — it stamps the date, tags, and publishes. Guard: tag must not already exist.

## Build

`docs/ → site/ → pony.spec → dist/pony/ → installers + archives`

```bash
uv sync --group build --group docs
uv run mkdocs build --strict
uv run python scripts/build.py
uv run python scripts/build.py --installer
uv run python scripts/build.py --skip-tests --skip-docs --installer
```

- `site/` is gitignored; never commit it.
- `pony.spec` `datas` list controls bundled files.
- `paths.bundled_docs_path()` detects PyInstaller execution.
- Installers: Inno Setup (Win), `hdiutil` (macOS), `appimagetool` (Linux).

## TUI

- Each screen owns `BINDINGS`; footer shows only its own.
- `push_screen()` / `notify()` only — no private App methods.
- Sync workflow in `MainScreen`, not `PonyApp`.
- `SyncConfirmScreen` takes `on_confirm` callback, not app ref.
