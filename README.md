# Pony Express

Pony Express is a terminal-first mail user agent written in Python 3.13. It
synchronises mail over IMAP, stores it locally in Maildir or mbox format,
indexes it in SQLite for fast search, and presents it through a keyboard-driven
terminal interface built with [Textual](https://textual.textualize.io/).
Outgoing mail is sent over SMTP with optional Markdown rendering to
`multipart/alternative`.

## Warning

This is a vibe-coded project created with the assistance of various AI
agents. There is no guarantee about this code working. It may wipe out your
entire hard disk or do even worse things. Use at your own risk.

## Documentation

Full documentation is available at
**[juanjosegarciaripoll.github.io/pony](https://juanjosegarciaripoll.github.io/pony/)**
or in the [`docs/`](docs/index.md) directory:

- [Getting started](docs/index.md)
- [Configuration reference](docs/configuration.md)
- [CLI reference](docs/cli.md)
- [Terminal UI](docs/tui.md)
- [Composer](docs/composer.md)
- [Contacts](docs/contacts.md)
- [Synchronization](docs/synchronization.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)

## Quick start

```bash
# Install dependencies
uv sync

# Check your setup
uv run pony doctor

# Sync and read mail
uv run pony sync
uv run pony tui
```

## Development

```bash
# Lint
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Type check
uv run mypy src/
uv run basedpyright src/

# Tests
uv run python -m pytest tests/

# Build documentation locally
uv sync --group docs
uv run mkdocs serve
```

## License

MIT
