---
title: Configuration
---

# Configuration

Pony Express is configured through a single TOML file. Use `pony account add`
to print an annotated template you can paste into it, or `pony config edit`
to open the file in `$EDITOR` (it's created from the bundled sample if it
does not exist yet).

## File location

| Platform | Default path |
|---|---|
| Linux | `~/.config/pony/config.toml` |
| macOS | `~/.config/pony/config.toml` |
| Windows | `%APPDATA%\pony\config.toml` |

Override the directory with `PONY_CONFIG_DIR`, or pass `--config <path>` to
any command to use a specific file.

---

## Schema version

```toml
config_version = 2
```

`config_version` is **required** at the top of the file. Pony refuses to
load a config that omits it or carries a different value, rather than
silently migrating. The current supported version is `2`. The bump from v1
moved the SMTP keys out of the flat namespace and into a per-account
`[accounts.<name>.smtp]` subtable.

---

## Annotated example

The following config defines two IMAP accounts and two local accounts
(one read-only, one with SMTP).

```toml
config_version = 2

# -- Global options --------------------------------------------------------

# Show Unicode symbols in the TUI (attachment clip, flag star, etc.).
use_utf8 = true

# External editor launched with ctrl+x e in the composer.  If omitted or
# not executable, the built-in inline editor is used.
editor = "/usr/bin/nvim"

# Global default for Markdown composition mode.  Override per-account
# below; toggle per-message with ctrl+x m.
markdown_compose = false

# BBDB v3 file for Emacs interop.  Imported on each `pony sync` if its
# mtime advanced since the last import; exported to <data_dir>/contacts.bbdb.
# Supports ~, $VAR, and %VAR% expansion.
# bbdb_path = "~/.emacs.d/bbdb"

# Where the TUI's "open" / "save" attachment keys land.  Created on demand.
# Default: ~/Downloads.
# downloads_path = "~/mail-attachments"

# Textual theme for the TUI colour scheme.  Run `pony --list-themes` for
# names.  The --theme CLI flag overrides this for a single session.
# theme = "nord"


# -- First account: personal Gmail ----------------------------------------

[[accounts]]
name          = "Personal"
email_address = "jane@example.com"
imap_host     = "imap.gmail.com"
username      = "jane@example.com"
credentials_source = "encrypted"
archive_folder = "Archive"
markdown_compose = true
signature = """
Jane Smith
jane@example.com"""

[accounts.smtp]
host = "smtp.gmail.com"
# port and ssl default to 465 / true.

[accounts.mirror]
path   = "mirrors/personal"     # relative to Pony's data directory
format = "maildir"
trash_retention_days = 30

[accounts.folders]
exclude   = ["\\[Gmail\\]/Spam", "\\[Gmail\\]/Trash"]
read_only = ["\\[Gmail\\]/Sent Mail", "\\[Gmail\\]/All Mail"]


# -- Second account: corporate IMAP, STARTTLS on 587 ----------------------

[[accounts]]
name          = "Work"
email_address = "jane.smith@corp.example.com"
imap_host     = "mail.corp.example.com"
imap_port     = 993
imap_ssl      = true
username      = "jsmith"
credentials_source = "command"
password_command   = ["pass", "show", "corp/imap"]

sent_folder    = "Sent Items"
drafts_folder  = "Drafts"
archive_folder = "Archive"

[accounts.smtp]
host = "smtp.corp.example.com"
port = 587
ssl  = false      # STARTTLS

[accounts.mirror]
path   = "mirrors/work"
format = "mbox"
trash_retention_days = 14

[accounts.folders]
include   = ["INBOX", "Projects", "Projects/.*"]
exclude   = ["Junk E-mail", "Deleted Items"]
read_only = ["Sent Items"]


# -- Local read-only archive ---------------------------------------------

[[accounts]]
account_type  = "local"
name          = "Archive"
email_address = "jane@example.com"

[accounts.mirror]
path   = "mirrors/archive"
format = "maildir"
trash_retention_days = 90


# -- Local account with SMTP (read mail from disk, send via relay) -------

[[accounts]]
account_type       = "local"
name               = "Spool"
email_address      = "jane@example.com"
username           = "jane@example.com"
credentials_source = "plaintext"
password           = "change-me"

[accounts.smtp]
host = "smtp.example.com"

[accounts.mirror]
path   = "mirrors/spool"
format = "mbox"
trash_retention_days = 30
```

---

## Global options

| Key | Type | Default | Description |
|---|---|---|---|
| `config_version` | int | — | **Required.** Must be `2`. |
| `use_utf8` | bool | `false` | Enable Unicode symbols in the TUI. |
| `editor` | string | *(none)* | External editor for the composer (`ctrl+x e`). |
| `markdown_compose` | bool | `false` | Global default for Markdown composition mode. |
| `bbdb_path` | string | *(none)* | BBDB v3 file imported/exported on each sync. Supports `~`, `$VAR`, `%VAR%`. |
| `downloads_path` | string | `~/Downloads` | Directory for the TUI's open/save attachment keys. |
| `theme` | string | *(textual default)* | Textual theme name. `--theme` overrides per-session. |

---

## IMAP account fields

These apply when `account_type = "imap"` (the default if `account_type` is
omitted).

### Identity

| Key | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Unique account identifier shown in the TUI and CLI. |
| `email_address` | string | yes | Address used in the From: header. |
| `username` | string | yes | Login username for IMAP and SMTP (often the email address). |

### IMAP connection

| Key | Type | Default | Description |
|---|---|---|---|
| `imap_host` | string | — | IMAP server hostname. |
| `imap_port` | int | `993` (SSL) / `143` | IMAP port. |
| `imap_ssl` | bool | `true` | Use TLS for IMAP; `false` enables STARTTLS. |

### SMTP connection — `[accounts.smtp]` subtable

Required for IMAP accounts; optional for local accounts (omit to make a
local account read-only).

| Key | Type | Default | Description |
|---|---|---|---|
| `host` | string | — | SMTP server hostname. |
| `port` | int | `465` (SSL) / `587` | SMTP port. |
| `ssl`  | bool | `true` | Implicit TLS when `true`, STARTTLS when `false`. |

### Credentials

| Key | Type | Default | Description |
|---|---|---|---|
| `credentials_source` | string | — | One of `"plaintext"`, `"env"`, `"command"`, `"encrypted"`. |
| `password` | string | *(none)* | Password — only with `credentials_source = "plaintext"`. |
| `password_command` | list of strings | *(none)* | Command whose stdout is the password — only with `credentials_source = "command"`. |

See [Credential backends](#credential-backends) below.

### Composer

| Key | Type | Default | Description |
|---|---|---|---|
| `sent_folder` | string | *(auto)* | Exact folder name where sent messages are stored; auto-discovered by fuzzy match if omitted. |
| `drafts_folder` | string | *(auto)* | Exact folder name for saved drafts. |
| `archive_folder` | string | *(none)* | Target for the `A` key in the TUI. Omit to disable archiving. Must not be excluded or read-only. Created on the server on first archive if it doesn't exist. |
| `markdown_compose` | bool | inherits global | Default Markdown mode for this account. |
| `signature` | string | *(none)* | Text appended below the cursor on replies and forwards. |

---

## Local account fields

Local accounts point at a Maildir or mbox tree managed by something else
(`offlineimap`, `getmail`, `procmail`…). Pony never connects to an IMAP
server for them; sync is a no-op. Browsing, searching, harvesting contacts,
and folder creation all work normally.

| Key | Type | Required | Description |
|---|---|---|---|
| `account_type` | string | yes | Must be `"local"`. |
| `name` | string | yes | Unique account identifier. |
| `email_address` | string | yes | Address used in the From: header. |
| `sent_folder` / `drafts_folder` | string | no | Folder-name overrides. |
| `markdown_compose` | bool | no | Default Markdown mode. |
| `signature` | string | no | Signature text. |

A local account becomes send-capable by adding an `[accounts.smtp]`
subtable plus `username` / `credentials_source` / `password` /
`password_command` fields. Only send-capable accounts appear in the
composer's From: dropdown.

---

## Mirror configuration — `[accounts.mirror]`

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | — | Local mail directory. Env vars (`~`, `$VAR`, `%VAR%`) expanded. Relative paths resolve against Pony's data directory; use absolute for custom locations. |
| `format` | string | — | `"maildir"` or `"mbox"`. |
| `trash_retention_days` | int | `30` | Days to keep trashed messages before permanent deletion. |

**Maildir** stores one file per message in `cur/`, `new/`, and `tmp/`
subdirectories. Robust under concurrent access, plays well with
rsync-based backups.

**mbox** stores all messages in a single file per folder. Compact, but
does not tolerate concurrent writers. Pony maintains a TOC sidecar so
re-opens are fast. Prefer Maildir unless you have an existing mbox
archive to point at.

---

## Folder sync policy — `[accounts.folders]`

All three keys accept lists of **Python `re.fullmatch()` patterns**. A plain
name like `"INBOX"` matches exactly; `"Archive/.*"` matches any subfolder of
Archive; `".*"` matches everything. In TOML, backslashes must be doubled
(`"\\[Gmail\\]/.*"`).

| Key | Default | Description |
|---|---|---|
| `include` | `[]` (all) | If non-empty, only matching folders are synced. |
| `exclude` | `[]` | Folders never synced, even if matched by `include`. |
| `read_only` | `[]` | Synced server-to-local only; local flag changes and deletions are never pushed back. |

**Precedence:** `exclude` beats `include` beats `read_only`. A folder in
both `include` and `read_only` is synced read-only. A folder in both
`read_only` and `exclude` is not synced at all.

**Auto-include of read_only:** When `include` is non-empty, folders in
`read_only` are still synced unless also matched by `exclude` — so
`include = ["INBOX"]` will still pull `Sent Mail` if it's in `read_only`.

### Common patterns

```toml
# Sync everything except Spam and Trash
[accounts.folders]
exclude = ["Spam", "Trash"]

# Gmail: sync INBOX and Archive; treat Sent/All Mail as read-only
[accounts.folders]
exclude   = ["\\[Gmail\\]/Spam", "\\[Gmail\\]/Trash"]
read_only = ["\\[Gmail\\]/Sent Mail", "\\[Gmail\\]/All Mail"]

# Sync only a specific project folder tree
[accounts.folders]
include = ["INBOX", "Projects", "Projects/.*"]
```

---

## Credential backends

### `plaintext`

Password stored as a string in `config.toml`. Convenient for testing;
leaves the password readable on disk.

```toml
credentials_source = "plaintext"
password           = "s3cret"
```

### `env`

Password read from an environment variable at runtime. The variable name
is derived from the account `name`: uppercased, spaces replaced with
underscores, prefixed with `PONY_PASSWORD_`.

```toml
credentials_source = "env"
```

For an account named `"Personal"`, set `PONY_PASSWORD_PERSONAL`. For
`"Work Email"`, set `PONY_PASSWORD_WORK_EMAIL`.

### `command`

An external command is run and its stdout (trailing whitespace stripped)
is used as the password. The command is executed directly — no shell
interpolation.

```toml
credentials_source = "command"
password_command   = ["pass", "show", "mail/personal"]
```

Compatible with [pass](https://www.passwordstore.org/), the 1Password CLI
(`op read`), macOS Keychain via `security find-generic-password`, and
similar tools.

### `encrypted`

Password is encrypted and stored as a blob in the SQLite index. The
encryption key is derived from machine-specific information (OS username
and machine ID), so the blob is usable only on the machine that created
it.

- **Windows:** DPAPI (`CryptProtectData`).
- **Linux/macOS:** PBKDF2-HMAC-SHA256 key derivation with a SHAKE-256
  keystream.

```toml
credentials_source = "encrypted"
```

The first time Pony tries to connect it prompts interactively:

```
Password for Personal:
```

The result is stored and reused. To re-prompt (after a password change):

```
pony account set-password Personal
```
