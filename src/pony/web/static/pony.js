// Keyboard dispatch for the browser front end.
//
// The keymap is not written here. It is fetched from /keymap.json, which
// serves `pony.keymap` and `chronos.keymap` — the same tables the
// Textual front end builds its bindings from — so a key cannot mean one
// thing in the terminal and another here, and the help overlay cannot
// advertise a key that does not exist.
//
// Three kinds of action:
//   * client   — selection, navigation and overlays, done here
//   * server   — POST /action/<name>, then refresh and show the notices
//   * absent   — in the keymap but not possible in a browser (editing
//                the body in $EDITOR); says so rather than doing nothing
//
// No framework and no CDN: a tailnet has no reason to reach the public
// internet, and a front end that needs one stops working on a train.

"use strict";

const state = {
  keymaps: {},
  keymap: [],
  surface: "reader",
  marked: new Set(),
};

// ── Rows, selection and marks ──────────────────────────────────

function rows() {
  return Array.from(document.querySelectorAll("#rows .row"));
}

function selectedRow() {
  return document.querySelector("#rows .row[aria-selected='true']") || rows()[0];
}

function select(row) {
  if (!row) return;
  for (const other of rows()) other.setAttribute("aria-selected", "false");
  row.setAttribute("aria-selected", "true");
  row.scrollIntoView({ block: "nearest" });
}

function moveSelection(delta) {
  const all = rows();
  if (all.length === 0) return;
  const current = all.indexOf(selectedRow());
  const next = Math.min(all.length - 1, Math.max(0, current + delta));
  select(all[next]);
}

function toggleMark(row) {
  if (!row) return;
  const id = row.dataset.id;
  if (state.marked.has(id)) {
    state.marked.delete(id);
    row.classList.remove("marked");
  } else {
    state.marked.add(id);
    row.classList.add("marked");
  }
}

// What an action applies to: every marked row, or the selected one.
function targets() {
  if (state.marked.size > 0) return Array.from(state.marked);
  const row = selectedRow();
  return row ? [row.dataset.id] : [];
}

function openSelected() {
  const row = selectedRow();
  const link = row && row.querySelector("a[href]");
  if (link) window.location.assign(link.href);
}

// ── Notices ────────────────────────────────────────────────────

function notify(notices) {
  const box = document.getElementById("notices");
  if (!box) return;
  box.replaceChildren(
    ...notices.map((notice) => {
      const line = document.createElement("p");
      line.className = `notice ${notice.severity || "information"}`;
      line.textContent = notice.text;
      return line;
    }),
  );
  if (notices.length > 0) {
    window.setTimeout(() => box.replaceChildren(), 8000);
  }
}

function say(text, severity = "information") {
  notify([{ text, severity }]);
}

// ── Where we are ───────────────────────────────────────────────

function here() {
  return document.body.dataset;
}

function mailParams() {
  const data = here();
  const params = new URLSearchParams();
  if (data.account) params.set("account", data.account);
  if (data.folder) params.set("folder", data.folder);
  if (data.query) params.set("q", data.query);
  return params;
}

function go(path) {
  window.location.assign(path);
}

function composeUrl(mode) {
  const data = here();
  const params = new URLSearchParams({ mode });
  if (data.account) params.set("account", data.account);
  if (data.folder) params.set("folder", data.folder);
  const id = data.message || (selectedRow() && selectedRow().dataset.id);
  if (id) params.set("id", id);
  return `/compose?${params}`;
}

function messageParams() {
  const data = here();
  const id = data.message || (selectedRow() && selectedRow().dataset.id);
  if (!id) return null;
  const params = new URLSearchParams();
  params.set("account", data.account || "");
  params.set("folder", data.folder || "");
  params.set("id", id);
  return params;
}

// ── Prompts ────────────────────────────────────────────────────

// One dialog for everything that needs a word before it can act: a
// folder to move to, a query to search for, a date to go to.
function ask(question, { value = "", list = null, placeholder = "" } = {}) {
  const dialog = document.getElementById("prompt");
  if (!dialog) return Promise.resolve(null);
  const form = document.createElement("form");
  const label = document.createElement("label");
  label.textContent = question;
  const input = document.createElement("input");
  input.type = "text";
  input.value = value;
  input.placeholder = placeholder;
  input.autocomplete = "off";
  if (list) input.setAttribute("list", list);
  const buttons = document.createElement("div");
  buttons.className = "invitation-answers";
  const ok = document.createElement("button");
  ok.type = "submit";
  ok.className = "primary";
  ok.textContent = "OK";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  buttons.append(ok, cancel);
  form.append(label, input, buttons);
  dialog.replaceChildren(form);

  return new Promise((resolve) => {
    const finish = (answer) => {
      dialog.close();
      dialog.replaceChildren();
      resolve(answer);
    };
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      finish(input.value.trim());
    });
    cancel.addEventListener("click", () => finish(null));
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      finish(null);
    });
    dialog.showModal();
    input.focus();
    input.select();
  });
}

// ── Server actions ─────────────────────────────────────────────

async function runServerAction(name, extra = {}) {
  const data = here();
  // `g` and ctrl+g mean "sync what I am looking at", and on the
  // calendar that is the calendar. The two tables spell the action the
  // same way, so the surface is what tells them apart.
  if (state.surface === "calendar" && (name === "sync" || name === "background_sync")) {
    name = "sync_calendar";
  }
  // URLSearchParams, not FormData: the server reads a urlencoded body
  // so it needs no multipart parser.
  const body = new URLSearchParams();
  if (data.account) body.append("account", data.account);
  if (data.folder) body.append("folder", data.folder);
  for (const id of targets()) body.append("id", id);
  for (const [key, value] of Object.entries(extra)) {
    if (value !== null && value !== undefined) body.append(key, value);
  }

  // A sync can take a while, so say it started rather than leaving the
  // page looking like nothing happened.
  const slow = name.startsWith("sync");
  if (slow) {
    say("Syncing…");
    document.body.dataset.busy = "1";
  }
  let outcome;
  try {
    const response = await fetch(`/action/${encodeURIComponent(name)}`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
    outcome = await response.json();
  } catch (error) {
    say(`The server did not answer: ${error}`, "error");
    return { changed: 0, notices: [] };
  } finally {
    if (slow) delete document.body.dataset.busy;
  }
  notify(outcome.notices || []);
  if (outcome.changed > 0) {
    state.marked.clear();
    await refresh();
  }
  return outcome;
}

// Only the list is replaced where there is one to replace, so the open
// message and everything's scroll position survive an action.
async function refresh() {
  const data = here();
  if (state.surface !== "reader" || !data.account || !data.folder) {
    window.location.reload();
    return;
  }
  const target = document.getElementById("rows");
  if (!target) {
    window.location.reload();
    return;
  }
  const response = await fetch(`/mail/rows?${mailParams()}`);
  const markup = await response.text();
  const parsed = new DOMParser().parseFromString(markup, "text/html");
  const fresh = parsed.getElementById("rows");
  if (fresh) {
    target.replaceWith(fresh);
    wireMarkTaps();
    select(selectedRow());
  } else {
    window.location.reload();
    return;
  }
  // The unread counts beside the folders are now out of date, and they
  // are a pane of their own, so they come back the same way.
  const tree = document.getElementById("folders");
  if (!tree) return;
  const folders = await fetch(`/folders?${mailParams()}`);
  const refreshed = new DOMParser()
    .parseFromString(await folders.text(), "text/html")
    .getElementById("folders");
  if (refreshed) tree.replaceWith(refreshed);
}

// ── The help overlay ───────────────────────────────────────────

function showHelp() {
  const dialog = document.getElementById("help");
  if (!dialog) return;
  if (dialog.open) {
    dialog.close();
    return;
  }
  const list = document.createElement("dl");
  for (const binding of state.keymap) {
    const key = document.createElement("dt");
    key.textContent = binding.key;
    if (!binding.available) key.classList.add("unavailable");
    const what = document.createElement("dd");
    what.textContent = binding.available
      ? binding.description
      : `${binding.description} (terminal only)`;
    list.append(key, what);
  }
  const heading = document.createElement("h2");
  heading.textContent = "Keys";
  dialog.replaceChildren(heading, list);
  dialog.showModal();
}

// ── Pane geometry ──────────────────────────────────────────────
//
// The same four keys the terminal resizes its panes with, kept per
// browser because it is that viewer's window they describe.

const PANES = "pony.panes";

function panes() {
  try {
    return JSON.parse(window.localStorage.getItem(PANES)) || {};
  } catch {
    return {};
  }
}

function setPanes(next) {
  const root = document.documentElement;
  if (next.folders) root.style.setProperty("--folders", `${next.folders}%`);
  if (next.list) root.style.setProperty("--list", `${next.list}fr`);
  try {
    window.localStorage.setItem(PANES, JSON.stringify(next));
  } catch {
    // A private window refuses to remember; the layout still works.
  }
}

function resizePane(key, delta, { min, max }) {
  const current = panes();
  const value = Math.min(max, Math.max(min, (current[key] || (key === "folders" ? 23 : 1)) + delta));
  setPanes({ ...current, [key]: Number(value.toFixed(2)) });
}

// ── Calendar navigation ────────────────────────────────────────

function calendarUrl(changes = {}) {
  const data = here();
  const params = new URLSearchParams();
  params.set("view", changes.view || data.view || "agenda");
  params.set("date", changes.date || data.date || "");
  if (changes.span || (params.get("view") === "grid" && data.span)) {
    params.set("span", changes.span || data.span);
  }
  if (changes.window) params.set("window", changes.window);
  if (data.query) params.set("q", data.query);
  return `/calendar?${params}`;
}

function shiftedDate(days) {
  const data = here();
  const base = data.date ? new Date(`${data.date}T12:00:00`) : new Date();
  base.setDate(base.getDate() + days);
  return base.toISOString().slice(0, 10);
}

function selectedEventUid() {
  const row = selectedRow();
  if (!row || !row.dataset.id) return null;
  // "account/calendar/uid", and a UID may itself contain slashes.
  const parts = row.dataset.id.split("/");
  return parts.length > 2 ? parts.slice(2).join("/") : null;
}

function submitForm() {
  const form = document.querySelector("form#compose-form, form#event-form, form#contact-form");
  if (form) form.requestSubmit();
}

// ── What each action does here ─────────────────────────────────

const clientActions = {
  // Lists
  cursor_down: () => moveSelection(1),
  cursor_up: () => moveSelection(-1),
  cursor_first: () => select(rows()[0]),
  cursor_last: () => select(rows()[rows().length - 1]),
  mark_down: () => {
    toggleMark(selectedRow());
    moveSelection(1);
  },
  mark_up: () => {
    toggleMark(selectedRow());
    moveSelection(-1);
  },

  // Everywhere
  show_help: showHelp,
  toggle_calendar: () => go(state.surface === "calendar" ? "/mail" : "/calendar"),
  browse_contacts: () => go("/contacts"),
  quit: () => say("Close the tab to quit."),

  // Mail
  search: async () => {
    const data = here();
    const query = await ask("Search", { value: data.query || "" });
    if (query === null) return;
    if (state.surface === "contacts") {
      go(`/contacts?${new URLSearchParams({ q: query })}`);
      return;
    }
    if (state.surface === "calendar") {
      go(`${calendarUrl()}&${new URLSearchParams({ q: query })}`);
      return;
    }
    const params = new URLSearchParams({
      account: data.account || "",
      folder: data.folder || "",
    });
    if (query) params.set("q", query);
    go(`/mail?${params}`);
  },
  goto_folder: async () => {
    const chosen = await ask("Go to folder (account/folder)", {
      list: "folder-names",
    });
    if (!chosen) return;
    const cut = chosen.indexOf("/");
    if (cut < 1) {
      say("Name it as account/folder.", "warning");
      return;
    }
    go(
      `/mail?${new URLSearchParams({
        account: chosen.slice(0, cut),
        folder: chosen.slice(cut + 1),
      })}`,
    );
  },
  compose_new: () => go(composeUrl("new")),
  compose_reply: () => go(composeUrl("reply")),
  compose_reply_all: () => go(composeUrl("reply_all")),
  compose_forward: () => go(composeUrl("forward")),
  edit_draft: () => go(composeUrl("draft")),
  open_browser: () => {
    const params = messageParams();
    if (!params) {
      say("Open a message first.", "warning");
      return;
    }
    window.open(`/message/body?${params}`, "_blank", "noopener");
  },
  print_pdf: () => {
    const params = messageParams();
    if (!params) {
      say("Open a message first.", "warning");
      return;
    }
    window.open(`/message/body?${params}`, "_blank", "noopener");
    say("Opened the printable version — print it from that tab.");
  },
  save_message: () => {
    const params = messageParams();
    if (!params) {
      say("Open a message first.", "warning");
      return;
    }
    go(`/message/raw?${params}`);
  },
  attachments_open: () => openAttachment(),
  attachments_save: () => openAttachment(),
  move: async () => {
    const target = await ask("Move to (account/folder)", { list: "folder-names" });
    if (!target) return;
    void runServerAction("move", { target });
  },
  copy: async () => {
    const target = await ask("Copy to (account/folder)", { list: "folder-names" });
    if (!target) return;
    void runServerAction("copy", { target });
  },
  new_folder: async () => {
    const name = await ask("New folder name");
    if (!name) return;
    void runServerAction("new_folder", { name });
  },
  respond_invitation: () => {
    const block = document.querySelector(".invitation-answers button");
    if (!block) {
      say("This message carries no invitation.");
      return;
    }
    block.focus();
  },

  // Pane geometry
  narrow_folders: () => resizePane("folders", -2, { min: 10, max: 50 }),
  widen_folders: () => resizePane("folders", 2, { min: 10, max: 50 }),
  shrink_list: () => resizePane("list", -0.2, { min: 0.4, max: 4 }),
  grow_list: () => resizePane("list", 0.2, { min: 0.4, max: 4 }),

  // Calendar
  view_agenda: () => go(calendarUrl({ view: "agenda" })),
  view_month: () => go(calendarUrl({ view: "month" })),
  agenda_window_day: () => go(calendarUrl({ view: "agenda", window: "day" })),
  agenda_window_week: () => go(calendarUrl({ view: "agenda", window: "week" })),
  agenda_window_month: () => go(calendarUrl({ view: "agenda", window: "month" })),
  today: () => go(calendarUrl({ date: new Date().toISOString().slice(0, 10) })),
  goto_date: async () => {
    const day = await ask("Go to date (YYYY-MM-DD)", { value: here().date || "" });
    if (!day) return;
    go(calendarUrl({ date: day }));
  },
  next_day: () => go(calendarUrl({ date: shiftedDate(1) })),
  prev_day: () => go(calendarUrl({ date: shiftedDate(-1) })),
  next_week: () => go(calendarUrl({ date: shiftedDate(7) })),
  prev_week: () => go(calendarUrl({ date: shiftedDate(-7) })),
  new_event: () => go(`/calendar/event?${new URLSearchParams({ date: here().date || "" })}`),
  edit_event: () => openSelected(),
  open_event: () => openSelected(),
  delete_event: () => {
    const uid = selectedEventUid();
    if (!uid) {
      say("Select an event first.", "warning");
      return;
    }
    void runServerAction("delete_event", { uid });
  },
  toggle_calendars: () => {
    const first = document.querySelector("#folders .tree .folder");
    if (first) first.focus();
  },
  sync_dialog: () => {
    void runServerAction("sync");
  },

  // Contacts
  new_contact: () => go("/contacts/edit"),
  edit: () => openSelected(),
  close: () => go("/mail"),

  // Forms
  save: submitForm,
  send: submitForm,
  delete: () => {
    const button = document.getElementById("delete-event");
    if (!button) {
      say("Nothing here to delete.", "warning");
      return;
    }
    button.click();
  },
  cancel: () => window.history.back(),
  add_attachment: () => {
    const picker = document.getElementById("attach");
    if (picker) picker.click();
  },
  toggle_markdown: () => {
    const box = document.querySelector("input[name='markdown']");
    if (!box) return;
    box.checked = !box.checked;
    say(`Markdown ${box.checked ? "on" : "off"}.`);
  },
};

function openAttachment() {
  const link = document.querySelector(".attachments a[href]");
  if (!link) {
    say("This message has no attachments.");
    return;
  }
  window.location.assign(link.href);
}

// ── Key dispatch ───────────────────────────────────────────────

// The same normalised spelling `keymap.web_key` produces, so the two
// sides compare without either guessing at the other's vocabulary.
function pressed(event) {
  const parts = [];
  if (event.ctrlKey) parts.push("ctrl");
  if (event.altKey) parts.push("alt");
  if (event.shiftKey && event.key.length > 1) parts.push("shift");
  parts.push(event.key);
  return parts.join("+");
}

function typing(target) {
  return (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement ||
    (target && target.isContentEditable)
  );
}

function onKeyDown(event) {
  if (event.metaKey) return;
  const inField = typing(event.target);
  const spelling = pressed(event);
  const binding = state.keymap.find((entry) => entry.key === spelling);
  // Never steal a plain key from something being typed into; a chord
  // (ctrl+s to send, Escape to cancel) is still ours.
  if (inField && !event.ctrlKey && !event.altKey && event.key !== "Escape") return;
  if (!binding) {
    if (!inField && event.key === "Enter") {
      event.preventDefault();
      openSelected();
    }
    return;
  }
  event.preventDefault();
  const name = binding.action;
  const client = clientActions[name] || clientActions[name.split("(")[0]];
  if (client) {
    client(name);
    return;
  }
  if (binding.available) {
    void runServerAction(name);
    return;
  }
  say(`${binding.description} is only in the terminal.`, "warning");
}

// `select_span(3)` carries its argument in the action name.
clientActions.select_span = (name) => {
  const days = Number((name.match(/\((\d+)\)/) || [])[1] || 1);
  go(calendarUrl(days === 1 ? { view: "day" } : { view: "grid", span: days }));
};

// ── Page wiring ────────────────────────────────────────────────

// Buttons that stand for a key, so a phone can do what the keyboard
// does: the reader's Archive and Trash, the contacts list's bulk
// operations. The name is the action; the dispatcher does the rest.
function wireActionButtons() {
  for (const button of document.querySelectorAll("[data-action]")) {
    button.addEventListener("click", () => {
      void runServerAction(button.dataset.action);
    });
  }
}

function wireMarkTaps() {
  for (const cell of document.querySelectorAll("#rows .row .mark")) {
    cell.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const row = cell.closest(".row");
      select(row);
      toggleMark(row);
    });
  }
}

function wireInvitation() {
  for (const button of document.querySelectorAll(".invitation-answer")) {
    button.addEventListener("click", () => {
      const chooser = document.getElementById("invitation-calendar");
      void runServerAction("respond_invitation", {
        answer: button.dataset.answer,
        calendar: chooser ? chooser.value : "",
      });
    });
  }
}

function wireEventDelete() {
  const button = document.getElementById("delete-event");
  if (!button) return;
  button.addEventListener("click", async () => {
    const outcome = await runServerAction("delete_event", {
      uid: button.dataset.uid,
    });
    if (outcome && outcome.changed > 0) go("/calendar");
  });
}

// Attachments go up as raw bytes, so the server needs no multipart
// parser: the form then carries a token rather than a path.
function wireAttachments() {
  const picker = document.getElementById("attach");
  const staged = document.getElementById("staged");
  if (!picker || !staged) return;
  picker.addEventListener("change", async () => {
    for (const file of Array.from(picker.files || [])) {
      const response = await fetch(
        `/compose/attachment?${new URLSearchParams({ name: file.name })}`,
        { method: "POST", body: await file.arrayBuffer() },
      );
      const result = await response.json();
      if (result.error) {
        say(result.error, "warning");
        continue;
      }
      const item = document.createElement("li");
      item.className = "staged";
      const token = document.createElement("input");
      token.type = "hidden";
      token.name = "attachment";
      token.value = result.token;
      const label = document.createElement("span");
      label.textContent = result.name;
      item.append(token, label);
      staged.append(item);
    }
    picker.value = "";
  });
}

function wireFolderToggle() {
  // On a phone the folder tree is a page of its own; the status line
  // doubles as the way back to it.
  const status = document.getElementById("status");
  if (!status) return;
  status.addEventListener("click", () => {
    document.body.classList.toggle("show-folders");
  });
}

async function start() {
  state.surface = document.body.dataset.surface || "reader";
  setPanes(panes());
  wireActionButtons();
  wireMarkTaps();
  wireInvitation();
  wireEventDelete();
  wireAttachments();
  wireFolderToggle();
  document.addEventListener("keydown", onKeyDown);
  // Clicking a row selects it, so the keyboard and the pointer agree
  // about what an action will act on.
  document.addEventListener("click", (event) => {
    const row = event.target.closest && event.target.closest(".row");
    if (row) select(row);
  });
  select(selectedRow());
  const response = await fetch("/keymap.json");
  state.keymaps = await response.json();
  state.keymap = state.keymaps[state.surface] || [];
}

void start();
