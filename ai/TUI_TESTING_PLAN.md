# TUI automated test plan

## Context

Pony Express has solid test coverage of services, sync, index store, CLI,
storage backends, and pure helper functions, but **zero automated coverage
of the Textual TUI itself** (`tests/test_folder_panel.py` deliberately
tests only pure helpers to avoid instantiating widgets).  User-facing
regressions — a broken keybinding, a screen that crashes on mount, a
Pilot-driven action that no longer routes through the correct handler —
can only be caught by launching the real TUI manually.

Textual ships headless test primitives (`App.run_test()` + `Pilot`) and
the project already depends on Textual ≥ 8.2.3, so building a useful
TUI test harness is mostly a matter of fixture plumbing and picking the
first slice of flows to cover.  This plan adds a small harness plus
~12 end-to-end flow tests covering the highest-risk TUI surfaces.

The intent is to establish the pattern, not to exhaustively test every
widget; future work (parked in `ai/STATUS.md`) can extend coverage as
the UI evolves.

## Decisions

- **Async style**: new tests are `async def` functions driven by
  `pytest-asyncio` with `asyncio_mode = "auto"`.  Existing
  `unittest.TestCase` tests are not migrated.
- **Scope**: ~12 flow tests covering the main user paths, not a smoke
  test.
- **Snapshot testing** (`pytest-textual-snapshot`): deferred.  Too
  brittle while UI is still evolving; add later if needed.
- **Real vs fake repositories**: real `SqliteIndexRepository` on a temp
  file, real `MaildirMirrorRepository` in a temp dir.  Matches existing
  test idiom (`_make_repo()` in `tests/test_contacts.py:22-27`,
  `test_fixture_flow.py:21-44`) and avoids writing a fake protocol
  surface that would drift from the real one.

## Files to change

### New — `tests/tui_helpers.py`

Single helper module holding constructor functions used by every TUI
test.  No pytest fixtures (codebase pattern prefers plain helpers);
tests call these explicitly.  Public signatures:

```python
def make_tmp_paths(label: str) -> AppPaths: ...
def make_test_account(
    name: str = "acct",
    *,
    with_smtp: bool = True,
    mirror_dir: Path,
) -> AccountConfig: ...
def make_test_config(
    paths: AppPaths,
    *,
    accounts: Sequence[AnyAccount] = (),
) -> AppConfig: ...
def make_index(paths: AppPaths) -> SqliteIndexRepository: ...
def make_mirrors(
    config: AppConfig,
) -> dict[str, MaildirMirrorRepository]: ...
def make_credentials(config: AppConfig) -> CredentialsProvider: ...
def seed_message(
    *,
    index: IndexRepository,
    mirror: MirrorRepository,
    folder: FolderRef,
    raw: bytes,
) -> MessageRef: ...
def build_pony_app(
    *,
    seed: Sequence[tuple[FolderRef, bytes]] = (),
) -> tuple[PonyApp, AppConfig, AppPaths]: ...
def build_compose_app(
    *,
    account: AnyAccount | None = None,
    **compose_kwargs: object,
) -> tuple[ComposeApp, AppConfig, AppPaths]: ...
```

Implementation notes:

- Reuse `tests/corpus.py` for raw message bodies (`plain_text()`,
  `multipart_mixed_attachment()`, `html_only()`, etc.).
- Reuse `tests.conftest.TMP_ROOT` for temp locations — same cleanup
  path every other test uses.
- `seed_message` projects raw bytes to an `IndexedMessage` (via
  `project_rfc822_message`) and writes both the mirror file and index
  row.  This keeps tests declarative: "give the INBOX these two
  messages, then start the app".
- `build_pony_app` must leave `config.mcp = None` so
  `PonyApp.on_mount` does not spawn the MCP thread.
- `build_compose_app` takes `markdown_mode`, `to`, `cc`, `bcc`,
  `subject`, `body` through `**compose_kwargs` and forwards to
  `ComposeApp` — matches the CLI entry point's signature
  (`src/pony/tui/app.py:89-129`).

### New — `tests/test_tui_flows.py`

Contains the 12 flow tests.  Each function is:

```python
async def test_<flow>(monkeypatch: pytest.MonkeyPatch) -> None:
    app, cfg, paths = build_pony_app(seed=...)
    async with app.run_test() as pilot:
        ...
    # assertions against disk / index here — outside the pilot context
```

Side-effect patching uses `monkeypatch` in each test body.  Patch
targets, one rule per hotspot:

| Hotspot | Patch target |
|---|---|
| SMTP send | `pony.tui.screens.compose_screen.smtp_send` → `Mock()` |
| `webbrowser.open` | `pony.tui.widgets.message_view.webbrowser.open` → `Mock()` |
| External editor | `pony.tui.screens.compose_screen.subprocess.run` → `Mock()`; `App.suspend` → `contextlib.nullcontext()` |
| Attachment file-launch | `pony.tui.screens.main_screen.subprocess.run` → `Mock()`; `os.startfile` on Windows via same module attr |
| `tempfile.NamedTemporaryFile` | left real — writes into `TMP_ROOT` |

The 12 flows (implement in this order; stop and commit after 5 if the
plumbing proves too expensive):

1. **`test_f1_opens_and_dismisses_help`** — Pilot presses `F1`; asserts
   `HelpScreen` on the screen stack.  Presses `F1` again; asserts it's
   gone.  Cheap regression guard for app-level bindings.
2. **`test_main_opens_message_shows_body`** — seed one `plain_text()`
   message in INBOX; Pilot presses Enter on the list row; asserts
   `MessageViewPanel` now displays the subject string.
3. **`test_compose_send_happy_path`** — launch `ComposeApp`, Pilot fills
   To / Subject / Body, hits the send chord; asserts `smtp_send` called
   once with the expected envelope, and a raw message landed in the
   account's Sent mirror folder.
4. **`test_compose_send_requires_to`** — same as above but To blank;
   asserts `smtp_send` was NOT called and `ComposeScreen` is still
   on the stack.
5. **`test_open_in_browser_calls_webbrowser`** — seed one `html_only()`
   message; Pilot presses `w`; asserts `webbrowser.open` called with a
   `file://` URI whose file exists under `TMP_ROOT`.
6. **`test_trash_flips_status`** — seed two messages; Pilot presses `d`
   on the second; asserts `IndexedMessage.local_status == TRASHED` for
   that row and the first row is unchanged.
7. **`test_toggle_flagged`** — seed one message; Pilot presses `!`;
   asserts `MessageFlag.FLAGGED` in `local_flags`.  Presses again;
   asserts flag cleared.
8. **`test_mark_read`** — seed one unread message; Pilot presses `R`;
   asserts `MessageFlag.SEEN` in `local_flags`.
9. **`test_archive_moves_to_configured_folder`** — account with
   `archive_folder="Archive"`; seed one INBOX message; Pilot presses
   `A`; asserts index row in Archive folder, mirror file in
   `.Archive/cur/`, no row left in INBOX.
10. **`test_copy_to_folder`** — seed one message; Pilot presses `C`,
    selects the target in `PickFolderScreen` via Pilot; asserts two
    index rows (source + target) and two mirror files.  Source row's
    Message-ID is preserved; target row has a synthetic MID (same
    account) — assert they differ.
11. **`test_search_dialog_returns_results`** — seed three messages with
    distinct subjects; Pilot presses `/`, types a query matching one,
    Enter; asserts `MessageListPanel` shows exactly one row with the
    matching subject.
12. **`test_folder_tree_next_inbox`** — two accounts, each with an
    INBOX; Pilot presses `N`; asserts `FolderPanel.cursor_node` moved
    to the second INBOX.  Presses `P`; asserts cursor back on the
    first.  Regression guard for the INBOX-jump bindings.

### Modify — `pyproject.toml`

```toml
[dependency-groups]
dev = [
    ...existing...,
    "pytest-asyncio>=0.24",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

No other config changes.  `basedpyright` already includes `tests/`
(`pyproject.toml:46`) so `tui_helpers.py` and `test_tui_flows.py` get
type-checked automatically; both must pass in standard mode.  `mypy
strict` checks only `src/pony` (package scope), so new test files are
not subject to strict-mode rules.

### Parked (not in this PR) — `ai/STATUS.md`

After this lands, add a bullet noting:
- Snapshot tests (`pytest-textual-snapshot`) as a future option once
  UI stabilises.
- Per-widget unit coverage where still thin.
- Pilot-driven tests for `ContactBrowserScreen` edit / merge paths
  and `SyncConfirmScreen` phase transitions (non-trivial because
  of worker-thread interaction).

## Reuse inventory (do NOT reinvent)

- `tests/corpus.py` — `plain_text()`, `multipart_mixed_attachment()`,
  `html_only()`, `encoded_headers()`, constants `FROM_ADDR`, `TO_ADDR`,
  `MESSAGE_ID`, etc.
- `tests/conftest.py:TMP_ROOT` — shared temp dir with `atexit`
  cleanup; all helper paths go under this root.
- `tests/test_fixture_flow.py:21-44` — `AppConfig`/`MirrorConfig`
  builder template; adapt for `make_test_config`.
- `tests/test_contacts.py:22-27` `_make_repo` — pattern for
  `make_index`.
- `src/pony/message_projection.py:project_rfc822_message` — raw bytes
  → `IndexedMessage`; used by `seed_message`.
- `src/pony/storage.py:MaildirMirrorRepository.store_message` — writes
  a raw message into a maildir folder and returns its storage key.

## Verification

Local developer loop:

```
uv sync --group dev       # pulls pytest-asyncio
uv run pytest tests/test_tui_flows.py -v     # new suite alone
uv run pytest                                # full suite
uv run ruff check .
uv run mypy
uv run basedpyright
```

Success criteria:

- `pytest tests/test_tui_flows.py` shows all 12 tests passing.
- Full `pytest` still reports the previous 369 + 12 = **381 passing**
  (plus the existing 8 skipped).
- `ruff`, `mypy`, `basedpyright` all clean.
- No test relies on a live network, a real editor, a real browser, or
  a real SMTP server — confirmed by the monkeypatch rules above.

## What could go wrong

- **Pilot keystroke routing**: if a binding lives on a focused widget
  that isn't the one Pilot targets, `pilot.press()` will be a no-op.
  Mitigation: after `run_test()`, explicitly `pilot.focus(widget)`
  before issuing key presses that depend on widget focus.
- **Worker threads in sync action**: `MainScreen.action_sync` runs in
  `run_worker(thread=True)`.  Tests should not trigger sync; if they
  do, they need `await pilot.wait_for_workers()`.  The 12 flows above
  avoid sync entirely.
- **External editor path** (`ctrl+x e` in ComposeScreen): if any test
  accidentally triggers this without patching `subprocess.run` +
  `App.suspend`, the test will hang.  Mitigation: the compose tests
  listed above never press that chord.
- **Windows `os.startfile`**: `main_screen._launch_file` uses
  `os.startfile` on Windows and `subprocess.run` elsewhere.  The
  attachment tests (none in the first 12 — parked for later) will need
  to monkeypatch `os.startfile` conditionally.
