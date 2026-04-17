---
title: Development
---

# Development

## Prerequisites

- Python **3.13** or later
- [uv](https://docs.astral.sh/uv/) for dependency management

## Setup

```bash
git clone https://github.com/juanjosegarciaripoll/pony.git
cd pony
uv sync
```

## Running

```bash
# Run the TUI
uv run pony tui

# Run any command
uv run pony doctor
uv run pony sync
uv run pony compose --to alice@example.com
```

## Quality gates

All three checks must pass before merging:

```bash
# Lint and format
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Type checking (both checkers must pass)
uv run mypy src/
uv run basedpyright src/

# Tests
uv run python -m pytest tests/
```

### Lint

[Ruff](https://docs.astral.sh/ruff/) handles both linting and formatting.
Selected rule sets: `E`, `F`, `I`, `B`, `UP`, `N`, `ARG`, `SIM`.

### Type checking

Both [mypy](https://mypy-lang.org/) (strict mode) and
[basedpyright](https://docs.basedpyright.com/) (strict mode) are required.
The Textual framework's generic types produce some `reportUnknownMemberType`
warnings in basedpyright that are suppressed with inline comments.

### Tests

Tests use Python's built-in `unittest` framework, organized in `tests/`.
Run them with pytest for better output:

```bash
uv run python -m pytest tests/ -v
```

The test suite includes:

- **Unit tests**: MIME parsing, flag mapping, reply/forward quoting, attachment
  extraction, search query compilation, BBDB roundtrip
- **Conformance tests**: both Maildir and mbox backends run the same CRUD/flag
  test suite
- **Sync tests**: deterministic fixtures via `FakeImapSession` cover new message
  ingestion, flag reconciliation, conflict resolution, UIDVALIDITY reset
- **Index tests**: SQLite schema, multi-field search, case sensitivity,
  checkpoints, pending operations, delete, batched transactions
- **Contacts tests**: upsert, search, harvest, delete, merge (two-way and
  three-way), BBDB import/export
- **Mirror integrity tests**: orphan file detection, stale index row detection
- **Renderer tests**: HTML stripping (style/script block removal), nested email
  rendering

## Building documentation

The documentation uses [MkDocs](https://www.mkdocs.org/) with the
[Material](https://squidfunk.github.io/mkdocs-material/) theme.

```bash
# Install docs dependencies
uv sync --group docs

# Serve locally with live reload
uv run mkdocs serve

# Build static site
uv run mkdocs build
```

The built site goes to `site/` (gitignored). GitHub Pages deployment is
automated via the `.github/workflows/docs.yml` workflow.

## Project structure

```
pony/
  src/pony/           # main package
  tests/              # test suite
  docs/               # documentation (MkDocs source)
  .github/workflows/  # CI/CD workflows
  mkdocs.yml          # MkDocs configuration
  pyproject.toml      # project metadata and tool config
  PLAN.md             # development roadmap
  ARCHITECTURE.md     # technical design
  SPECIFICATIONS.md   # product goals and scope
  MEMORY.md           # session restart context
  config-sample.toml  # annotated config template
  LICENSE             # MIT license
```

## GitHub Pages setup

To enable GitHub Pages for this repository:

1. Go to **Settings** > **Pages** in the GitHub repository.
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.
3. Push to the `main` branch. The `docs.yml` workflow will build and deploy
   the documentation automatically.
4. The site will be available at
   `https://<username>.github.io/pony/`.

No additional configuration is needed. The workflow uses the
`actions/deploy-pages` action with the `github-pages` environment.
