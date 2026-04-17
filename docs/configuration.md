---
title: Configuration
---

# Configuration

Pony Express is configured through a single TOML file. The file is created
automatically on first run if it does not exist. Use `pony account add` to
print an annotated template you can paste into it.

## File location

| Platform | Default path |
|---|---|
| Linux | `~/.config/pony/config.toml` |
| macOS | `~/.config/pony/config.toml` |
| Windows | `%APPDATA%\pony\config.toml` |

Override the directory with `PONY_CONFIG_DIR`, or pass `--config <path>` to
any command to use a specific file.

---

## Annotated example

The following config defines two IMAP accounts and one local read-only account.
All credential values are illustrative.

```toml
# -- Global options --------------------------------------------------------

# Show Unicode symbols in the TUI (attachment clip, flag star, etc.).
# Set to false for terminals that render Unicode unreliably.
use_utf8 = true

# Path to an external editor launched with ctrl+x e in the composer.
# Must be an executable on PATH or an absolute path.  If omitted or not
# found, the built-in inline editor is used.
editor = "/usr/bin/nvim"

# Global default for Markdown composition mode.  When true, every new message
# opens in Markdown mode.  Override per-account with markdown_compose below.
# Toggle per-message with ctrl+x m inside the composer.
markdown_compose = false

# Path to a BBDB v3 file for automatic contact sync.
# On each `pony sync`, Pony imports from this file if it has changed since
# the last import, then exports to <data_dir>/contacts.bbdb.
# Supports ~, $VAR, and %VAR% expansion.
# bbdb_path = "~/.emacs.d/bbdb"


# -- First account: personal Gmail ----------------------------------------

[[accounts]]
name          = "Personal"
email_address = "jane@example.com"
imap_host     = "imap.gmail.com"
smtp_host     = "smtp.gmail.com"
username      = "jane@example.com"

# Use an app password generated in your Google account security settings.
# Store it as an OS-encrypted blob so it never appears in plain text on disk.
credentials_source = "encrypted"

# Signature appended below the cursor when replying or forwarding.
signature = """
Jane Smith
jane@example.com"""

# Compose new messages in Markdown by default for this account.
markdown_compose = true

[accounts.mirror]
path   = "mirrors/personal"   # relative to Pony's data directory
format = "maildir"
trash_retention_days = 30

# Folder sync policy:
#   include   - if non-empty, only these folders are synced
#   exclude   - never synced, even if matched by include
#   read_only - synced server->local only; local changes are never pushed back
# All values are Python re.fullmatch() patterns.
[accounts.folders]
exclude   = ["\\[Gmail\\]/Spam", "\\[Gmail\\]/Trash"]
read_only = ["\\[Gmail\\]/Sent Mail", "\\[Gmail\\]/All Mail"]


# -- Second account: corporate IMAP ---------------------------------------

[[accounts]]
name          = "Work"
email_address = "jane.smith@corp.example.com"
imap_host     = "mail.corp.example.com"
imap_port     = 993
imap_ssl      = true
smtp_host     = "smtp.corp.example.com"
smtp_port     = 587
smtp_ssl      = false          # STARTTLS on port 587
username      = "jsmith"

# Run an external command and read the password from its stdout.
# Useful with pass, 1Password CLI, macOS Keychain, etc.
credentials_source = "command"
password_command   = ["pass", "show", "corp/imap"]

sent_folder   = "Sent Items"   # override auto-discovery
drafts_folder = "Drafts"

[accounts.mirror]
path   = "mirrors/work"
format = "mbox"
trash_retention_days = 14

[accounts.folders]
include   = ["INBOX", "Projects", "Projects/.*"]
exclude   = ["Junk E-mail", "Deleted Items"]
read_only = ["Sent Items"]


# -- Local account: read-only archive -------------------------------------

[[accounts]]
account_type  = "local"
name          = "Archive"
email_address = "jane@example.com"

[accounts.mirror]
path   = "mirrors/archive"
format = "maildir"
trash_retention_days = 90
```

---

## Global options

| Key | Type | Default | Description |
|---|---|---|---|
| `use_utf8` | bool | `false` | Enable Unicode symbols in the TUI |
| `editor` | string | *(none)* | Path to external editor for the composer |
| `markdown_compose` | bool | `false` | Global default for Markdown composition mode |
| `bbdb_path` | string | *(none)* | Path to a BBDB v3 file for automatic contact sync on `pony sync`. Supports `~`, `$VAR`, and `%VAR%` expansion |

---

## IMAP account fields

These fields apply to accounts with `account_type = "imap"` (the default when
`account_type` is omitted).

### Identity

| Key | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Unique account identifier used in the TUI and CLI |
| `email_address` | string | yes | Address shown in From: when composing |
| `username` | string | yes | Login username for IMAP and SMTP (often the email address) |

### IMAP connection

| Key | Type | Default | Description |
|---|---|---|---|
| `imap_host` | string | -- | IMAP server hostname |
| `imap_port` | int | `993` (SSL) / `143` | IMAP port |
| `imap_ssl` | bool | `true` | Use TLS for IMAP; `false` enables STARTTLS |

### SMTP connection

| Key | Type | Default | Description |
|---|---|---|---|
| `smtp_host` | string | -- | SMTP server hostname |
| `smtp_port` | int | `465` (SSL) / `587` | SMTP port |
| `smtp_ssl` | bool | `true` | Use TLS for SMTP; `false` enables STARTTLS on the given port |

### Credentials

| Key | Type | Default | Description |
|---|---|---|---|
| `credentials_source` | string | -- | One of `"plaintext"`, `"env"`, `"command"`, `"encrypted"` |
| `password` | string | *(none)* | Password in plain text -- only with `credentials_source = "plaintext"` |
| `password_command` | list of strings | *(none)* | Command whose stdout is the password -- only with `credentials_source = "command"` |

See [Credential backends](#credential-backends) below for details.

### Composer

| Key | Type | Default | Description |
|---|---|---|---|
| `sent_folder` | string | *(auto)* | Exact folder name where sent messages are saved; auto-discovered by fuzzy match if omitted |
| `drafts_folder` | string | *(auto)* | Exact folder name for saved drafts; auto-discovered if omitted |
| `markdown_compose` | bool | `false` | Default Markdown mode for this account; overrides the global setting |
| `signature` | string | *(none)* | Text appended below the cursor in replies and forwards |

---

## Local account fields

Local accounts point at a Maildir or mbox tree managed by an external tool
(offlineimap, getmail, procmail, ...). Pony never connects to any server for
them; sync is skipped entirely. Browsing, searching, and composing all work
normally, but sending requires picking an IMAP account in the From: field.

| Key | Type | Required | Description |
|---|---|---|---|
| `account_type` | string | yes | Must be `"local"` |
| `name` | string | yes | Unique account identifier |
| `email_address` | string | yes | Address shown in From: when composing |
| `sent_folder` | string | *(none)* | Override sent-folder name |
| `drafts_folder` | string | *(none)* | Override drafts-folder name |
| `markdown_compose` | bool | `false` | Default Markdown mode |
| `signature` | string | *(none)* | Signature text |

---

## Mirror configuration (`[accounts.mirror]`)

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | -- | Directory for local mail storage. Env vars (`~`, `$VAR`, `%VAR%`) expanded. Relative paths resolve against Pony's data directory; use absolute for custom locations |
| `format` | string | -- | `"maildir"` or `"mbox"` |
| `trash_retention_days` | int | `30` | Days to keep trashed messages before permanent deletion |

**Maildir** stores one file per message in `cur/`, `new/`, and `tmp/`
subdirectories. It is robust under concurrent access and works well with
rsync-based backups.

**mbox** stores all messages in a single file per folder. It is compact but
does not tolerate concurrent writers. Prefer Maildir unless you have an
existing mbox archive to point at.

---

## Folder sync policy (`[accounts.folders]`)

All three keys accept lists of **Python `re.fullmatch()` patterns**. A plain
name like `"INBOX"` matches exactly; `"Archive/.*"` matches any subfolder of
Archive; `".*"` matches everything. In TOML, backslashes must be doubled
(`"\\[Gmail\\]/.*"`).

| Key | Default | Description |
|---|---|---|
| `include` | `[]` (all) | If non-empty, only folders matching a pattern are synced |
| `exclude` | `[]` | Folders never synced, even if matched by `include` |
| `read_only` | `[]` | Synced server-to-local only; local flag changes and deletions are never pushed back |

**Precedence:** `exclude` beats `include` beats `read_only`. A folder in both
`include` and `read_only` is synced read-only. A folder in both `read_only`
and `exclude` is not synced at all.

**Auto-include of read_only:** When `include` is non-empty, folders in
`read_only` are automatically included unless also matched by `exclude`. This
means you can set `include = ["INBOX"]` and still have Sent synced read-only
without listing it in `include`.

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

The password is stored as a string in `config.toml`. Convenient for testing
but leaves the password readable on disk.

```toml
credentials_source = "plaintext"
password           = "s3cret"
```

### `env`

The password is read from an environment variable at runtime. The variable
name is derived from the account `name`: uppercased with spaces replaced by
underscores, prefixed with `PONY_PASSWORD_`.

```toml
credentials_source = "env"
```

For an account named `"Personal"`, set `PONY_PASSWORD_PERSONAL` before
running Pony. For `"Work Email"`, set `PONY_PASSWORD_WORK_EMAIL`.

### `command`

An external command is run and its stdout (stripped of trailing whitespace) is
used as the password. The command is passed directly to the OS -- no shell
interpolation occurs.

```toml
credentials_source = "command"
password_command   = ["pass", "show", "mail/personal"]
```

Compatible with [pass](https://www.passwordstore.org/), the 1Password CLI
(`op read`), macOS Keychain via `security find-generic-password`, and similar
tools.

### `encrypted`

The password is encrypted and stored as a blob in the SQLite index database.
The encryption key is derived from machine-specific information (OS username
and machine ID), so the blob is usable only on the machine where it was
created.

On Windows the encryption uses **DPAPI** (`CryptProtectData`). On
Linux/macOS it uses **PBKDF2-HMAC-SHA256** key derivation with a
SHAKE-256 keystream cipher.

```toml
credentials_source = "encrypted"
```

The first time Pony tries to connect it prompts interactively:

```
Password for Personal:
```

The result is stored and reused for all subsequent runs. To re-prompt (e.g.
after a password change), run:

```
pony account set-password Personal
```
