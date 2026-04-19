# Plan: Accent/Case-Insensitive Search via FTS5

Status: **planned, not implemented**.
Scope: one schema break + one new search engine + the consumer cleanups
that the new design makes possible. No migration/rebuild path in this
round — that is deferred.

## Motivation

Today's search is broken for any locale with non-ASCII letters:

- Messages ([src/pony/index_store.py:494-546](../src/pony/index_store.py#L494-L546)) use plain `LIKE`
  with `PRAGMA case_sensitive_like OFF`. SQLite's built-in case folding
  only handles ASCII `A-Z`, so `LOWER("MARÍA") == "MARÍA"` — case-insensitive
  matching silently fails for accented letters.
- Contacts ([src/pony/index_store.py:870-885](../src/pony/index_store.py#L870-L885)) use
  `LOWER(col) LIKE LOWER(?)` with the same limitation.
- Neither path strips diacritics, so `maria` never matches `María`.

For a Spanish-language mailbox this means the search box is effectively
broken. The same issue applies to contact autocompletion.

## Goals

1. Search and contact autocompletion are **diacritic- and
   case-insensitive** by construction.
2. The DB becomes a pure projection for listing + searching. Message
   bodies are read from mirror files when needed, not from the index.
3. The change is **compact** (one shared helper, two FTS5 tables, a few
   query rewrites) and introduces a single schema version bump.

## Out of scope (future work)

- **Migration / rebuild-from-mirrors.** On a schema mismatch the index
  will refuse to open and print an instruction; the migration command
  itself is a later PR. User's current answer is to nuke mirrors and
  re-sync.
- **Synthetic Message-ID rework.** Today's formula
  ([sync.py:341-352](../src/pony/sync.py#L341-L352)) bakes UID into the hash, so
  synthetic IDs cannot survive a rebuild. This matters only once the
  migration command lands — not this round.
- **Search-parser UX changes.** The `from:`/`to:`/`cc:`/`subject:`/`body:`
  field prefixes keep their current semantics. The `case:` prefix
  becomes a no-op (folding is always on); we document it as deprecated
  and ignored.
- **CLI output format** other than removing the leaked `Preview:` line.

## Schema changes

### Base tables

- `messages` — **unchanged**. All existing columns keep their current
  meaning and remain the source of truth for display (TUI message list,
  CLI headers, MCP metadata dict).
- `body_preview` column: **repurposed**. Drop the 4000-byte cap in
  [message_projection.py:163](../src/pony/message_projection.py#L163); store the
  full extracted plain text (post HTML→text conversion). Cap at a
  generous upper bound (256 KB) as pathological-email protection.
  Consider renaming to `body_text` in a later PR if the distinction
  matters; not worth churn for this round.
- `contacts`, `contact_emails`, `contact_aliases` — **unchanged**.

### New FTS5 virtual tables

```sql
CREATE VIRTUAL TABLE messages_fts USING fts5(
    sender, recipients, cc, subject, body_preview,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE contacts_fts USING fts5(
    first_name, last_name, email_addresses, aliases,
    content='',
    tokenize='unicode61 remove_diacritics 2'
);
```

Notes:

- `remove_diacritics=2` strips all combining marks including `ñ → n`
  and `ç → c`. Linguistically imprecise for Spanish (ñ is a distinct
  letter), but the right choice for mail search: users who typed
  `ano` on a non-Spanish keyboard still find `año`.
- Vanilla `unicode61` (no `tokenchars` override) is the right default.
  Email addresses split into their alphanumeric runs
  (`juan.garcia@example.com → [juan, garcia, example, com]`), which
  matches the user's mental model — searching for `garcia` finds the
  address.
- `messages_fts` uses external-content mode (`content='messages'`) so
  the base table is authoritative for text.
- `contacts_fts` uses contentless mode because a contact's searchable
  text is the union of three tables (`contacts`, `contact_emails`,
  `contact_aliases`) — we can't point `content='...'` at one. The
  aggregated strings are built and inserted explicitly by triggers.

### Triggers (sync FTS with base tables)

Standard FTS5 external-content pattern for `messages_fts`:

```sql
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, sender, recipients, cc, subject, body_preview)
    VALUES (new.rowid, new.sender, new.recipients, new.cc, new.subject, new.body_preview);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, sender, recipients, cc, subject, body_preview)
    VALUES ('delete', old.rowid, old.sender, old.recipients, old.cc, old.subject, old.body_preview);
END;
CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, sender, recipients, cc, subject, body_preview)
    VALUES ('delete', old.rowid, old.sender, old.recipients, old.cc, old.subject, old.body_preview);
    INSERT INTO messages_fts(rowid, sender, recipients, cc, subject, body_preview)
    VALUES (new.rowid, new.sender, new.recipients, new.cc, new.subject, new.body_preview);
END;
```

Contacts need custom triggers on all three tables that rebuild the
aggregated row. Exact form deferred to implementation; pattern: on
any change to `contacts` / `contact_emails` / `contact_aliases` for a
given `contact_id`, `DELETE FROM contacts_fts WHERE rowid = ?` and
re-`INSERT` the aggregated fields.

### Schema version gate

- Write the new version to `PRAGMA user_version`. Pick version `2`
  (assume current DB is version `0` / `1`).
- On `initialize()`, read `user_version`. If it's lower than expected,
  raise a clear `SystemExit` with text:

  > Pony's index schema has changed. The safest path right now is
  > to delete `<index_db_file>` and the mirror directory, then run
  > `pony sync` to redownload. A rebuild-from-mirror command is
  > planned but not yet available.

  No fallback logic, no auto-delete — explicit user action.

## Ingest path changes

- `_extract_body_preview` ([message_projection.py:166](../src/pony/message_projection.py#L166)):
  drop the 4000-byte cap at line 163; apply a 256 KB safety cap on the
  final collapsed text.
- No other ingest-side changes. FTS5 is kept in sync by the triggers.

## Search-engine changes

### Shared helper: user input → FTS5 MATCH expression

One function, used by both message and contact search:

```python
def _fts5_query(text: str, *, prefix: bool = False) -> str:
    """Translate a user-supplied term into a safe FTS5 MATCH expression.

    - Double-quotes become doubled (FTS5 phrase escape).
    - The result is wrapped as a phrase to neutralise reserved tokens
      (AND, OR, NOT, NEAR, parentheses, `-`, `:`, `*`).
    - If ``prefix`` is True, appends `*` to get prefix matching.
    """
    escaped = text.replace('"', '""')
    phrase = f'"{escaped}"'
    return phrase + "*" if prefix else phrase
```

### `SqliteIndexRepository.search`

Rewrite to use `messages_fts MATCH`. Field-prefixed terms map to
column-scoped FTS5 queries:

- `from:alice` → `sender:"alice"`
- `subject:foo` → `subject:"foo"`
- `body:bar` → `body_preview:"bar"`
- bare terms → `{subject:"x" OR body_preview:"x"}` (keeps current
  default-scope behavior)

Assemble all terms with implicit AND (FTS5's default). Execute as:

```sql
SELECT m.* FROM messages m
JOIN messages_fts f ON f.rowid = m.rowid
WHERE messages_fts MATCH ?
ORDER BY m.received_at DESC
LIMIT ?
```

The `case:` prefix is parsed but ignored (document as deprecated).

### `SqliteIndexRepository.search_contacts`

Rewrite as:

```sql
SELECT c.id FROM contacts c
JOIN contacts_fts f ON f.rowid = c.id
WHERE contacts_fts MATCH ?
ORDER BY c.message_count DESC, c.last_seen DESC
LIMIT ?
```

with `? = _fts5_query(prefix_arg, prefix=True)` — trailing `*` gives
the "as you type" prefix-match behavior the autocomplete needs.

### Tests

- Existing search tests in `tests/test_search_parser.py` keep
  working (parser semantics unchanged).
- Add new tests covering:
  - `maria` matches stored `María` (case + diacritic folding).
  - `garcia` matches stored `Juan.Garcia@example.com`.
  - Prefix contact search: `mar` returns `María`, `Mariano`.
  - Word-exact default: `mar` does **not** return `María` without
    the `*` (enforces the documented contract).
  - Field scoping: `subject:foo` doesn't match when `foo` appears
    only in `sender`.

## CLI changes

- [cli.py:1059-1062](../src/pony/cli.py#L1059-L1062) (`run_message_get`): delete the
  `Preview:` block. `pony message get` becomes metadata-only; users
  who want body content call `pony message body` (already exists,
  reads from mirror). Update the command's docstring.

## MCP changes

- [mcp_server.py:58](../src/pony/mcp_server.py#L58) (`_msg_to_dict`): remove the
  `body_preview` field from the dict. `search_messages`, `list_messages`,
  and `get_message` tools return pure metadata. Callers who want the
  body invoke `get_message_body` (already a registered tool at
  [mcp_server.py:249](../src/pony/mcp_server.py#L249)).
- Update the tool docstrings so LLM callers know where to get body
  content.

## Rollout

1. Land the schema bump + FTS5 tables + triggers + version gate.
2. Land the ingest-side cap change.
3. Land the search and search_contacts rewrites.
4. Land CLI/MCP cleanups.
5. User nukes `~/.local/share/pony/index.sqlite` (or platform
   equivalent) + mirror directories and re-syncs from IMAP.

All five steps in a single PR — they're tightly coupled and there's no
compatibility story.

## Decisions to confirm before implementation

1. **Rename `body_preview` → `body_text`?** The column no longer holds
   a short preview. Either rename now (tiny extra churn, cleaner name)
   or leave for a follow-up.
2. **256 KB body cap — acceptable?** Essentially invisible for normal
   mail; protects against log-pasted-into-body outliers.
3. **`remove_diacritics=2` — confirm for ñ.** Means `año` and `ano`
   match each other. Standard mail-search behavior but not everyone
   likes it.
