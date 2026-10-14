# Pony Express — Agent Instructions

Python 3.13 MUA **and calendar**, one program, two front ends:

- Mail (`src/pony`): IMAP sync → Maildir/mbox mirror → SQLite index → Textual TUI (`pony tui`) or browser UI (`pony web`); SMTP out, optional Markdown compose.
- Calendar (`src/pony/calendar`): CalDAV sync → `.ics` mirror → SQLite index + recurrence/alarm cache → its own screens, hosted by the mail app (++f2++), and its own pages in the browser UI.

Both front ends offer the same operations, because the operations are
not in either of them: see **Where an operation lives** below.

One config file, one notification space, one entry point (`pony`).
**The two models never import each other** — `tests/test_layering.py`
fails if one does. What the calendar needs from its host arrives as
optional collaborators on `TuiServices` (`invitation_sender`,
`attendee_completer`), so `tests_calendar/` exercises the calendar model
with no mail code in the way.

## Docs

| File | Purpose |
|---|---|
| `docs/architecture.md` | Package layout, subsystems, data flow, **how mail and calendar are one program** |
| `ai/calendar/README.md` | The calendar's own specs: sync, recurrence, MCP, scope |
| `docs/synchronization.md` | Sync algorithm, schema, conflicts (see its Implementation reference) |
| `ai/CONVENTIONS.md` | Quality gates, style, build |
| `ai/STATUS.md` | Scope, goals, delivered + queued, deferred |
| `ai/SECURITY.md` | Threat model + patched findings |
| `config-sample.toml` | Config reference |
| `CHANGELOG.md` | Release history |

## Rules

1. **Read first.** Use `docs/architecture.md` to locate the right module.
2. **Quality gates after every change:** `ruff check src tests tests_calendar`, `ruff format --check src tests tests_calendar`, `uv run mypy` (checks both packages), `basedpyright`, `pytest`. Run `uv run python -m pytest` — never pass `--no-cov`; it runs **both** suites (`tests/` and `tests_calendar/`). The CI enforces **85 % combined statement+branch coverage** (`--cov-fail-under=85` in `pyproject.toml`). New code must ship with tests; do not lower the coverage percentage.

   A development environment needs the web extra as well as the dev
   group, because the suite covers both front ends:
   `uv sync --group dev --extra web`. Both CI workflows pass it.

   **Run `basedpyright` with no arguments, as the CI does.** Its
   `include` in `pyproject.toml` is `["src", "tests"]`, so a path
   argument narrows the check: `basedpyright src` leaves every test file
   unchecked, and the test suite is where most new code lands. One run
   of it that way sent an `Optional` member access to CI unnoticed. On a
   machine with under about 2 GB the bare run dies with a V8 heap
   overflow — node sizes its heap from the memory it finds, not from the
   work — and the equivalent is the same check in two passes,
   `basedpyright src` *and* `basedpyright tests`. Not one of them.

   The run is parallel: `addopts` carries `-n auto --dist loadfile`
   (pytest-xdist). Most of the suite drives a Textual app through `Pilot`
   and pays for starting one, and there is no hot spot to cut — the
   slowest single test is 11 s and the rest is a flat tail of 1–2 s
   startups — so running them at once is the only lever there is. It
   halves the wall clock (7 m 33 s → 3 m 51 s on two cores) and coverage
   aggregates across the workers unchanged. `loadfile` keeps a whole file
   on one worker; do not switch to plain `--dist load`, which would split
   a file's classes across workers.

   **While iterating, run the files you touched, not the suite.** The
   full run belongs at a commit boundary. A model layer under `src/pony`
   or `src/pony/calendar` has unit tests that finish in seconds
   (`tests/test_mailbox_actions.py`, `tests_calendar/test_event_actions.py`);
   reach for those before the Pilot-driven ones.
3. **Never run a mutating command against the live environment.** Not
   `pony sync`, `reset`, `rescan`, a `POST /action/*` on a running server,
   or `pony web`/`tui` against the real data directory. Verify in the test
   suite, or with `--dry-run`, or in an isolated environment.

   **A `--config` alone is not isolation.** A config declares `data_dir`
   (where the index lives, and what relative `mirror.path` resolves
   under) and `profile` (which data directory it owns). An isolated run
   needs a config whose `data_dir` points somewhere scratch:

   ```toml
   config_version = 3
   data_dir = "/tmp/scratch-data"
   profile = "scratch"
   ```

   ```bash
   pony --config /tmp/scratch.toml sync --dry-run
   ```

   Pony stamps `profile` into `<data_dir>/profile.toml` and refuses any
   config declaring a different profile for that directory. Keep both
   keys required and the stamp check ahead of anything that opens the
   index. `pony --config X doctor` prints the paths a run will use.

   `PONY_ROOT` relocates every directory a run writes to underneath it,
   including a `data_dir` a config declares. `tests/conftest.py` sets it
   for the whole session, which is why no test can reach a real
   directory; do not remove it, and do not add a second variable.

4. **No speculative complexity.** No feature flags, compat shims, unused abstractions.
5. **Runtime deps:** `imapclient`, `textual`, `markdown-it-py`, `tinymcp`, `icalendar`, `python-dateutil` — new ones need approval. The browser front end's `starlette` + `uvicorn` are an **extra** (`[project.optional-dependencies] web`), not required deps; keep it that way, and add nothing to it without asking. `pony.web` must never import Textual and nothing outside it may import Starlette — `tests/test_layering.py` enforces both.
6. **Keep docs in sync:** `config-sample.toml` ↔ config model (both halves); `docs/architecture.md` ↔ package layout and subsystems. There is exactly one architecture document — it is published, so it is the one that must be right. Do not add a second copy under `ai/`.
7. **Never touch version strings.** Release workflow stamps `pyproject.toml` + `version.py` from `CHANGELOG.md`.
8. **Tests:** `unittest` run via `pytest`. Sync: `FakeImapSession`. Storage: shared conformance suite. TUI: `build_pony_app` / `build_compose_app` in `tests/tui_helpers.py` + Textual `Pilot`.
9. **CI runs in UTC; your machine probably does not.** The calendar's
   screens render times in the local zone, so a test that leaves a form
   field at a clock-derived default passes or fails depending on where it
   runs. Two of them were green in CEST and red on every runner for exactly
   that reason. Before pushing anything that touches dates or times, run
   `TZ=UTC uv run python -m pytest` — a green suite in one zone proves
   nothing about another. Pin both ends of a time range explicitly in tests
   rather than relying on a default.

   Known, pre-existing, and **not** CI-blocking: three timeline-grid tests
   in `tests_calendar/` fail east of about UTC+9
   (`test_is_full_day_rejects_timed_events`,
   `test_compute_hour_range_default_when_all_events_inside`,
   `test_mouse_click_on_timed_event_opens_detail_modal`, plus
   `test_enter_on_event_cell_pushes_detail_modal` at UTC+14). They fail in
   the calendar's own repository too; the day-boundary arithmetic in the
   grid is what needs fixing, not the tests.

10. **Live IMAP tests are opt-in.** `PONY_LIVE_IMAP=1 uv run python -m pytest tests/test_imap_live.py` runs the sync engine against a real Dovecot, started unprivileged by `scripts/dovecot_userspace.sh` (first run downloads it under `~/.cache`). Without the variable they skip, so the default run needs no server. Use them for anything touching UID handling, UIDVALIDITY or APPEND — a fake decides those for itself.

11. **Browser UX tests are opt-in.** `PONY_BROWSER_UI=1 uv run python -m pytest tests/test_web_browser.py` serves the real app on a loopback port, loads it in a headless Chromium (`--dump-dom`) with a driver page around it, and presses keys at it. It installs nothing — only a `chromium` / `google-chrome` on `PATH` — and without the variable, or without a browser, it skips, so the default run needs neither. Use it for anything in `static/pony.js` whose behaviour matters: the rest of that file is checked by *reading* it (`tests/test_web_dispatch.py`), which catches a name that does not exist but not a mistake in what the code does.

    The same switch guards `tests/test_html_policy.py`, which loads a message that tries to navigate itself away — once under `MESSAGE_HTML_CSP` and once without it — and reads the DOM back with `--dump-dom`. The policy's `sandbox` is the only thing that stops a `<meta http-equiv="refresh">`, and nothing but a browser can show that it does. It uses no iframe and no driver page: one URL, one `--dump-dom`, and the control case proves the probe can see a real hijack.

## Coverage requirements

The CI gate is **85 % combined statement+branch** (see `pyproject.toml → [tool.pytest.ini_options]`). The current baseline is **92.2 %** across both packages, measured on the full suite after the browser front end landed — mail alone sat at 96.20 % before the calendar merge, and the calendar's own suite runs to a lower figure, so the combined number fell without any mail coverage being lost. Regenerate rather than trusting it: every figure recorded here has drifted within a release or two, and a stale number reads as a regression against an honest run.

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
| Mailbox actions (archive/move/copy/trash/flags) | `tests/test_mailbox_actions.py` — real stores, no app |
| Filing a draft or sent copy, contacts | `tests/test_contact_actions.py` |
| Message-list row projections | `tests/test_views.py` |
| Calendar event operations | `tests_calendar/test_event_actions.py` |
| Sync / IMAP | `tests/test_sync.py` with `FakeImapSession` |
| SMTP send | `tests/test_smtp_sender.py` — patched `smtplib.SMTP_SSL` / `SMTP` |
| Sync failures + races | `tests/test_sync_execution_failures.py` |
| TUI screens | `tests/test_tui_flows.py` via `build_pony_app` + `Pilot` |
| Main screen by concern | `tests/test_main_screen_{messages,compose,sync,io}.py` |
| Message list widget | `tests/test_message_list_panel.py` |
| Compose widgets | `tests/test_compose_screen_widgets.py` — buttons, paste, account resolution |
| Index / storage | `tests/test_index_store.py`, `tests/test_storage_conformance.py` |
| Calendar config section | `tests/test_calendar_config.py` |
| Notification space | `tests/test_notifications.py` |
| Mail + calendar in one app | `tests/test_calendar_in_app.py` — `build_pony_app(calendar=make_calendar_runtime(...))` |
| Browser front end | `tests/test_web_app.py` — Starlette `TestClient` over real stores |
| Browser compose and send | `tests/test_web_compose.py` — patched `pony.delivery.smtp_send` |
| Browser contacts | `tests/test_web_contacts.py` |
| Browser calendar views | `tests/test_web_calendar.py` |
| Browser invitations | `tests/test_web_invitations.py` |
| The HTML builder's escaping | `tests/test_web_html.py` |
| Browser flows, as a user walks them | `tests/test_web_flows.py` — links and forms read off the page itself |
| `/keymap.json` ↔ `pony.js` ↔ the buttons | `tests/test_web_dispatch.py` |
| The dispatcher executed in a real browser | `tests/test_web_browser.py` — **opt-in**, see rule 11 |
| The policy message HTML is shown under | `tests/test_html_policy.py` — the directives always, a real browser **opt-in** |
| Reading a served page or `pony.js` | `tests/web_helpers.py` |
| The shared keymap | `tests/test_keymap.py` — both tables, and that the screens bind exactly them |
| Answering an invitation | `tests/test_invitation_actions.py` — no screen, no socket |
| Calendar grid geometry | `tests_calendar/test_grid_projections.py` |
| Invitations (parsing, replies) | `tests/test_invitation.py` |
| Invitations in the reader | `tests/test_invitation_flows.py` |
| Invitations out of the calendar | `tests/test_invitation_sending.py` |
| `pony calendar ...` | `tests/test_cli_calendar.py` |
| Everything calendar-internal | `tests_calendar/` — the calendar model's own suite |
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
`unittest.IsolatedAsyncioTestCase`: it closes the event loop on teardown, so
anything later in the session that reaches for the running loop fails
depending on file order. The contact-suggester tests in
`tests/test_screens.py` used to be exactly that casualty — they now `await`
directly instead of driving `asyncio.get_event_loop()`, so the trap is one
step less sharp, but the rule stands.

`tests_calendar/` still has three `IsolatedAsyncioTestCase` classes
(`test_tui_flows.py`, `test_mcp_server.py`, `test_mcp_transport.py`), carried
over with the calendar. They used to be harmless because `testpaths` ran
`tests/` first; under `-n auto` there is no such order at all — each worker
takes whole files as they come free, so what runs after a closed event loop
is now down to the scheduler. Do not add a fourth, and do not write a test
that depends on anything another file did.

Rank by **absolute uncovered statements+branches**, not percentage. Regenerate
the ranking rather than trusting this list — it is a snapshot:

```bash
uv run python -m pytest --cov-report=json:cov.json   # then sort files by missing_lines + missing_branches
```

Largest remaining gaps, by absolute missing statements+branches, across
both packages:

| Missing | File | % |
|---:|---|---:|
| 292 | `pony/cli_calendar.py` | 77.59 |
| 117 | `pony/cli.py` | 94.85 |
| 94 | `pony/tui/calendar/screens/main_screen.py` | 84.84 |
| 94 | `pony/calendar/sync.py` | 87.02 |
| 70 | `pony/calendar/recurrence.py` | 83.41 |
| 63 | `pony/calendar/ingest.py` | 82.20 |
| 61 | `pony/calendar/locking.py` | 46.02 |
| 59 | `pony/tui/app.py` | 85.29 |
| 50 | `pony/storage.py` | 92.78 |
| 49 | `pony/tui/screens/main_screen.py` | 95.71 |

The calendar's files dominate this list because its suite was written to
its own 85 % floor, not Pony's habits — that is where the headroom is.
`pony/calendar/locking.py` is the calendar's `credentials.py`: half of it is the
Windows branch and cannot run on Linux CI.

`pony/credentials.py` is platform-gated and cannot rise on Linux CI
(below). Pony's `cli.py`, `sync.py` and `main_screen.py` are mostly the
verified-unreachable arms below — read that section before spending effort
on them. `pony/cli.py` rose in missing lines when `pony web` gained its
credentials and scheduler wiring: launching a real server is not
something the suite does, so those lines sit with `run_tui`'s.

The browser front end does not appear in that list: every module in
`pony/web` is at 92 % or better, and what is left uncovered is the
rendering arms a request does not take — a message with no attachments
*and* no invitation, a folder with no mirror. Every route, action and
refusal has a test (`tests/test_web_*.py`).

**Extraction lowers the donor file's percentage.** Collapsing N duplicated
copies into one helper removes N-1 *covered* lines from the file they left.
If those lines were better covered than the file's average — duplicated
logic usually is, since one test exercises every copy — the donor's
percentage falls even though the code improved. `main_screen.py` went
97.62 → 97.50 while losing 25 lines for exactly this reason, and then
96.82 → 94.48 on the way from 2406 lines to 2004, while the project total
rose. Judge a refactor by the project total and by missing-line counts,
not by the donor file's percentage.

## Coverage that is not worth chasing

**Platform-gated.** `credentials.py` sits at 83.5 % and cannot rise on Linux
CI: every uncovered line is inside `_dpapi_encrypt` / `_dpapi_decrypt`
(`sys.platform == "win32"`) or the macOS `ioreg` branch. `cli.py`'s
`editor = "notepad"` is the same. `action_print_pdf` needs an external
converter and `action_edit_external` spawns an editor.

**Verified unreachable** — each of these was attempted and found to be dead or
defensive, so re-deriving it is wasted effort:

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

## Where an operation lives

**An operation goes in an action module, not in a screen.** Archive,
move, copy, trash, flags, folder creation, sending a message, saving a
message or one of its parts, the contact operations, answering an
invitation and the calendar's event operations each live over the
stores (`pony/mailbox_actions.py`, `pony/delivery.py`,
`pony/message_export.py`, `pony/contact_actions.py`,
`pony/invitation_actions.py`, `pony/calendar/event_actions.py`) and return a
`pony.outcome.Outcome` — a count of what changed, and the notices to
show. An operation that returns something as well returns it beside the
outcome (`FiledMessage`, `FlaggedMessage`), never instead of it. No `app.notify`, no `query_one`, no Textual import. An operation
that blocks (SMTP, a sync) is a plain blocking call: the terminal hands
it to a worker thread, the browser to the thread pool.

**A key goes in `keymap.py`, not in a `BINDINGS` list.** `pony.keymap`
is the mail reader's table and `pony.calendar.keymap` is the calendar's, which
also holds the vocabulary both share (`KeyBinding`, `for_surface`,
`web_key`) because the dependency arrow points that way. Each front end
converts: `to_textual` for the terminal, `/keymap.json` for the browser,
one entry per page surface. A key written straight into a screen's
`BINDINGS` exists in the terminal and nowhere else, and
`tests/test_keymap.py` fails when a screen stops matching its table.
The browser marks a key it cannot do as unavailable rather than
swallowing the press, and `tests/test_web_dispatch.py` fails if the
server calls an action available that `static/pony.js` does not
implement, or serves a key no press could produce.

**A message's parts are numbered in one place.** `0` is the message as
it arrived, `T` is its body as text with no HTML in it, and `1..N` are
its attachments — `pony/message_export.py` defines that, and the `s`
key, the `S` prompt, the digit accelerators, the parts list in both
front ends and `/message/attachment?n=` all read it from there. A front
end that invents its own numbering is how `s` came to save
body-as-Markdown in the terminal and the raw `.eml` in the browser.

**A projection goes in `views.py`, not under `tui/`.** `pony/views.py`
and `pony/calendar/views.py` decide what a row is *of* — and, for the
calendar, which cell or column a span occupies and how tall the hour
axis is; padding it to a column width or wrapping it in a Rich `Text` is
the widget's half. Emphasis
travels as data (`MessageRow.unread`, `pony.calendar.views.RowEmphasis`) so a
second front end can render it as a CSS class.

**A page goes in its own `pony/web` module, not in `app.py`.** `app.py`
is routes, the token gate and the action table; the markup for mail,
compose, contacts and the calendar lives in a module each, the chrome in
`layout.py`, and every link in `urls.py`. Account, folder and message
travel as **query parameters** — a folder may be called `INBOX/2024`,
which no path segment can hold — so build links with `urls.*` rather
than by hand.

A screen gathers what the cursor and the marks point at, calls one
action, reports its notices and repaints — see
`MainScreen._act_on_targets`; a route does the same and returns the
`Outcome` as JSON. New operations get unit tests against the stores
rather than a `Pilot`; see the table above.
See `docs/architecture.md → Actions and projections` and
`→ The browser front end`.

## Local mutations

TUI actions that round-trip to the server (archive, compose, folder create) set `uid IS NULL` on the index row. Sync planner is the sole observer — emitting `PushMoveOp` or `PushAppendOp`. No parallel queues or status flags. See `docs/synchronization.md`.

The calendar works the same way with its own signal: `href IS NULL` on a
component row means it should exist on the server but has not been
confirmed there. Same rule — no pending-operations table on either side.
See `ai/calendar/SYNCHRONIZATION.md`.

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
