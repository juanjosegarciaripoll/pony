---
title: Composer
---

# Composer

The composer opens as a full-screen overlay inside the TUI or as a standalone
window when launched from the command line.

## Opening the composer

| Method | How |
|---|---|
| New message | Press ++c++ in the TUI |
| Reply | Press ++r++ on a message in the TUI |
| Forward | Press ++f++ on a message in the TUI |
| CLI | `pony compose [--to ...] [--subject ...] ...` |

---

## Layout

```
 Pony Express -- Compose
+----------------------------------------------------------------------------+
| From:    [ Personal <jane@example.com>                                v ]  |
| To:      alice@example.com                                                 |
| Cc:                                                                        |
| Bcc:                                                                       |
| Subject: Re: Project update                                                |
+--------------------------- Body -- Markdown OFF ---------------------------+
| Hi Alice,                                                                  |
|                                                                            |
| Thanks for the heads-up. I'll get the room booked for Tuesday.             |
|                                                                            |
| Best,                                                                      |
| Jane                                                                       |
|                                                                            |
| ------------------- Original message --------------------                  |
| > Hi Jane,                                                                 |
| >                                                                          |
| > Thanks for the update! The timeline looks good.                          |
| > I'll book the conference room for Tuesday.                               |
|                                                                            |
+----------------------------------------------------------------------------+
| Attachments: (none)                                                        |
+----------------------------------------------------------------------------+
 ctrl+s Send  Esc Cancel  ctrl+x... (s=send  a=attach  e=editor  m=markdown)
```

### Fields

| Field | Description |
|---|---|
| **From** | Dropdown showing all configured accounts. Defaults to the account whose folder was selected, or the first account. |
| **To / Cc / Bcc** | Address fields. Multiple addresses are separated by commas. Supports tab-completion from the contacts store (see [Contacts](contacts.md)). |
| **Subject** | Subject line. Prefilled with `Re:` or `Fwd:` when replying or forwarding. |
| **Body** | Multi-line text editor. Quoted content is included at the bottom when replying or forwarding. |
| **Attachments** | Listed at the bottom; managed with ++ctrl+x++ then ++a++. |

!!! note "Screen-specific bindings"
    The compose screen footer shows only compose-relevant bindings. Mail-reader
    keys like `g` (sync) or `d` (trash) do not fire from the composer.

---

## Keyboard shortcuts

### Direct shortcuts

| Key | Action |
|---|---|
| ++ctrl+s++ | Send the message immediately |
| ++escape++ | Cancel (prompts to save as draft if body is non-empty) |

### `ctrl+x` prefix chord

Press ++ctrl+x++ to enter prefix mode. A brief notification appears:

```
ctrl+x -- s=send  a=attach  e=editor  m=markdown  c=cancel
```

Then press the second key:

| Second key | Action |
|---|---|
| ++s++ | Send |
| ++a++ | Add attachment |
| ++e++ | Open body in external editor |
| ++m++ | Toggle Markdown mode |
| ++c++ | Cancel |

---

## Reply quoting

Pony uses **top-posting**: the cursor is placed above the quoted original. All
existing quote levels are preserved on reply-to-reply, so a thread stays
readable without manual cleanup.

```
Hi Alice,

I'll book the room for Tuesday.

------------- Original message -------------
> Hi Jane,
>
> Thanks for the update! The timeline looks good.
>
> ------------- Original message -------------
> > Could you review the Q1 plan?
```

---

## Markdown mode

When Markdown mode is enabled, the body is treated as CommonMark source. The
sent message becomes `multipart/alternative`:

- `text/plain` part -- the raw Markdown source as typed (fully readable in
  plain-text clients)
- `text/html` part -- the rendered HTML (shown by clients that support it)

The border title of the body area reflects the current state:

```
 Body -- Markdown ON
```

### Enabling Markdown mode

| Method | How |
|---|---|
| Per-message toggle | ++ctrl+x++ then ++m++ inside the composer |
| Account default | `markdown_compose = true` in the account config |
| Global default | `markdown_compose = true` in the top-level config |
| CLI flag | `pony compose --markdown` / `pony compose --no-markdown` |

### Example: Markdown message

With Markdown ON, you can write:

```markdown
Hi Alice,

Here is the **updated timeline**:

| Milestone | Date |
|---|---|
| Design review | Apr 14 |
| Implementation | Apr 21 |
| QA sign-off | Apr 28 |

Let me know if anything looks off.
```

Recipients using a mail client that renders HTML will see a formatted table.
Recipients in a plain-text client see the raw Markdown, which is legible as-is.

---

## External editor

If `editor` is set in the config, press ++ctrl+x++ then ++e++ to open the
message body in that editor. Pony suspends the TUI, waits for the editor to
exit, then resumes with the updated content.

```toml
editor = "/usr/bin/nvim"
```

If `editor` is not set or the executable is not found on `PATH`, the shortcut
does nothing and a notification is shown.

---

## Attachments

Press ++ctrl+x++ then ++a++ to open the attachment picker:

```
+----------------- Add attachment -------------------+
| Filter: report                                     |
+----------------------------------------------------+
| > ~/Documents/                                     |
|    q1-report.pdf                                   |
|    q1-report-draft.pdf                             |
| > ~/Downloads/                                     |
|    quarterly-report-final.pdf                      |
+----------------------------------------------------+
```

The picker is a `DirectoryTree` with a typeahead filter (0.8 s debounce).
Navigate to a file and press ++enter++ to attach it. Repeat ++ctrl+x++ then
++a++ to add more files.

Attached files are shown at the bottom of the composer:

```
Attachments:
  1. q1-report.pdf  (342 KB)
```

---

## Sending

Pony sends immediately over SMTP when you press ++ctrl+s++ (or ++ctrl+x++
then ++s++). The account's `smtp_host`, `smtp_port`, and `smtp_ssl` settings
are used. After a successful send, the message is saved to the sent folder
(auto-discovered by fuzzy name match, or the explicit `sent_folder` config
value).

If sending fails, a notification appears with the error. You can then choose
to save the message as a draft.

---

## Drafts

There is no autosave. If you press ++escape++ (or ++ctrl+x++ then ++c++) with
a non-empty body, Pony asks whether to save a draft:

```
+-----------------------------+
| Save as draft?              |
|                             |
|   [Save]      [Discard]    |
+-----------------------------+
```

Drafts are saved to the drafts folder (auto-discovered by fuzzy name match, or
the explicit `drafts_folder` config value) and appear in the message list like
any other message.
