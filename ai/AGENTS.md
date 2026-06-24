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
2. **Quality gates after every change:** `ruff check`, `ruff format --check`, `mypy`, `basedpyright`, `pytest`. Run `uv run python -m pytest` — never pass `--no-cov`. The CI enforces **85 % combined statement+branch coverage** (`--cov-fail-under=85` in `pyproject.toml`). New code must ship with tests; do not lower the coverage percentage.
3. **No speculative complexity.** No feature flags, compat shims, unused abstractions.
4. **Runtime deps:** `imapclient`, `textual`, `markdown-it-py`, `tinymcp` — new ones need approval.
5. **Keep docs in sync:** `config-sample.toml` ↔ config model; `ai/ARCHITECTURE.md` ↔ subsystem layout.
6. **Never touch version strings.** Release workflow stamps `pyproject.toml` + `version.py` from `CHANGELOG.md`.
7. **Tests:** `unittest` run via `pytest`. Sync: `FakeImapSession`. Storage: shared conformance suite. TUI: `build_pony_app` / `build_compose_app` in `tests/tui_helpers.py` + Textual `Pilot`.

## Coverage requirements

The CI gate is **85 % combined statement+branch** (see `pyproject.toml → [tool.pytest.ini_options]`). The current baseline after the 0.7.x series is in this range; do not regress it.

**Every new function or branch must have a corresponding test.** Coverage is measured per commit in the release workflow; a drop below 85 % fails the build.

Key test infrastructure:
| Need | Use |
|---|---|
| CLI commands | `tests/test_cli.py` — call `main([...])` with a temp `AppPaths` |
| MIME rendering | `tests/test_attachment_extraction.py`, `tests/test_link_rendering.py` |
| Message projection | `tests/test_message_projection.py` |
| Sync / IMAP | `tests/test_sync.py` with `FakeImapSession` |
| TUI screens | `tests/test_tui_flows.py` via `build_pony_app` + `Pilot` |
| Compose screen | `tests/test_save_message_screen.py`, `tests/test_compose_utils.py` |
| Index / storage | `tests/test_index_store.py`, `tests/test_storage_conformance.py` |

Modules currently below 85 % that need the most attention (in priority order):
1. `tui/screens/` — most screens are 0 %; add smoke tests via `Pilot`
2. `cli.py` — many subcommands untested; use `main([...])` pattern
3. `imap_client.py` — use `FakeImapSession` or mock at the socket level
4. `mcp_server.py` — basic tool-call round-trips
5. `credentials.py` — env-var, command, and encrypted paths

## Local mutations

TUI actions that round-trip to the server (archive, compose, folder create) set `uid IS NULL` on the index row. Sync planner is the sole observer — emitting `PushMoveOp` or `PushAppendOp`. No parallel queues or status flags. See `ai/SYNCHRONIZATION.md`.

## Build

```bash
uv sync --group build --group docs
uv run python scripts/build.py             # portable archive
uv run python scripts/build.py --installer # + platform installer
```

Artifacts → `artifacts/`. `pony.spec` controls bundling. `paths.bundled_docs_path()` detects frozen execution.

## Releasing (agent runbook)

One dispatch-driven workflow does everything: `.github/workflows/release.yml`
(`prepare → build → publish`). To cut version `X.Y.Z`:

1. Make sure `main` is green and up to date (`git pull`).
2. Edit `CHANGELOG.md`: add `## [X.Y.Z]` **with no date** as the **very first**
   heading, with the release notes under it. `X.Y.Z` must be **strictly
   greater** than the `version` in `pyproject.toml`. Do **not** touch any
   version string — the workflow stamps `pyproject.toml` + `version.py`.
3. Commit and push to `main`.
4. Trigger the release: `gh workflow run release.yml`
   (add `-f prerelease=true` for a pre-release).
5. Watch it:
   `gh run watch "$(gh run list --workflow release.yml -L1 --json databaseId -q '.[0].databaseId')"`.
   It stamps the date + version files, pushes the bump commit, builds/tests on
   Linux + macOS + Windows, then tags `vX.Y.Z` and publishes the release with
   binaries attached.
6. If it fails **before** publish: nothing is tagged or released. Fix the cause
   and re-run. The bump commit may already be on `main` — if so, `git revert`
   it before retrying so step 2's version check stays valid.

The workflow aborts (no release) if the first CHANGELOG heading is dated/not a
bare `## [X.Y.Z]`, if the version is not greater than the current one, or if
tag `vX.Y.Z` already exists.

## Do NOT

- Mock the database — use real SQLite via `SqliteIndexRepository`.
- Add `# type: ignore` without a diagnostic code.
- Call `self.app._private_method()` from screens.
- Add a pending-mutations table — set `uid=NULL` on the index row.
- Commit `site/` — generated at build time.
- Add write/mutating MCP tools without approval.
- Create docs files unless asked.
- Add emojis unless asked.
- **Reference AI in git history.** Never mention Claude, Anthropic, "AI", or
  any assistant in commit messages, commit trailers (no `Co-Authored-By` for
  the assistant), PR titles, or PR descriptions. Commits must read as
  ordinary human-authored history.
