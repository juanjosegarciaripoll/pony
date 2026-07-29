# Engineering Conventions

## Stack

Python 3.13, `uv`, `hatchling`.

## Quality gates

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run basedpyright src/
uv run pytest
```

`pytest` runs with `--cov=pony --cov-branch --cov-fail-under=85`; new code must keep combined statement+branch coverage at or above **85 %**. The gate is live — run `uv run python -m pytest` and never pass `--no-cov`. New code ships with tests; do not lower the threshold.

## Typing

- `mypy` + `basedpyright` strict.
- `Protocol` over ABCs; frozen `@dataclass` for domain objects.
- Textual `Screen.app` triggers `reportUnknownMemberType` — suppress with `# pyright: ignore[reportUnknownMemberType]` on public calls (`push_screen`, `notify`) only; never on private methods.

## Testing

- `unittest` (stdlib), run via `pytest`. Files: `tests/test_*.py`.
- **Live IMAP tests** in `tests/test_imap_live.py` skip unless `PONY_LIVE_IMAP=1`. They drive a real
  Dovecot via `scripts/dovecot_userspace.sh`, which installs it under `~/.cache` with no root and no
  system packages. They cover what a fake session cannot decide honestly: UID assignment, a genuine
  UIDVALIDITY change, and what APPEND returns.

  Turn them on for a checkout by writing `PONY_LIVE_IMAP=1` into a `.env` at the repo root —
  `tests/conftest.py` loads it, a real environment variable still wins, and `.env` is gitignored.
  The server always runs as the invoking user, on its own port, with the harness's own config and
  Maildir root — a packaged install supplies only the binaries, and its own service and
  `/etc/dovecot` are never used. When a working system Dovecot is present the harness uses it and
  skips the sandbox; otherwise it unpacks one under `~/.cache`. Set `PONY_DOVECOT_FORCE_UNPACK=1`
  to exercise the unpacked path regardless.

  The unpacked install is cached: the first run downloads roughly 3 MB, later runs cost
  milliseconds. `scripts/dovecot_userspace.sh install --force` rebuilds it. The eight
  scenarios take about 40 s, most of it restarting the server to change a UID epoch.
- Sync: `FakeImapSession`. Storage: conformance suite (Maildir + mbox). Contacts: real `SqliteIndexRepository`.
- Fixture messages: `tests/corpus.py` (15 RFC 5322 types). All addresses use `@example.com`.

## Style

- `ruff` rules: E, F, I, B, UP, N, ARG, SIM. Line length 88.
- No emojis, no docstrings on obvious methods, no error handling for impossible cases.

## Dependencies

Runtime (approved): `imapclient`, `textual`, `markdown-it-py`, `tinymcp`. New deps need approval.
Dev (approved): `ruff`, `mypy`, `basedpyright`, `pytest`, `pytest-asyncio`, `pytest-cov`, `coverage`, `pyinstrument`, `mkdocs-material`.

## Config

Single `config.toml` → domain objects directly. `config-sample.toml` must mirror the model. Paths expand `~`, `$VAR`, `%VAR%`.

## Versions

`pyproject.toml` + `version.py` updated atomically by the `release.yml` Action (single dispatch-driven `prepare → build → publish` pipeline). To release: add an undated `## [X.Y.Z]` heading at the **very top** of `CHANGELOG.md`, then trigger the workflow — it reads **only the first heading**, stamps the date, builds + tests on all three platforms, then tags and publishes the release with binaries attached. Guards: the first heading must be an undated bare `## [X.Y.Z]`; the version must be **strictly greater** than the current one (no downgrades); the tag must not already exist.

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
