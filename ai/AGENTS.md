# Pony Express — Agent Instructions

Terminal-first Python 3.13 MUA: IMAP sync → Maildir/mbox mirror → SQLite index → Textual TUI; SMTP out, optional Markdown compose.

## Docs

| File | Purpose |
|---|---|
| `ai/ARCHITECTURE.md` | Package layout, subsystems, data flow |
| `ai/SYNCHRONIZATION.md` | Sync algorithm, schema, conflicts |
| `ai/CONVENTIONS.md` | Quality gates, style, build |
| `ai/STATUS.md` | Scope, goals, delivered + queued, deferred |
| `ai/SECURITY.md` | Threat model + patched findings |
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

The CI gate is **85 % combined statement+branch** (see `pyproject.toml → [tool.pytest.ini_options]`). The current baseline is **96.5 %**; do not regress it.

**Every new function or branch must have a corresponding test.** Coverage is measured per commit in the release workflow; a drop below 85 % fails the build.

Key test infrastructure:
| Need | Use |
|---|---|
| CLI commands | `tests/test_cli.py` — call `main([...])` with a temp `AppPaths` |
| CLI sync command | `tests/test_cli_sync_cmd.py` — patches `pony.cli.ImapSession` |
| CLI account wizard | `tests/test_cli_account_cmds.py` — patched `input` / `getpass` |
| MIME rendering | `tests/test_attachment_extraction.py`, `tests/test_link_rendering.py` |
| Malformed MIME | `tests/test_mime_edge_cases.py` + the hostile fixtures in `tests/corpus.py` |
| Message projection | `tests/test_message_projection.py` |
| Sync / IMAP | `tests/test_sync.py` with `FakeImapSession` |
| Sync failures + races | `tests/test_sync_execution_failures.py` |
| TUI screens | `tests/test_tui_flows.py` via `build_pony_app` + `Pilot` |
| Main screen by concern | `tests/test_main_screen_{messages,compose,sync,io}.py` |
| Message list widget | `tests/test_message_list_panel.py` |
| Compose widgets | `tests/test_compose_screen_widgets.py` — buttons, paste, account resolution |
| Index / storage | `tests/test_index_store.py`, `tests/test_storage_conformance.py` |
| Dialog / standalone screens | `tests/test_screens.py` — `_make_host(ScreenCls, …)` + `Pilot` |
| Contacts browser | `tests/test_screens.py` via `ContactsApp(contacts=index)` |

Three techniques unlock most of what looks untestable:

- **Blocking UIs.** Patch the App class at its import site
  (`patch("pony.tui.PonyApp")`, `pony.tui.app.ContactsApp`,
  `pony.tui.app.EmlViewerApp`). Everything a launch command does *before*
  `run()` — config load, index setup, local rescan, BBDB import, theme
  resolution — is real work worth testing.
- **Mid-sync races.** `ImapSyncService.plan()` and `.execute()` are separate
  public calls, so a test can plan, mutate the index, then execute. That is
  how the "row vanished" arms are reached.
- **Interactive gates.** Several commands branch on `sys.stdin.isatty()`;
  patch it to reach the interactive side.

**Async tests are plain `async def` functions.** Do not use
`unittest.IsolatedAsyncioTestCase`: it closes the event loop on teardown, and
the contact-suggester tests in `tests/test_screens.py` call
`asyncio.get_event_loop()` directly, so they fail depending on file order.

Rank by **absolute uncovered statements+branches**, not percentage. Regenerate
the ranking rather than trusting this list — it is a snapshot:

```bash
uv run python -m pytest --cov-report=json:cov.json   # then sort files by missing_lines + missing_branches
```

Largest remaining gaps (in priority order):
1. `storage.py` (95 %, ~29) — mbox TOC edge cases; extend the conformance suite
2. `mcp_server.py` (89 %, ~21) and `tui/app.py` (85 %, ~21)
3. `tui/message_renderer.py` (96 %, ~22) — mostly the defensive guards below
4. `cli.py` (98 %, ~45), `sync.py` (98 %, ~25),
   `tui/screens/main_screen.py` (98.5 %, ~18) — see the note below before
   spending effort here

## Coverage that is not worth chasing

**Platform-gated.** `credentials.py` sits at 83.5 % and cannot rise on Linux
CI: every uncovered line is inside `_dpapi_encrypt` / `_dpapi_decrypt`
(`sys.platform == "win32"`) or the macOS `ioreg` branch. `cli.py`'s
`editor = "notepad"` is the same. `action_print_pdf` needs an external
converter and `action_edit_external` spawns an editor.

**Verified unreachable** — each of these was attempted and found to be dead or
defensive, so re-deriving it is wasted effort:

- **`cli.py:411-413` — the `pony some-message.eml` shortcut is dead code.**
  `parser.parse_known_args()` raises on an unknown subcommand choice before
  `main()` ever inspects `extra_args`, so the bare-filename path cannot run.
  Either the parser needs a `nargs="?"` positional or the check has to move
  ahead of parsing. **This is a real bug, not just a coverage gap.**
- `cli.py:621-622` — `parser.error("Unhandled command.")`. Every subcommand
  argparse accepts is handled above it.
- `cli.py:1403-1404` — `folder list`'s `(no folders)` arm. Both mirror
  backends always synthesize INBOX, so the list is never empty.
- `sync.py:1267-1272` — the `suppress_delete_ids` arm. The parameter has a
  default and **no call site ever passes it**, so the set is always empty.
- `sync.py:1958` — `return False` after the `match` in `_execute_one`. The
  branch data shows no op ever fails every `case`.
- `sync.py:486` — `_default_factory` constructs a real network `ImapSession`;
  tests always inject a session factory.
- `main_screen.py:724` — the cursor-restore after `_reload_folder`.
  `load_folder` streams rows from a worker, so `row_count` is still 0 on the
  next line and the guard never passes. **The cursor jumps to the top after
  trash/archive instead of holding its place — a real bug the comment above
  it says should not happen.**
- `message_renderer.py` — the `message/rfc822` branches assuming a non-list
  payload, and the `get_payload(decode=True)` non-bytes guards.
  `email.policy.default` always parses `message/rfc822` into a sub-message
  list, including when the part is base64-encoded (see
  `corpus.base64_rfc822_attachment`).

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
