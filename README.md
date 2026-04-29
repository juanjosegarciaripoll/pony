<img src="icons/pony-express.png" alt="Pony Express" width="160" align="right">

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

## Installation

**From GitHub with uv (no clone needed):**

```bash
uv tool install git+https://github.com/juanjosegarciaripoll/pony.git
pony --help
```

**From source:**

```bash
git clone https://github.com/juanjosegarciaripoll/pony.git
cd pony
uv tool install .
pony --help
```

**Prebuilt relocatable archive** — download the `.zip` (Windows) or `.tar.gz`
(macOS/Linux) from the [Releases](https://github.com/juanjosegarciaripoll/pony/releases)
page, extract it anywhere, and run the `pony` executable inside.

**Windows installer** — download `pony-windows-vX.Y.Z-setup.exe` from
[Releases](https://github.com/juanjosegarciaripoll/pony/releases) and run it.
The installer adds `pony` to your PATH automatically.

## Quick start

```bash
# Check your setup
pony doctor

# Sync and read mail
pony sync
pony tui
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

[MIT](LICENSE)
