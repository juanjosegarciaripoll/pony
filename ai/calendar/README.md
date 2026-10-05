# Calendar reference

The calendar subsystem's own specifications, carried over when it became
part of Pony Express. They describe behaviour that is still exactly as
written — CalDAV reconciliation, recurrence expansion, the MCP tools —
and are the reference for changes to `src/chronos`.

| File | Purpose |
|---|---|
| `SPECIFICATIONS.md` | Scope: what the calendar does and deliberately does not do |
| `SYNCHRONIZATION.md` | The three CalDAV sync paths, conflict rules, pending pushes |
| `RECURRENCE.md` | RRULE expansion, overrides, the occurrence and alarm caches |
| `MCP.md` | The calendar's MCP tools and transport |

Two things these files predate:

- **There is one architecture document**, `docs/architecture.md`, and it
  is published. The calendar's old `ARCHITECTURE.md` is not carried over;
  its content lives there now, including the section on how mail and
  calendar run as one program.
- **Pony's rules govern**, so the calendar's old `AGENTS.md` and
  `CONVENTIONS.md` are not carried over either: `ai/AGENTS.md` and
  `ai/CONVENTIONS.md` at the root apply to both halves. Where these files
  say `chronos ...` as a command, the command is now `pony calendar ...`.

The load-bearing invariant is unchanged and worth repeating here:
`href IS NULL` on a component row means it should exist on the server but
has not been confirmed there yet. That is what drives local pending
pushes — the calendar's equivalent of mail's `uid IS NULL`.
