# Pony Express — Security Findings

## Threat model

Remote attacker controls email content (headers, MIME structure, attachment
filenames, HTML body) delivered to a Pony Express user via a third-party mail
server.  Goal: compromise the local machine.

With `pony web` serving, that content is also rendered into HTML by a
server the user's own browser trusts, so the same attacker gains a
second surface.  What stands between them:

- **Everything is escaped on the way out.** `pony.web.html` escapes
  every text child and quotes every attribute; `Raw` is the only way
  past it, and it is used for markup this program built, never for
  anything a message said.  `tests/test_web_html.py` pins that, and
  `tests/test_web_app.py` checks a hostile subject through a real
  request.
- **Who a message claims to be from is stated, when the server said.**
  `pony.authentication` reads the first `Authentication-Results`
  header and shows whether the signing domain *is* the `From` domain.
  It is deliberately narrow: only the topmost header, because a message
  can arrive carrying reports the sender wrote; alignment compared,
  because `dkim=pass` for an attacker's own domain is trivial to get
  and is what the attack looks like; `spf=pass` alone not treated as a
  pass, because it authenticates the envelope and not the `From` a
  reader sees; and nothing shown at all when no server reported, so the
  indicator stays worth reading.  Pony verifies no signature itself and
  does not claim to — the panel names the server that did.

  It reaches the reader as one padlock, shut only for an aligned pass,
  with a third glyph for a check that ran and failed so that is not
  confused with the ordinary case of no report.  Behind it, in every
  front end, is a paragraph saying what a shut lock does *not* mean: a
  domain's own mail authenticates perfectly well, which is what a
  convincing phishing domain relies on.  An indicator that reassures
  without that sentence would be worse than none.
- **A message body is shown as text.** The reader renders
  `render_message(...).body` — HTML stripped, URLs as text — and
  linkifies only `https?://`, `ftp://` and bare addresses, so a
  `javascript:` URL in a message is text.  The HTML version is a
  separate top-level page under `default-src 'none'; img-src data:;
  style-src 'unsafe-inline'; form-action 'none'; base-uri 'none';
  frame-ancestors 'none'` with `Referrer-Policy: no-referrer`, never a
  frame inside the application, so nothing in a message can reach the
  page around it.  `form-action` and `base-uri` are named explicitly
  because CSP3 does **not** fall them back to `default-src`; without
  them a `<form>` in a message posted to the attacker's server from
  Pony's own origin.  One gap remains: a message can still navigate
  the tab it was opened in, and no directive covers that — a sandbox
  strict enough to stop it also breaks every ordinary link.
- **Attachments are downloads.** Served with
  `X-Content-Type-Options: nosniff` and a quoted filename stripped of
  quotes, backslashes and carriage returns.
- **Nothing the page sends is used as a path.** An uploaded attachment
  is addressed by a 16-hex-digit token that indexes a directory the
  server made; a forward's original is read from the index by message
  reference.  `pony.web.compose.staged_path` refuses anything that is
  not a token.
- **Reachability.** Loopback by default, plus a token compared with
  `secrets.compare_digest` and kept in an `HttpOnly`, `SameSite=strict`
  cookie.  A tailnet address or `tailscale serve` is the intended way to
  publish it.
- **A request has to come from Pony's own page.** Every mutating
  method is refused unless `Sec-Fetch-Site` is `same-origin`/`none`,
  or — for a client that sends neither header — `Origin` is absent or
  matches the request's own authority.  The check is independent of
  the token, because the address is not a secret: the loopback default
  is reachable from the user's browser whatever the network does.

---

## Confirmed vulnerabilities (patched)

| # | Finding | Severity | File(s) | Status |
|---|---------|----------|---------|--------|
| 1 | **Path traversal via `Content-Disposition` filename (Save dialog path)** — `filename="../../.bashrc"` pre-populated the Save dialog; `(dest / item.filename).write_bytes()` could write outside the chosen directory. | HIGH | `tui/screens/save_message_screen.py`, `tui/screens/main_screen.py` | Fixed |
| 2 | **Path traversal via `Content-Disposition` filename (direct-save path)** — `save_attachment()` → `_unique_path(dest_dir, payload.filename)` used the raw filename directly, bypassing the dialog-level fix. | HIGH | `tui/widgets/message_view.py` | Fixed |
| 3 | **Path traversal via Subject-derived `.eml` filename** — nested `message/rfc822` attachments used the raw Subject header as a filename stem. | HIGH | `tui/message_renderer.py` | Fixed |
| 4 | **Subject slug admitted `..`** — `_subject_slug()` allowed `.` through, so a subject `".. "` produced the slug `".."` as a body filename stem. | MEDIUM | `tui/screens/save_message_screen.py` | Fixed |
| 5 | **Terminal escape injection via email headers** — `_escape()` escaped `[` for Rich markup but did not strip `\x1b` or other C0 control characters.  A Subject or From header containing ANSI escape sequences could corrupt terminal state (clear screen, move cursor, change colours) when the message was displayed. | MEDIUM | `tui/widgets/message_view.py` | Fixed |
| 6 | **Textual markup injection via contact fields** — `ContactDetailScreen` passed `display_name`, `affix`, `aliases`, `organization`, email addresses and `notes` directly to `Static()` without escaping.  A crafted `From` display name harvested into the contacts database could inject `[link=…]` or other Rich markup tags, creating false clickable links in the contact detail view. | HIGH | `tui/screens/contact_detail_screen.py` | Fixed |
| 8 | **Cross-site request forgery against every mutating route** — `POST /action/<name>`, `/compose`, `/compose/attachment` and `/contacts/edit` had no origin check of any kind.  Under `--no-token` — the configuration `docs/architecture.md` recommends for a tailnet — any page the user's browser loaded could empty a folder, send mail as them and stop the process, needing no network access at all because the documented `127.0.0.1:8730` default is reachable from their own browser.  Reproduced from a genuinely cross-site origin in headless Chromium.  With a token the attack was refused only by the cookie's `SameSite=strict`, which carried a comment describing it as a convenience for phones. | CRITICAL | `web/app.py` | Fixed |
| 9 | **CSP omitted the directives that do not inherit** — `/message/body` was served under `default-src 'none'; img-src data:; style-src 'unsafe-inline'`.  CSP3 does not fall `form-action` or `base-uri` back to `default-src`, so a `<form>` in a `text/html` message rendered and submitted from Pony's own origin — a password prompt one tab from the mailbox.  Reproduced to a sink in headless Chromium. | HIGH | `web/app.py` | Fixed |
| 10 | **The token comparison raised instead of refusing** — `secrets.compare_digest` rejects non-ASCII `str`, so `GET /mail?token=%C3%A9` returned 500 with a traceback rather than 403.  No bypass; the one function that must never raise, raised, and distinguished itself from an ordinary refusal. | MEDIUM | `web/app.py` | Fixed |
| 11 | **Unbounded upload into memory** — `/compose/attachment` read the whole request body with no size cap, and the staging directory it wrote to was a `mkdtemp` outside `data_dir` and `PONY_ROOT` that nothing ever removed.  A forward staged a fresh copy on every attempt.  With finding 8 this was remotely triggerable; on its own it filled `$TMPDIR`, which on a RAM-backed `/tmp` is memory. | MEDIUM | `web/app.py`, `web/compose.py` | Fixed |
| 12 | **An attachment filename outside Latin-1 could not be served** — `Content-Disposition` was built by stripping three characters and letting Starlette encode it Latin-1, so `документ.pdf` raised `UnicodeEncodeError` and returned 500.  A newline survived the strip; the email parser happened to fold it, so no header injection materialised. | LOW | `web/app.py` | Fixed |
| 13 | **The terminal showed message HTML at a `file://` origin, and left it on disk** — `w` and the `.eml` viewer wrote `build_browser_html`'s output to `NamedTemporaryFile(delete=False)` and opened it as a file URL.  Nothing unlinked it, so a reader accumulated one plaintext copy of every message it opened in `$TMPDIR` — outside `data_dir`, so `pony reset` did not reach it, and resident in memory on a machine whose `/tmp` is a tmpfs.  At a file origin none of `MESSAGE_HTML_CSP` applied, so the same bytes the browser front end serves with scripts, network and framing switched off ran with all three available, from a local-file origin. | HIGH | `tui/screens/main_screen.py`, `tui/widgets/message_view.py` | Fixed |
| 14 | **A message could navigate the tab it was opened in** — `<meta http-equiv="refresh">` is not a fetch, so `default-src` does not cover it and CSP3 dropped `navigate-to`; no directive in the policy applied to it.  The page a reader opened to read a message could send itself to the sender's server, which is a phishing hop that starts from inside the mailbox.  It was a knowingly accepted limitation: the comment beside the policy argued a sandbox strict enough to stop it would break every link in the message.  Measuring Chromium 154 showed that is not so — `sandbox allow-popups allow-popups-to-escape-sandbox` blocks the refresh while leaving same-tab links, `target=_blank` links, inline styles and `data:` images working. | MEDIUM | `html_policy.py` | Fixed |
| 7 | **NUL-byte injection of format sentinels** — The renderer uses `\x00` as an internal delimiter for format tokens (`\x00B1\x00` = bold-open, etc.).  A `text/plain` body with embedded NUL bytes (valid in base64/QP-encoded parts) or an HTML body using `&#0;` entities (converted by `HTMLParser`'s default `convert_charrefs=True`) could inject fake bold/italic/underline/strike formatting into the TUI display. | LOW | `tui/message_renderer.py` | Fixed |

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
- `text/plain` payloads now have NUL bytes stripped after decoding (finding #7).
- `_HTMLStripper.handle_data` strips NUL bytes after whitespace normalisation,
  blocking the `&#0;` entity injection path (finding #7).

**`tui/screens/contact_detail_screen.py`**

- All contact fields (`display_name`, `affix`, `aliases`, `organization`,
  email addresses, `notes`) are passed through `rich.markup.escape` before
  being placed in `Static()` widgets (finding #6).

**`web/app.py`** (findings 8–12)

- `_check_origin()` runs beside `_check_token()` in `_handler`, so one
  place covers every mutating route rather than each one remembering.
- `MESSAGE_HTML_CSP` names `form-action`, `base-uri` and
  `frame-ancestors` explicitly.
- The token is compared only when `offered.isascii()`.
- `MAX_ATTACHMENT_BYTES` is enforced on the declared length *and* by
  counting the stream, so a lying `Content-Length` does not get past it.
- `_content_disposition()` follows RFC 6266: an ASCII-sanitised
  `filename=` plus `filename*=UTF-8''<percent-encoded>`.
- Staging moved to `data_dir/web-attachments`, which `PONY_ROOT`
  contains and `pony reset` clears, with an `atexit` sweep and a
  release on send.

**`browser_preview.py`, `html_policy.py`** (finding 13)

- The document is served from memory instead of written: `127.0.0.1` on
  a kernel-chosen port, one unguessable path, a bodyless 404 for every
  other, and the window closes after two minutes or when the next
  message is opened, whichever comes first.  A terminal-only install
  needs no `web` extra for it — `http.server` answers one GET.
- `MESSAGE_HTML_CSP` moved to `pony.html_policy`, which neither
  transport owns, so the terminal's page and `GET /message/body` cannot
  drift apart.  Both now also send `X-Content-Type-Options: nosniff` and
  `Cache-Control: no-store`; without the latter the browser's own cache
  writes the message to disk, which is the leak serving was meant to
  close.
- `MESSAGE_HTML_CSP` carries `sandbox allow-popups
  allow-popups-to-escape-sandbox` (finding 14).  Each token was measured
  rather than reasoned about: without the first a `target="_blank"` link
  does nothing, and without the second the site the reader clicked
  through to opens inside the sandbox, with its own scripts dead and its
  storage raising `SecurityError`.
  `allow-top-navigation-by-user-activation` is deliberately absent — it
  governs a nested context navigating its ancestor, so it changes
  nothing on a top-level page.  `tests/test_html_policy.py` asserts the
  token list, and under `PONY_BROWSER_UI=1` loads a refreshing page in a
  real browser both with the policy and without it.
- An external viewer needs a real file, which serving cannot answer, so
  `handoff.py` answers it instead: the file goes under `data_dir`, in a
  directory emptied on first use of each run and removed at exit.
  Emptying on first use is what covers a run that was killed, since an
  `atexit` hook does not fire then.  Only the extension comes from the
  message — a viewer is chosen by it — and the name is Pony's own, so
  no attachment filename is ever used as a path.  The external editor's
  draft moved there too, and is now removed in a `finally`.

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
