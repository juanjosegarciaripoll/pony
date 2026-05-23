# Pony Express — Security Findings

## Threat model

Remote attacker controls email content (headers, MIME structure, attachment
filenames, HTML body) delivered to a Pony Express user via a third-party mail
server.  Goal: compromise the local machine.

---

## Confirmed vulnerabilities (patched)

| # | Finding | Severity | File(s) | Status |
|---|---------|----------|---------|--------|
| 1 | **Path traversal via `Content-Disposition` filename (Save dialog path)** — `filename="../../.bashrc"` pre-populated the Save dialog; `(dest / item.filename).write_bytes()` could write outside the chosen directory. | HIGH | `tui/screens/save_message_screen.py`, `tui/screens/main_screen.py` | Fixed |
| 2 | **Path traversal via `Content-Disposition` filename (direct-save path)** — `save_attachment()` → `_unique_path(dest_dir, payload.filename)` used the raw filename directly, bypassing the dialog-level fix. | HIGH | `tui/widgets/message_view.py` | Fixed |
| 3 | **Path traversal via Subject-derived `.eml` filename** — nested `message/rfc822` attachments used the raw Subject header as a filename stem. | HIGH | `tui/message_renderer.py` | Fixed |
| 4 | **Subject slug admitted `..`** — `_subject_slug()` allowed `.` through, so a subject `".. "` produced the slug `".."` as a body filename stem. | MEDIUM | `tui/screens/save_message_screen.py` | Fixed |
| 5 | **Terminal escape injection via email headers** — `_escape()` escaped `[` for Rich markup but did not strip `\x1b` or other C0 control characters.  A Subject or From header containing ANSI escape sequences could corrupt terminal state (clear screen, move cursor, change colours) when the message was displayed. | MEDIUM | `tui/widgets/message_view.py` | Fixed |

### Mitigations applied

**`tui/widgets/message_view.py`**

- `_escape()` now strips all C0/C1 control characters (except tab, LF, CR)
  before the Rich `[` escaping step, preventing terminal escape injection.
- `_unique_path()` now strips directory components via `Path(filename).name`
  and removes remaining control characters before constructing the output path,
  closing the direct-save path traversal.

**`tui/screens/save_message_screen.py`**

- New `_sanitize_attachment_filename()`: strips directory components via
  `Path(filename).name`, removes control characters, and rejects bare `.`/`..`.
- `_proposed_attachment_filename()` now calls `_sanitize_attachment_filename()`.
- `_subject_slug()` collapses multi-dot sequences (`..`, `...`) to a single dot.

**`tui/message_renderer.py`**

- New `_safe_filename_stem()` and `_UNSAFE_FILENAME_CHARS_RE`: replaces
  forbidden path characters and strips leading/trailing dots and spaces.
- All three `f"{subj}.eml"` constructions now use `_safe_filename_stem(subj)`.

**`tui/screens/main_screen.py`**

- `_on_folder()` resolves each output path and checks `is_relative_to(dest)`
  before writing.  Files that escape the destination directory are skipped and
  counted as failures, providing defense-in-depth independent of dialog-level
  sanitization.

---

## False positives

| Claim | Why it does not apply |
|---|---|
| XSS via HTML event handlers (`onclick`, `onload`, …) | Pony Express renders email in a Textual terminal UI — there is no JavaScript runtime.  HTML is converted to plain text before display. |
| SQL injection | All queries in `index_store.py` use `?` parameterized placeholders.  FTS5 user queries are double-quote escaped. |
| Rich markup injection via link sentinels | Link indices are validated as integers; `kind` is assigned internally, never from email content. |
| ReDoS in `_PLAIN_LINK_RE` | The regex has no nested quantifiers or overlapping alternation — backtracking is linear. |
| `os.startfile()` shell injection | `os.startfile` takes a `Path` object, not a shell string.  No command injection is possible; double-extension attacks remain a user-education issue. |
| RFC 2047 unknown-charset decode | Falls back to `latin-1`, which may corrupt text but cannot execute code. |
