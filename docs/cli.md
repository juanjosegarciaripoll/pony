---
title: CLI Reference
---

# CLI Reference

All commands are invoked as `pony <command>` (or `uv run pony <command>`).

## Global flags

These flags can be used with any command.

| Flag | Description |
|---|---|
| `--config <path>` | Use a specific config file instead of the default location |
| `--debug` | Enable verbose debug logging to stderr |

---

## `pony tui`

Launch the interactive terminal UI.

```
pony tui [account]
```

| Argument | Description |
|---|---|
| `account` | Optional: focus the given account on startup |

```
pony tui
pony tui Personal
```

See the [TUI](tui.md) page for full keyboard reference.

---

## `pony sync`

Synchronise all (or one) IMAP accounts with their remote servers. Pony shows a
summary of planned operations and asks for confirmation before making any
changes. Progress bars track scanning and execution.

```
pony sync [account] [--yes]
```

| Argument | Description |
|---|---|
| `account` | Optional: only sync this account |
| `--yes` | Skip the confirmation prompt and execute immediately |

```
pony sync
pony sync Work --yes
```

### Example output

```
Sync plan for Personal
  INBOX        : 3 new, 0 deleted, 1 flag update
  Archive      : 0 new, 0 deleted, 0 flag updates
  Sent Mail    : 2 new (read-only)

Proceed? [y/N] y

Personal: 5 new message(s), 1 flag update(s) from server, 0 flag push(es) to server
```

Local accounts are skipped silently.

---

## `pony compose`

Open the composer directly, bypassing the TUI.

```
pony compose [--account name] [--to addr] [--cc addr] [--bcc addr]
             [--subject text] [--body text] [--markdown | --no-markdown]
```

| Flag | Description |
|---|---|
| `--account` | Account to compose from (default: first account in config) |
| `--to` | Pre-fill the To field |
| `--cc` | Pre-fill the Cc field |
| `--bcc` | Pre-fill the Bcc field |
| `--subject` | Pre-fill the Subject line |
| `--body` | Pre-fill the message body |
| `--markdown` | Enable Markdown mode (overrides account default) |
| `--no-markdown` | Disable Markdown mode (overrides account default) |

```
pony compose --to alice@example.com --subject "Hello"
pony compose --account Work --markdown
```

See the [Composer](composer.md) page for full usage.

---

## `pony search`

Search the local index and print matching messages.

```
pony search [query]
```

If `query` is omitted, Pony prompts for one interactively.

### Query syntax

| Token | Matches |
|---|---|
| `word` | Body text containing *word* |
| `from:alice` | From address containing *alice* |
| `to:bob` | To address containing *bob* |
| `cc:carol` | Cc address containing *carol* |
| `subject:hello` | Subject containing *hello* |
| `body:text` | Body containing *text* (explicit) |
| `"quoted phrase"` | Exact phrase (any field prefix applies) |
| `case:yes` | Switch to case-sensitive matching |

Multiple tokens for the same field are AND-joined. Tokens for different fields
are also AND-joined.

```
pony search "from:alice subject:project"
pony search "quarterly report case:yes"
```

---

## `pony doctor`

Inspect the local setup and report any problems. Checks include Python version,
config parsing, index DB accessibility, per-account mirror path existence and
writability, mirror integrity (orphan files, stale index rows), and optional
dependency availability.

```
pony doctor
```

### Example output

```
Pony Express -- diagnostics
----------------------------
[OK   ] Python 3.13.2
[OK   ] Config loaded  (2 IMAP accounts, 1 local)
[OK   ] Index DB       ~/.local/share/pony/index.sqlite3
[OK   ] Personal       mirror exists and is writable
[OK   ] Work           mirror exists and is writable
[OK   ] Archive        mirror exists and is writable
[OK   ] markdown-it-py 3.0.0

Paths
  Config : ~/.config/pony/config.toml
  Data   : ~/.local/share/pony/
  State  : ~/.local/state/pony/
  Logs   : ~/.local/state/pony/logs/
```

Exit code is 0 when all checks pass, 1 if any check reports ERROR or WARN.

---

## `pony server-summary`

Connect to IMAP and list remote folders with message counts and the date of
the most recent message.

```
pony server-summary [account]
```

| Argument | Description |
|---|---|
| `account` | Optional: only show this account |

```
pony server-summary
pony server-summary Personal
```

### Example output

```
Personal -- imap.gmail.com
  INBOX            142 messages   last: 2026-04-11
  [Gmail]/Sent Mail  89 messages  last: 2026-04-10
  [Gmail]/All Mail  847 messages  last: 2026-04-11
```

---

## `pony local-summary`

Show the state of local mirrors, the index DB, and config without connecting
to any server.

```
pony local-summary [account]
```

| Argument | Description |
|---|---|
| `account` | Optional: only show this account |

```
pony local-summary
```

---

## `pony reset`

Delete the index database and all local mirror data, giving you a clean slate
for a full re-sync. **This is destructive.** The command asks for confirmation
unless `--yes` is given. Works even without a valid config file.

```
pony reset [--yes]
```

| Flag | Description |
|---|---|
| `--yes` | Skip the confirmation prompt |

!!! warning
    All locally mirrored mail and the SQLite index are deleted. If the messages
    still exist on the server they will be re-fetched on the next sync. If you
    have unsent drafts or messages deleted from the server, they will be lost.

---

## `pony config edit`

Open the config file in your editor (`$EDITOR`, `$VISUAL`, or the platform
default). If no config file exists yet, one is created from the sample config.

```
pony config edit
pony config
```

---

## `pony account add`

Print an annotated TOML template for a new account. If stdin is a terminal,
prompts interactively for account details (email, IMAP/SMTP servers,
credentials) and appends the account to `config.toml`. Server hostnames are
guessed from the email domain.

```
pony account add
```

!!! tip "First-run detection"
    If you run `pony tui` or `pony sync` without a config file, Pony offers
    to launch this wizard automatically.

---

## `pony account set-password`

Re-prompt for a password and store the new encrypted blob in the SQLite index.
Use this after a password change or to rotate credentials. Only applies to
accounts with `credentials_source = "encrypted"`.

```
pony account set-password <name>
```

```
pony account set-password Personal
# Password for Personal:
```

---

## `pony contacts`

Open the interactive contacts browser. This is a standalone TUI for browsing,
searching, editing, merging, and deleting contacts.

```
pony contacts
pony contacts browse
```

See the [Contacts](contacts.md) page for keybindings and editor details.

---

## `pony contacts search`

Search the contacts store by name, alias, or email address.

```
pony contacts search <prefix> [--limit N]
```

| Argument | Description |
|---|---|
| `prefix` | Name, alias, or email fragment to search for |
| `--limit` | Maximum number of results (default: 20) |

```
pony contacts search alice
pony contacts search "@example.com" --limit 50
```

### Example output

```
  Alice Smith  <alice@example.com>  (seen 47x)
  Alice Jones  <alice.jones@corp.example.com, aj@home.net>  (seen 12x)  aka AJ
```

---

## `pony contacts show`

Display the full record for a contact identified by email address.

```
pony contacts show <email>
```

```
pony contacts show alice@example.com
```

---

## `pony contacts export`

Write the contacts database to a BBDB v3 file.

```
pony contacts export [path]
```

If `path` is omitted, writes to `<data_dir>/contacts.bbdb`.

---

## `pony contacts import`

Read a BBDB v3 file into the contacts database with smart merge (match by
email, combine emails/aliases, prefer richer name/org/notes).

```
pony contacts import [path]
```

If `path` is omitted, reads from the `bbdb_path` config setting.

---

## `pony fixture-ingest`

Ingest a small set of deterministic fixture messages into the index. Intended
for development and testing, not regular use.

```
pony fixture-ingest
```

See the [Contacts](contacts.md) page for more details.
