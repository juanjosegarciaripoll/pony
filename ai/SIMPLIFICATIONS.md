# Code Simplification Backlog

Behavior-preserving refactors identified by a read-only audit of the largest
modules (`cli.py` 3343 LOC, `sync.py` 2258, `tui/screens/main_screen.py` 1939,
`tui/screens/compose_screen.py` 845, `message_renderer.py` 781). Line numbers
are approximate and will drift — locate by function/symbol name.

**Ground rules for every item below:**

- Pure refactors: no public-API or behavior change. Keep CLI output, sync ops,
  and screen flows byte-identical.
- Quality gates must stay green after each item: `ruff check`, `ruff format
  --check`, `mypy src/`, `basedpyright src/`, `uv run python -m pytest` (never
  `--no-cov`; the 85 % combined statement+branch gate is live).
- Extracting a helper can *drop* coverage if the helper has an untested branch.
  When you collapse N copies into one helper, make sure an existing test still
  exercises each branch; add a focused test if not.
- Land items as small independent commits so a regression is easy to bisect.
- The lint surface is already clean (no unused imports / dead vars) — these are
  all structural duplication, not dead code.

Tier 1 = safe, high-value dedup. Tier 2 = function decomposition (higher risk,
may need new tests). Do Tier 1 first.

---

## Tier 1 — safe deduplications (~250 lines)

### cli.py

**1. Account-lookup-by-name helper (6 copies → 1, ~60 lines).**
The pattern below appears ~6× (approx lines 1760, 1796, 1868, 1941, 2669, 3006)
in `_try_render_attachments`, `run_message_body`, `run_message_attachment`,
`run_message_mime`, `run_account_test`, `run_account_set_password`:

```python
acc = next(
    (a for a in config.accounts
     if isinstance(a, AccountConfig) and a.name == account),
    None,
)
if acc is None:
    raise SystemExit(f"No account named {account!r} in config.")
```

Add one module-level helper and replace all sites:

```python
def _require_imap_account(config: AppConfig, name: str) -> AccountConfig:
    acc = next(
        (a for a in config.accounts
         if isinstance(a, AccountConfig) and a.name == name),
        None,
    )
    if acc is None:
        raise SystemExit(f"No account named {name!r} in config.")
    return acc
```

Note the exact `SystemExit` message text varies slightly between call sites —
confirm before unifying, or `test_cli.py` assertions on the message may break.

**2. `_make_mirror` duplication (~10 lines).**
`run_tui` (~2511) and `run_compose` (~2612) each define a local `_make_mirror`
identical to the existing module-level `_build_mirror` (~1351). Delete both
locals; call `_build_mirror(acc)`.

**3. Shared TUI setup block (~20 lines).**
`run_tui` (~2499) and `run_compose` (~2576) repeat:
`paths.ensure_runtime_dirs()` → build `pony-tui.log` path →
`_install_tui_log_handler` → `require_config`. Extract
`_setup_tui_environment(paths, config_path, log_name) -> AppConfig`.
Watch: the two functions may differ in index/credentials setup after this
block — only fold the genuinely identical prefix.

**4. Message-lookup-or-fail helper (4 copies → 1, ~40 lines).**
`run_message_get/body/attachment/mime` (~1648, 1812, 1881, 1954) all do:

```python
hits = _find_messages(index, account_name=account,
                      folder_name=folder, message_id=message_id)
if not hits:
    raise SystemExit(f"Message not found in index: {account}/{folder}/{message_id}")
indexed = hits[0]
```

Fold the not-found `SystemExit` into a `find_or_fail_message(...)` wrapper
around the existing `_find_messages` (itself a 2-line wrapper ~1723).

### sync.py

**5. Timing boilerplate context manager (11 copies → 1, ~50 lines).**
`_t = time.perf_counter(); ...; int((time.perf_counter() - _t) * mult)`
recurs ~11× (approx 742, 764, 777, 915, 925, 1597, 1623…). Add:

```python
@contextmanager
def _measure_ms(store: dict[str, int], key: str) -> Iterator[None]:
    t = time.perf_counter()
    yield
    store[key] = int((time.perf_counter() - t) * 1000)
```

Verify each call site uses the same multiplier (ms vs µs) before swapping — a
couple may scale differently. Keep the timing values identical.

**6. Flag-update helper after fetch/merge (4 copies → 1, ~40 lines).**
The `get_message(ref) → dataclasses.replace(row, <flag fields>, synced_at=now)`
pattern repeats in the `PullFlagsOp` / `PushFlagsOp` / `MergeFlagsOp` /
`RestoreOp` arms of `_execute_one` (approx 1793, 1815, 1838, 1912). Extract
`_replace_flags(row, *, base, server, extra, local, uid, now)`. This is the
highest bug-surface dedup (copy-paste of flag field names) — make sure each op
arm keeps the exact fields it set before.

**7. `FetchNewOp` double dict-lookup (smell, ~4 lines).**
Around 1121 the code calls `uid_to_flags.get(uid, (frozenset(), frozenset()))`
twice to pull `[0]` and `[1]`. Unpack once:
`server, extra = uid_to_flags.get(uid, (frozenset(), frozenset()))`.

**8. `_finalize_message_after_server_op` (~12 lines).**
After successful APPEND (~2028) and MOVE (~2095) the new-UID flag-setting block
is near-identical. Extract one helper taking `(row, new_uid, local, extra, now)`.

### tui/

**9. Shared `_launch_file` (~16 lines).**
Identical platform launcher in `main_screen.py` (~1904) and
`eml_viewer_screen.py` (~164). Move to `tui/terminal.py` (or a new
`tui/utils.py`) and import in both.

**10. Parameterized single-input screen (3 copies → 1 base, ~60 lines).**
`SearchDialogScreen`, `NewFolderScreen`, and `_CreateFolderScreen`
(in `save_folder_picker_screen.py`) all subclass `FloatingInputScreen`, build
`Horizontal(id="floating-bar")` with `Label`+`Input`, and dismiss on submit.
Add a `SimpleInputScreen(label, placeholder, allow_empty=True)` and replace the
three. Keep each screen's distinct result type / validation.

**11. Folder-picker workflow helper (~40 lines).**
`action_copy` (~920) and `action_move` (~1037) in `main_screen.py` share the
targets → resolve → `PickFolderScreen(title)` → nested `_on_dismiss` →
`push_screen` skeleton. Extract `_prompt_folder_picker(label, on_chosen)`.

**12. Compose-preamble helper (4 copies, ~80 lines).**
`compose_reply`, `compose_reply_all`, `compose_forward`, `compose_from_draft`
(~1435–1698 in `main_screen.py`) share: `get_current_message()` + None check →
`_sendable_accounts()` + identical error → load raw bytes (try/except, same
error) → render. Extract `_load_current_message_or_error(verb) -> EmailMessage |
None`. Also collapses the 5 near-identical "requires an IMAP account (SMTP is
needed to send)" notices (~1402, 1445, 1503, 1564, 1622) into one
`_check_sendable_accounts(verb) -> bool`.

**13. `contextlib` import hygiene (~1 line).**
`compose_screen.py` imports `contextlib` inline inside `on_button_pressed`
(~444); hoist to module top to match `main_screen.py`.

---

## Tier 2 — function decomposition (do after Tier 1, may need new tests)

**14. `sync._plan_folders_inner` is 273 lines (~630–903).**
Mixes Gmail-aggregate warnings → stale-folder cleanup → STATUS path selection →
remote-UID collection → per-folder planning → new-folder planning. Extract
cohesive helpers (`_warn_gmail_aggregates`, `_gate_folders_by_status`,
`_collect_remote_uids`, `_plan_new_folders`) and leave the function as an
orchestrator. Each extracted helper needs a direct test or guaranteed coverage
via existing `test_sync.py` cases.

**15. `op.phase` property instead of `isinstance` dispatch (~8 lines).**
Execution splits ops into phase-1 (fetch) vs phase-2 (mutate) via
`isinstance(op, (PushFlagsOp, PushDeleteOp, …))` (~1561). Add an abstract
`phase` property on the op base and let each op declare its phase. Removes a
brittle tuple that must be updated whenever an op is added.

**16. `cli.run_sync` confirmation extraction (~35 lines).**
`run_sync` (~741–907) interleaves setup / plan / confirmation+paging / execute /
report. Pull the confirmation+pager retry loop (~809–849) into
`_handle_sync_confirmation(plan, index, yes, header) -> bool`. Improves
testability of the confirm/pager UX.

**17. `main_screen.archive_current_message` is 104 lines (~790–892).**
Split the ~50-line account/folder precondition block into
`_validate_archive_preconditions(sample) -> FolderRef | None` (returns target or
notifies + returns None), leaving the execution half.

**18. `compose_screen.action_send` is 89 lines (~455–543).**
Extract the SMTP send (~484–523) into `_send_message_via_smtp(...)`, leaving
`action_send` as validation + orchestration.

---

## Remote-UID collection consolidation (sync.py, ~15 lines)

The fast/medium/slow paths each run a near-identical loop populating
`remote_uids_by_folder` for `pending_move_source_folders` (~810–823). Merge into
one `_populate_remote_uids(paths)` helper. (Related to item 14; can land with it.)
