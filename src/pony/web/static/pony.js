// Keyboard dispatch for the browser front end.
//
// The keymap is not written here. It is fetched from /keymap.json, which
// serves `pony.keymap` — the same table the Textual front end builds its
// bindings from — so a key cannot mean one thing in the terminal and
// another here, and the help overlay cannot advertise a key that does
// not exist.
//
// Three kinds of action:
//   * client   — selection and overlays, done here with no request
//   * server   — POST /action/<name> over the selected rows, then
//                refresh the list and show the notices that come back
//   * absent   — in the keymap but not available in the browser yet;
//                says so instead of doing nothing silently
//
// No framework and no CDN: a tailnet has no reason to reach the public
// internet, and a front end that needs one is a front end that stops
// working on a train.

"use strict";

const state = {
  keymap: [],
  marked: new Set(),
};

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

function notify(notices) {
  const box = document.getElementById("notices");
  if (!box) return;
  box.replaceChildren(
    ...notices.map((notice) => {
      const line = document.createElement("p");
      line.className = `notice ${notice.severity}`;
      line.textContent = notice.text;
      return line;
    }),
  );
  if (notices.length > 0) {
    window.setTimeout(() => box.replaceChildren(), 6000);
  }
}

async function runServerAction(name) {
  const account = document.body.dataset.account;
  const folder = document.body.dataset.folder;
  if (!account || !folder) return;
  // URLSearchParams, not FormData: the server reads a urlencoded body
  // so it needs no multipart parser.
  const body = new URLSearchParams();
  body.append("account", account);
  body.append("folder", folder);
  for (const id of targets()) body.append("id", id);

  const response = await fetch(`/action/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  const outcome = await response.json();
  notify(outcome.notices || []);
  if (outcome.changed > 0) {
    state.marked.clear();
    await refreshRows(account, folder);
  }
}

// Only the list is replaced, so the open message and the scroll position
// of everything else survive an action.
async function refreshRows(account, folder) {
  const target = document.getElementById("rows");
  if (!target) return;
  const path = `/f/${encodeURIComponent(account)}/${encodeURIComponent(folder)}/rows`;
  const response = await fetch(path);
  const markup = await response.text();
  const parsed = new DOMParser().parseFromString(markup, "text/html");
  const fresh = parsed.getElementById("rows");
  if (fresh) {
    target.replaceWith(fresh);
  } else {
    target.replaceChildren();
  }
}

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

const clientActions = {
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
  show_help: showHelp,
  toggle_calendar: () => {
    window.location.assign(
      window.location.pathname === "/agenda" ? "/" : "/agenda",
    );
  },
};

// The same normalised spelling `keymap.web_key` produces, so the two
// sides compare without either guessing at the other's vocabulary.
function pressed(event) {
  const parts = [];
  if (event.ctrlKey) parts.push("ctrl");
  if (event.shiftKey && event.key.length > 1) parts.push("shift");
  parts.push(event.key);
  return parts.join("+");
}

function onKeyDown(event) {
  if (event.metaKey || event.altKey) return;
  const target = event.target;
  // Never steal a key from something being typed into.
  if (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target.isContentEditable
  ) {
    return;
  }
  const binding = state.keymap.find((entry) => entry.key === pressed(event));
  if (!binding) return;
  event.preventDefault();
  const client = clientActions[binding.action];
  if (client) {
    client();
    return;
  }
  if (binding.available) {
    void runServerAction(binding.action);
    return;
  }
  notify([
    {
      text: `${binding.description} is only in the terminal for now.`,
      severity: "warning",
    },
  ]);
}

async function start() {
  const response = await fetch("/keymap.json");
  state.keymap = await response.json();
  document.addEventListener("keydown", onKeyDown);
  // Clicking a row selects it, so the keyboard and the pointer agree
  // about what an action will act on.
  document.addEventListener("click", (event) => {
    const row = event.target.closest(".row");
    if (row) select(row);
  });
  select(selectedRow());
}

void start();
