// Keyboard dispatch for the browser front end.
//
// The keymap is not written here. Every page carries its own surface's
// table, served from `pony.keymap` and `chronos.keymap` — the same
// tables the Textual front end builds its bindings from — so a key
// cannot mean one thing in the terminal and another here, and the help
// overlay cannot advertise a key that does not exist. It arrives in the
// page rather than in a request of its own, because a request leaves a
// window in which `keydown` is already listening and every press
// matches nothing and is dropped.
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
  keymap: [],
  surface: "reader",
  marked: new Set(),
  // Set once `Q` has stopped the server: the page is a corpse after
  // that and must not keep acting as though it were live.
  quitting: false,
  // What the page's form held when it loaded, for the Escape that
  // leaves it. Null on a page with no form.
  formAtLoad: null,
  // Which row stream is the current one. A new one makes every older
  // one stop at its next batch, which is what the reader's
  // `exclusive=True` worker does when a folder is loaded again.
  stream: 0,
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

// What an action applies to: every marked row, the open message, or the
// selected row.
//
// The open message comes before the DOM deliberately. A refreshed list
// may no longer hold the row that was acted on — archiving it takes it
// out of the folder — and `selectedRow()` then falls back to the first
// row, which is somebody else's mail. `composeUrl()` and
// `messageParams()` already ask the page what is open rather than the
// list; this is the same question, asked by the one caller that moves
// mail around.
function targets() {
  if (state.marked.size > 0) return Array.from(state.marked);
  const open = here().message;
  if (state.surface === "reader" && open) return [open];
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

// ── Where each pane was scrolled to ───────────────────────────
//
// Choosing a folder or opening a message is an ordinary navigation, and
// browsers restore scroll for history navigation only -- never for a
// new URL. Without this the folder list and the message list both
// return to the top on every click, and what you just chose can be off
// screen.
const PANE_SCROLL = "pony.paneScroll";
const SCROLLED_PANES = ["folders", "list"];

function paneScroller(id) {
  const pane = document.getElementById(id);
  return pane ? pane.querySelector(".pane-body") : null;
}

function rememberPaneScroll() {
  const tops = {};
  for (const id of SCROLLED_PANES) {
    const body = paneScroller(id);
    if (body) tops[id] = body.scrollTop;
  }
  try {
    window.sessionStorage.setItem(PANE_SCROLL, JSON.stringify(tops));
  } catch {
    // A private window, or blocked site data, throws. The position is a
    // convenience and nothing depends on having it.
  }
}

function restorePaneScroll() {
  let tops = null;
  try {
    tops = JSON.parse(window.sessionStorage.getItem(PANE_SCROLL) || "null");
  } catch {
    return;
  }
  if (!tops) return;
  for (const id of SCROLLED_PANES) {
    const body = paneScroller(id);
    if (body && typeof tops[id] === "number") body.scrollTop = tops[id];
  }
}

// ── Which folders are folded ──────────────────────────────────
//
// A <details> opens and shuts with no script at all; what the script
// adds is memory. Choosing a folder is a new URL, so the tree comes
// back built from scratch with everything open, and a fold would be
// undone by the very next click. The reader's tree stays as it was
// left, so the folds are written down beside the pane positions and on
// the same occasion -- `pagehide`, rather than on each toggle, because
// what matters is the state the page is left in.
const FOLDS = "pony.folds";

function foldable() {
  return document.querySelectorAll("[data-fold]");
}

// Which folders are shut right now.  An action replaces the pane with
// markup the server renders fully open, so the state has to come off
// the live tree and go back on -- not out of storage, which holds what
// was written down at the last navigation and would undo a fold made
// since.
function shutFolds() {
  const shut = [];
  for (const fold of foldable()) {
    if (!fold.open) shut.push(fold.dataset.fold);
  }
  return shut;
}

function applyFolds(shut) {
  const closed = new Set(shut);
  for (const fold of foldable()) {
    if (closed.has(fold.dataset.fold)) fold.open = false;
  }
}

function rememberFolds() {
  try {
    window.sessionStorage.setItem(FOLDS, JSON.stringify(shutFolds()));
  } catch {
    // A private window, or blocked site data, throws. A fold is a
    // convenience and nothing depends on remembering it.
  }
}

function restoreFolds() {
  let shut = null;
  try {
    shut = JSON.parse(window.sessionStorage.getItem(FOLDS) || "null");
  } catch {
    return;
  }
  if (!Array.isArray(shut)) return;
  applyFolds(shut);
}

// The folder being read, without the message open in it.
function folderParams() {
  const data = here();
  const params = new URLSearchParams();
  if (data.account) params.set("account", data.account);
  if (data.folder) params.set("folder", data.folder);
  if (data.query) params.set("q", data.query);
  return params;
}

function mailParams() {
  const params = folderParams();
  // Which row is open, so the refreshed list comes back with it marked
  // as such. Without it the fragment carries no selection at all, the
  // cursor dropped to the first row after every action, and the next
  // press acted on whatever happened to be at the top.
  const data = here();
  if (data.message) params.set("id", data.message);
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

// The same dialog, for a question answered by choosing rather than by
// typing.  `window.confirm` could not put these questions at all: it
// offers two buttons in the browser's own locale, so "Discard this
// message? Use Save draft to keep it." named a control the modal was
// covering and left dismissing the modal as one of the two answers.
// Browsers also offer to block a page from opening more dialogs, after
// which `confirm` returns false with no UI — a question nobody was
// asked, answered no.
//
// So: Pony's words, Pony's buttons, and *which* answer was chosen
// rather than a yes or a no, because the terminal screens that decide
// the same things have three buttons (`SaveDraftScreen`) or name the
// operation on them (`SyncConfirmScreen`).  Each choice is
// `{ value, label, key }`; the first is the default, which Enter takes,
// and the last is what Escape and a dismissed dialog mean.
function choose(question, choices) {
  const dialog = document.getElementById("prompt");
  // Nowhere to ask is not permission to go ahead.
  if (!dialog) return Promise.resolve(null);
  const form = document.createElement("form");
  const label = document.createElement("label");
  // A question, not a field's label: a halted sync describes what it
  // would delete over several lines before asking whether to.
  label.className = "question";
  label.textContent = question;
  const buttons = document.createElement("div");
  buttons.className = "invitation-answers";
  const made = choices.map((choice, position) => {
    const button = document.createElement("button");
    button.type = position === 0 ? "submit" : "button";
    if (position === 0) button.className = "primary";
    button.textContent = choice.label;
    button.dataset.choice = choice.value;
    buttons.append(button);
    return button;
  });
  form.append(label, buttons);
  dialog.replaceChildren(form);

  return new Promise((resolve) => {
    let answered = false;
    const finish = (answer) => {
      if (answered) return;
      answered = true;
      dialog.close();
      dialog.replaceChildren();
      resolve(answer);
    };
    const refuse = () => finish(choices[choices.length - 1].value);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      finish(choices[0].value);
    });
    made.forEach((button, position) => {
      button.addEventListener("click", (event) => {
        if (button.type === "submit") return;
        event.preventDefault();
        finish(choices[position].value);
      });
    });
    // The reader's own accelerators — Y to save, N to discard — and
    // Escape for the last choice.  Nothing here reaches the page
    // underneath: a modal dialog is open, and `D` in it must not trash
    // the message the question is about.
    form.addEventListener("keydown", (event) => {
      event.stopPropagation();
      if (event.key === "Escape") {
        event.preventDefault();
        refuse();
        return;
      }
      const hit = choices.find(
        (choice) => choice.key && choice.key === event.key.toLowerCase(),
      );
      if (hit) {
        event.preventDefault();
        finish(hit.value);
      }
    });
    // The window's own close request, for a browser that does not
    // deliver Escape as a key here.
    dialog.addEventListener(
      "cancel",
      (event) => {
        event.preventDefault();
        refuse();
      },
      { once: true },
    );
    dialog.showModal();
    made[0].focus();
  });
}

// ── Server actions ─────────────────────────────────────────────

// Actions whose label says "marked". They act on the marks and on
// nothing else, so the cursor is not offered as a fallback: it starts
// on the first row, so "Delete marked" on a freshly loaded contacts
// list used to delete the first contact without a word.
// `actions.MARKED_ONLY_ACTIONS` holds the same set on the server.
const markedOnly = new Set(["delete_marked", "merge_marked"]);

async function server(name, extra = {}) {
  const data = here();
  // `g` and ctrl+g mean "sync what I am looking at", and on the
  // calendar that is the calendar. The two tables spell the action the
  // same way, so the surface is what tells them apart.
  // The event form is a calendar page too: its Sync button used to
  // fetch mail, because the surface it declares is `event`.
  const onCalendar = state.surface === "calendar" || state.surface === "event";
  if (onCalendar && (name === "sync" || name === "background_sync")) {
    name = "sync_calendar";
  }
  // URLSearchParams, not FormData: the server reads a urlencoded body
  // so it needs no multipart parser.
  const body = new URLSearchParams();
  if (data.account) body.append("account", data.account);
  if (data.folder) body.append("folder", data.folder);
  const over = markedOnly.has(name) ? Array.from(state.marked) : targets();
  for (const id of over) body.append("id", id);
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
  // The server may answer with a question instead of a result: a sync
  // that would delete a large share of a folder is held until the user
  // has seen what it would do, exactly as the terminal holds it.
  if (outcome.confirm) {
    // The notices above are already on the page: a native `confirm`
    // blocked the paint, so the user was asked to decide with the
    // explanation that goes with the question still unpainted.
    const answer = await choose(outcome.confirm.question, [
      { value: "proceed", label: "Proceed [Y]", key: "y" },
      { value: "cancel", label: "Cancel [N]", key: "n" },
    ]);
    if (answer !== "proceed") return outcome;
    return server(name, { ...extra, ...outcome.confirm.fields });
  }
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
  // Where the cursor is, so it can be put back. The server marks the
  // open message in the fragment; a list with nothing open has only
  // this to go on, and dropping it sent the cursor to the top of the
  // folder after every action.
  const was = selectedRow();
  const wasId = was ? was.dataset.id : null;
  const response = await fetch(`/mail/rows?${mailParams()}`);
  const markup = await response.text();
  const parsed = new DOMParser().parseFromString(markup, "text/html");
  const fresh = parsed.getElementById("rows");
  // Where the open message sat, so its successor can be found after the
  // swap. Taken before, because the row is about to be gone.
  const openId = here().message;
  const openRow =
    openId && target.querySelector(`.row[data-id="${CSS.escape(openId)}"]`);
  const openAt = openRow
    ? Array.from(target.querySelectorAll(".row")).indexOf(openRow)
    : -1;
  if (fresh) {
    target.replaceWith(fresh);
    wireMarkTaps();
    // The message that was open may have left the folder: archiving,
    // trashing or moving it does that. `data-message` is rendered by
    // the server and this swapped only the rows, so it would go on
    // naming a message that is no longer here -- `targets()` prefers
    // it, so `A` pressed twice archived the first message and then
    // failed on it again, and the reading pane still showed it.
    //
    // Follow on to whatever took its place, which is what the reader
    // does: leaving for the folder on its own emptied the pane and put
    // the cursor back at the top.
    if (
      openId &&
      !document.querySelector(`#rows .row[data-id="${CSS.escape(openId)}"]`)
    ) {
      const rows = Array.from(document.querySelectorAll("#rows .row"));
      const next = openAt >= 0 ? rows[Math.min(openAt, rows.length - 1)] : null;
      const link = next && next.querySelector("a[href]");
      rememberPaneScroll();
      go(link ? link.href : `/mail?${folderParams()}`);
      return;
    }
    const again =
      wasId && document.querySelector(`#rows .row[data-id="${CSS.escape(wasId)}"]`);
    // The row may be gone -- archiving it takes it out of the folder --
    // and then the fragment's own selection, or the first row, is right.
    select(again || selectedRow());
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
  if (refreshed) {
    // The replacement starts at the top and fully open, so carry the
    // position and the folds over.
    const was = paneScroller("folders");
    const top = was ? was.scrollTop : 0;
    const folds = shutFolds();
    tree.replaceWith(refreshed);
    applyFolds(folds);
    const now = refreshed.querySelector(".pane-body");
    if (now) now.scrollTop = top;
  }
  // The swap put the first batch back, so the rest has to come again.
  void streamRows();
}

// The rest of the folder, a batch at a time.
//
// The page carries the first batch and a marker saying where the next
// one starts; this follows the markers until there are none. It is what
// the reader does -- rows into the table in batches, the event loop
// free between them -- so a long folder is usable from the first
// screenful instead of after the last row has been built. The marker is
// a link, so a page with no script still reaches the next batch; this
// consumes it before a user would.
async function streamRows() {
  const mine = ++state.stream;
  for (;;) {
    const marker = document.querySelector("#rows .more");
    if (!marker || mine !== state.stream) return;
    const offset = marker.dataset.offset;
    let fresh = null;
    try {
      const response = await fetch(`/mail/rows?${mailParams()}&offset=${offset}`);
      if (!response.ok) return;
      fresh = new DOMParser()
        .parseFromString(await response.text(), "text/html")
        .getElementById("rows");
    } catch {
      // Offline, or the server has stopped. The marker is still a
      // link, so the rest of the folder is still reachable by hand.
      return;
    }
    // A batch that is still in the document is the one to replace: a
    // refresh may have swapped the list while this was in flight, and
    // then this answer belongs to a list that is gone.
    if (!fresh || mine !== state.stream || !marker.isConnected) return;
    const arrived = Array.from(fresh.children);
    if (!arrived.length) {
      marker.remove();
      return;
    }
    marker.replaceWith(...arrived);
    wireMarkTaps();
    // The same offset twice would be a loop rather than a list.
    const next = document.querySelector("#rows .more");
    if (next && next.dataset.offset === offset) {
      next.remove();
      return;
    }
  }
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
  const window_ = changes.window || (params.get("view") === "agenda" && data.window);
  if (window_) params.set("window", window_);
  const query = changes.query === undefined ? data.query : changes.query;
  if (query) params.set("q", query);
  // Which calendars are shown. Prev, Next and Today kept the filter
  // because the server built those links; `n`, `p`, `t`, `a`, `M` and
  // a search built their own and dropped it, so pressing any of them
  // silently widened the view back to every calendar.
  for (const value of selectedCalendars()) params.append("cal", value);
  return `/calendar?${params}`;
}

function selectedCalendars() {
  try {
    return JSON.parse(here().cals || "[]") || [];
  } catch {
    return [];
  }
}

function shiftedDate(days) {
  const data = here();
  const base = data.date ? new Date(`${data.date}T12:00:00`) : new Date();
  base.setDate(base.getDate() + days);
  return base.toISOString().slice(0, 10);
}

function selectedEvent() {
  const row = selectedRow();
  if (!row || !row.dataset.uid || !row.dataset.cal) return null;
  // The calendar travels with the UID: the same UID can be mirrored
  // into two calendars, and the server must act on the one on screen.
  // Both are read off the row rather than split out of its id, because
  // a calendar name may contain a slash and so may a UID — this used
  // to cut "account/holidays/2026/uid" in the wrong place.
  return { cal: row.dataset.cal, uid: row.dataset.uid };
}

// ── Forms ──────────────────────────────────────────────────────

function currentForm() {
  return document.querySelector("form#compose-form, form#event-form, form#contact-form");
}

function submitForm() {
  const form = currentForm();
  if (form) form.requestSubmit();
}

// What the form holds, as one string, so Escape can tell a form someone
// has typed into from one they only opened.
function formState(form) {
  const parts = [];
  for (const field of form.elements) {
    if (!field.name) continue;
    parts.push(`${field.name}=${field.type === "checkbox" ? field.checked : field.value}`);
  }
  return parts.join("\n");
}

// What each form holds, and the button that files it.  All three keep
// that button in the pane's heading with a `form` attribute naming the
// form it submits; the compose form's also carries `save_draft`, which
// is what tells the server to file the message rather than send it.
const FORMS = {
  "compose-form": { holds: "message", files: "Save draft" },
  "event-form": { holds: "event", files: "Save" },
  "contact-form": { holds: "contact", files: "Save" },
};

function saveButton(form) {
  return (
    document.querySelector(`button[name="save_draft"][form="${form.id}"]`) ||
    document.querySelector(`button.primary[form="${form.id}"]`)
  );
}

// Escape on a form used to be `window.history.back()` with no question
// asked, so it threw away a half-written message. The terminal asks
// with `SaveDraftScreen`, which offers three answers, and so does this:
// the question used to name Save draft as the way to keep the message
// while the modal covering that button was the only thing the user
// could reach.
async function leaveForm() {
  const form = currentForm();
  if (form && state.formAtLoad !== null && formState(form) !== state.formAtLoad) {
    const kind = FORMS[form.id] || { holds: "form", files: "Save" };
    const save = saveButton(form);
    const answer = await choose(`Discard this ${kind.holds}?`, [
      ...(save ? [{ value: "save", label: `${kind.files} [Y]`, key: "y" }] : []),
      { value: "discard", label: "Discard [N]", key: "n" },
      { value: "cancel", label: "Cancel [Esc]" },
    ]);
    if (answer === "save" && save) {
      // Submitted by that button, so the form carries what it names.
      save.click();
      return;
    }
    if (answer !== "discard") return;
  }
  window.history.back();
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
  quit: quit,

  // Mail
  close_message: () => {
    // What `q` does in the reader: the message goes, the folder and the
    // search it was found in stay.
    if (!here().message) return;
    go(`/mail?${folderParams()}`);
  },
  // Space and PageDown, as the reader's message pane answers them:
  // down the message, and on to the next unread one once there is no
  // more of it to show.
  page_down_or_next: () => {
    const body = paneScroller("reader");
    if (!body) return;
    const more = body.scrollHeight - body.clientHeight - body.scrollTop;
    if (more > 2) {
      body.scrollTop += Math.max(body.clientHeight - 40, 40);
      return;
    }
    const next = rows()
      .slice(rows().indexOf(selectedRow()) + 1)
      .find((row) => row.classList.contains("unread"));
    const link = next && next.querySelector("a[href]");
    if (link) window.location.assign(link.href);
  },
  search: async () => {
    const data = here();
    const query = await ask("Search", { value: data.query || "" });
    if (query === null) return;
    if (state.surface === "contacts") {
      go(`/contacts?${new URLSearchParams({ q: query })}`);
      return;
    }
    if (state.surface === "calendar") {
      // Through `calendarUrl`, which already carries the view, the
      // span, the window and the calendar filter. Pasting a second
      // `q=` on the end left two, and the server reads the first --
      // so a second search kept showing the results of the first.
      go(calendarUrl({ query }));
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
  show_sender_check: () => {
    // The padlock beside `From` is a <details>, so there is nothing to
    // fetch and nothing to render: the key does what a click does.
    const panel = document.querySelector("details.sender-check");
    if (!panel) {
      say("Open a message first.", "warning");
      return;
    }
    panel.open = !panel.open;
    if (panel.open) panel.scrollIntoView({ block: "nearest" });
  },
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
  attachments_open: () => choosePart("Open"),
  attachments_save: () => choosePart("Save"),
  move: async () => {
    const target = await ask("Move to (account/folder)", { list: "folder-names" });
    if (!target) return;
    void server("move", { target });
  },
  copy: async () => {
    const target = await ask("Copy to (account/folder)", { list: "folder-names" });
    if (!target) return;
    void server("copy", { target });
  },
  new_folder: async () => {
    const name = await ask("New folder name");
    if (!name) return;
    void server("new_folder", { name });
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
    const event = selectedEvent();
    if (!event) {
      say("Select an event first.", "warning");
      return;
    }
    void server("delete_event", event);
  },
  toggle_calendars: () => {
    const first = document.querySelector("#folders .tree .folder");
    if (first) first.focus();
  },
  sync_dialog: () => {
    void server("sync");
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
  cancel: leaveForm,
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

// Which part to fetch, asked the way the reader asks it. The keys come
// from the numbering, not from the list on the page: a message with no
// attachments shows no list and still has part 0 and part T, which is
// the whole point of numbering them.
async function choosePart(verb) {
  const params = messageParams();
  if (!params) {
    say("Open a message first.", "warning");
    return;
  }
  const answer = await ask(
    `${verb} parts (0 = message, T = text, 1… = attachments, * = all)`,
    { placeholder: "0" },
  );
  if (!answer) return;
  // The server parses the selection, because the grammar is the
  // terminal's and lives in `pony.message_export`. Typing it here used
  // to send the whole answer as one part name, so `0,T` -- which the
  // prompt invites -- navigated the application away to a bare
  // "Which part?" page.
  let resolved;
  try {
    const response = await fetch(
      `/message/parts?${params}&select=${encodeURIComponent(answer)}`,
    );
    resolved = await response.json();
  } catch (error) {
    say(`The server did not answer: ${error}`, "error");
    return;
  }
  if (resolved.error) {
    say(resolved.error, "warning");
    return;
  }
  for (const part of resolved.parts) download(part.url, part.filename);
  say(`Downloading ${resolved.parts.map((part) => part.filename).join(", ")}`);
}

// Save one part without leaving the page. Every part is served with
// `Content-Disposition: attachment`, so a link click downloads it and
// the application stays where it is; `location.assign` would have been
// the same for a file and a navigation for anything else, which is how
// an unparsable answer replaced the page. Several parts are several
// clicks -- the browser may ask once whether to allow more than one
// download, which is its question to ask.
function download(url, filename) {
  const link = document.createElement("a");
  link.href = url;
  if (filename) link.download = filename;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
}

// ── Quitting ───────────────────────────────────────────────────

// Replaces the page with the fact that there is no longer a server
// behind it. Reached only when the tab could not be closed, which is
// the ordinary case: a tab the user navigated to cannot be closed by
// script. Measured on Chromium 154, `window.close()` does nothing
// there, and neither does the old `window.open('', '_self').close()`
// spelling; a tab opened by `window.open` does close. `pony web` hands
// its URL to the system browser, so the tab is always the first kind.
function showStopped() {
  document.title = "Pony Express has stopped";
  document.body.dataset.stopped = "1";
  const panel = document.createElement("div");
  panel.className = "stopped";
  const heading = document.createElement("h1");
  heading.textContent = "Pony Express has stopped";
  const line = document.createElement("p");
  line.textContent = "The server is no longer running — close this tab.";
  panel.append(heading, line);
  // Everything else goes: a folder tree and a message list that answer
  // nothing are worse than a blank page, because they look alive.
  document.body.replaceChildren(panel);
}

// `Q` means the same thing here as in the reader: stop Pony. Both
// halves are needed — closing the tab alone leaves the process serving,
// and stopping the process alone leaves a dead page on screen. The tab
// goes second, because the answer has to arrive before the socket does.
async function quit() {
  const outcome = await server("quit");
  if (!outcome || outcome.changed === 0) return;
  state.quitting = true;
  // Try to close first; `showStopped` is for when the browser refuses,
  // and the timer never fires if the tab really went.
  window.close();
  window.setTimeout(showStopped, 200);
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
  if (event.metaKey || state.quitting) return;
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
    void server(name);
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
    button.addEventListener("click", async () => {
      const name = button.dataset.action;
      // A tap is easier to do by accident than a key is, and a button
      // may say so: `data-confirm` is the question and `data-agree` the
      // word on the button that goes ahead. The key for the same action
      // is left alone -- ++shift+q++ in the reader asks nothing either.
      if (button.dataset.confirm) {
        const answer = await choose(button.dataset.confirm, [
          { value: "go", label: `${button.dataset.agree || "Yes"} [Y]`, key: "y" },
          { value: "cancel", label: "Cancel [N]", key: "n" },
        ]);
        if (answer !== "go") return;
      }
      // The same two halves `onKeyDown` chooses between: a button for
      // an action the page performs itself must not be posted to the
      // server instead. Quit is both -- the server stops and the tab
      // goes with it -- and posting it alone left a dead page up.
      const client = clientActions[name];
      if (client) {
        void client(name);
        return;
      }
      void server(name, buttonFields(button));
    });
  }
}

// What a button carries beyond its name. An Import button names the
// attachment it belongs to and the chooser that says where it goes, so
// one page can hold several and each sends its own.
function buttonFields(button) {
  const fields = {};
  if (button.dataset.attachment) fields.attachment = button.dataset.attachment;
  if (button.dataset.uid) fields.uid = button.dataset.uid;
  if (button.dataset.target) fields.cal = button.dataset.target;
  const chooserId = button.dataset.calendarFrom;
  if (chooserId) {
    const chooser = document.getElementById(chooserId);
    if (chooser) fields.target = chooser.value;
  }
  return fields;
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
      void server("respond_invitation", {
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
    const outcome = await server("delete_event", {
      uid: button.dataset.uid,
      cal: button.dataset.target || "",
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

function start() {
  state.surface = document.body.dataset.surface || "reader";
  // Before anything is wired, so no press can arrive unmatched.
  state.keymap = pageKeymap();
  setPanes(panes());
  wireActionButtons();
  wireMarkTaps();
  wireInvitation();
  wireEventDelete();
  wireAttachments();
  wireFolderToggle();
  const form = currentForm();
  state.formAtLoad = form ? formState(form) : null;
  restorePaneScroll();
  restoreFolds();
  void streamRows();
  // `pagehide` rather than `beforeunload`: it fires for an ordinary
  // navigation and does not keep the page out of the back/forward
  // cache.
  window.addEventListener("pagehide", () => {
    rememberPaneScroll();
    rememberFolds();
  });
  document.addEventListener("keydown", onKeyDown);
  // Clicking a row selects it, so the keyboard and the pointer agree
  // about what an action will act on.
  document.addEventListener("click", (event) => {
    if (!event.target.closest) return;
    // A panel opened to be read is dismissed the same way, so a click
    // anywhere inside it shuts it.  Not on the summary: that is the
    // native toggle, and handling it here would fight it.
    const panel = event.target.closest("details.sender-check[open]");
    if (panel && !event.target.closest("summary")) {
      panel.open = false;
      return;
    }
    const row = event.target.closest(".row");
    if (row) select(row);
  });
  select(selectedRow());
}

// The surface's keymap, as the page carries it.
function pageKeymap() {
  const holder = document.getElementById("keymap");
  if (!holder) return [];
  try {
    return JSON.parse(holder.textContent) || [];
  } catch {
    return [];
  }
}

start();
