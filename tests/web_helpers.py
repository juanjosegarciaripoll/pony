"""Reading a served page, and the script that drives it.

The browser front end has two halves and the tests need both.  The
server's half is HTML, so these helpers read a page the way the script
reads the DOM: the ``data-*`` the page tells its script about itself
with, the rows it offers, the actions its buttons name, the links it
leads to, and the fields a form would submit.  A flow test then walks
from page to page through the links the page itself carries, rather than
through URLs written out again in the test.

:func:`dispatch` is the one deliberate simulation.  ``server()`` in
``static/pony.js`` builds a request out of ``document.body.dataset`` and
the selected or marked rows; this builds the same request from the
served markup, so a flow test posts what a press would post rather than
a hand-made body.  ``tests/test_web_dispatch.py`` pins the script to
that shape, which is what keeps the simulation honest.

The script is *read* rather than run, because the suite has no
JavaScript engine and a dependency on one is not wanted: which actions
it implements, which names it calls, which paths it fetches.  That
catches a dispatcher promising an action it has not got, or calling a
function nobody defined — which is exactly how ``Q`` came to be dead —
but not a mistake in what the function then does.  For that there is
``tests/test_web_browser.py``, which runs the real script in a headless
Chromium when one is present and ``PONY_BROWSER_UI=1`` asks for it.

Reading has one trap worth naming: an assertion that a string appears
in the source holds whether or not the code around it ever runs.
Wrapping ``quit()``'s body in ``if (false) {}`` left every such
assertion about quitting still passing.  So what is read is what
reading can decide — a name, a table, a path — and what the code
*does* belongs in the browser test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING

from pony.web.actions import MARKED_ONLY_ACTIONS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.testclient import TestClient

#: The dispatcher the browser runs, as the server serves it.
JS_FILE = Path(__file__).resolve().parents[1] / "src/pony/web/static/pony.js"


def script() -> str:
    """``static/pony.js``, as text."""
    return JS_FILE.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# Reading a page
# ----------------------------------------------------------------------

_BODY_TAG = re.compile(r"<body\b([^>]*)>")
_ATTRIBUTE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
_FORM = re.compile(r"<form\b(?P<open>[^>]*)>(?P<fields>.*?)</form>", re.DOTALL)
_INPUT = re.compile(r"<input\b[^>]*>")
_TEXTAREA = re.compile(r"<textarea\b(?P<open>[^>]*)>(?P<text>.*?)</textarea>")
_SELECT = re.compile(
    r"<select\b(?P<open>[^>]*)>(?P<options>.*?)</select>",
    re.DOTALL,
)
_OPTION = re.compile(r"<option\b[^>]*>")


def _attributes(tag: str) -> dict[str, str]:
    return {name: unescape(value) for name, value in _ATTRIBUTE.findall(tag)}


def page_data(page: str) -> dict[str, str]:
    """The ``data-*`` attributes on ``<body>``, without the prefix.

    This is ``document.body.dataset``: the account, folder, open message,
    search query, surface and viewed date that every action the script
    sends is built out of.
    """
    found = _BODY_TAG.search(page)
    if found is None:
        return {}
    return {
        name.removeprefix("data-"): value
        for name, value in _attributes(found.group(1)).items()
        if name.startswith("data-")
    }


def attribute_values(page: str, attribute: str) -> list[str]:
    """Every value of *attribute* in *page*, in document order."""
    pattern = re.compile(rf'\b{re.escape(attribute)}="([^"]*)"')
    return [unescape(value) for value in pattern.findall(page)]


def actions(page: str) -> set[str]:
    """Every action a button or link on *page* names."""
    return set(attribute_values(page, "data-action"))


def links(page: str) -> list[str]:
    """The internal links on *page*, deduplicated, in document order.

    Stylesheets and anything off-site are left out: what a flow test
    wants is the pages this page leads to.
    """
    seen: list[str] = []
    for href in attribute_values(page, "href"):
        if not href.startswith("/") or href.startswith("/static/"):
            continue
        if href not in seen:
            seen.append(href)
    return seen


def link_to(page: str, label: str) -> str:
    """The link whose text is *label*, as the user would click it."""
    pattern = re.compile(
        rf"<a\b[^>]*\bhref=\"([^\"]+)\"[^>]*>{re.escape(label)}</a>",
    )
    found = pattern.search(page)
    if found is None:
        message = f"no link labelled {label!r}"
        raise AssertionError(message)
    return unescape(found.group(1))


def _tag_with_id(page: str, element_id: str, tag: str = r"[a-z]+") -> str:
    """The attributes of the ``<tag id="element_id">`` in *page*."""
    pattern = re.compile(rf'<{tag}\b([^>]*\bid="{re.escape(element_id)}"[^>]*)>')
    found = pattern.search(page)
    if found is None:
        message = f"no element with id {element_id!r}"
        raise AssertionError(message)
    return found.group(1)


def element_data(page: str, element_id: str) -> dict[str, str]:
    """The ``data-*`` of one element: its ``dataset``, as the script reads it."""
    return {
        name.removeprefix("data-"): value
        for name, value in _attributes(_tag_with_id(page, element_id)).items()
        if name.startswith("data-")
    }


def link_by_id(page: str, element_id: str) -> str:
    """Where the link with *element_id* goes — Prev, Today, Next."""
    return _attributes(_tag_with_id(page, element_id, "a"))["href"]


def select_value(page: str, element_id: str) -> str:
    """What a chooser would submit: ``getElementById(id).value``."""
    anchor = page.find(f'id="{element_id}"')
    start = page.rfind("<select", 0, anchor) if anchor >= 0 else -1
    found = _SELECT.search(page, start) if start >= 0 else None
    if found is None:
        message = f"no chooser with id {element_id!r}"
        raise AssertionError(message)
    options = _OPTION.findall(found.group("options"))
    chosen = next(
        (tag for tag in options if "selected" in tag),
        options[0] if options else "",
    )
    return _attributes(chosen).get("value", "")


def row_link(page: str, row_id: str | None = None) -> str:
    """Where a row leads: the link ``openSelected()`` follows.

    A row's own text is spans — the mark, the date, who and the subject
    — so a row is opened by its ``data-id`` rather than by its label.
    With no *row_id* the first row is taken, which is where the cursor
    starts.
    """
    rows = row_ids(page)
    wanted = row_id if row_id is not None else (rows[0] if rows else "")
    anchor = page.find(f'data-id="{wanted}"')
    found = re.compile(r'href="([^"]+)"').search(page, anchor) if anchor >= 0 else None
    if found is None:
        message = f"no row {wanted!r} with a link in it"
        raise AssertionError(message)
    return unescape(found.group(1))


def row_ids(page: str) -> list[str]:
    """The ``data-id`` of each row, in the order the page lists them."""
    seen: list[str] = []
    for value in attribute_values(page, "data-id"):
        if value not in seen:
            seen.append(value)
    return seen


def _find_form(page: str, selector: str) -> re.Match[str]:
    """The form *selector* names: ``#id`` or ``.class``."""
    for match in _FORM.finditer(page):
        attributes = _attributes(match.group("open"))
        if selector.startswith("#") and attributes.get("id") == selector[1:]:
            return match
        classes = attributes.get("class", "").split()
        if selector.startswith(".") and selector[1:] in classes:
            return match
    message = f"no form matching {selector!r}"
    raise AssertionError(message)


def form_target(page: str, selector: str) -> tuple[str, str]:
    """How the form *selector* names submits: ``(method, action)``."""
    attributes = _attributes(_find_form(page, selector).group("open"))
    return attributes.get("method", "get").lower(), attributes.get("action", "")


def form_fields(page: str, selector: str) -> dict[str, str]:
    """What the browser would submit for the form *selector* names.

    Unchecked boxes are left out, as a browser leaves them out, and a
    textarea carries its text rather than a ``value`` attribute.
    """
    found = _find_form(page, selector)
    markup = found.group("fields")
    fields: dict[str, str] = {}
    for tag in _INPUT.findall(markup):
        attributes = _attributes(tag)
        name = attributes.get("name")
        if not name:
            continue
        if attributes.get("type") == "checkbox" and "checked" not in tag:
            continue
        fields[name] = attributes.get("value", "")
    for area in _TEXTAREA.finditer(markup):
        name = _attributes(area.group("open")).get("name")
        if name:
            fields[name] = unescape(area.group("text"))
    for chooser in _SELECT.finditer(markup):
        name = _attributes(chooser.group("open")).get("name")
        if not name:
            continue
        options = _OPTION.findall(chooser.group("options"))
        chosen = next(
            (tag for tag in options if "selected" in tag),
            options[0] if options else "",
        )
        fields[name] = _attributes(chosen).get("value", "")
    return fields


# ----------------------------------------------------------------------
# Acting on a page
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dispatched:
    """What the script gets back from ``POST /action/<name>``."""

    status: int
    changed: int
    notices: tuple[tuple[str, str], ...]
    confirm: dict[str, object] | None

    @property
    def texts(self) -> tuple[str, ...]:
        """The notice texts, in the order the page would show them."""
        return tuple(text for text, _severity in self.notices)


def dispatch(
    client: TestClient,
    page: str,
    action: str,
    *,
    ids: Sequence[str] = (),
    **extra: str,
) -> Dispatched:
    """Run *action* the way a press on *page* would run it.

    The body is the one ``server()`` builds: the account and folder the
    page says it is showing, the rows that are marked or — with none
    marked — the one that is selected, and whatever the prompt added.
    With no *ids* given the page's own rows are used, which is what the
    cursor sits on when a page has just loaded.
    """
    data = page_data(page)
    if action in {"sync", "background_sync"} and data.get("surface") == "calendar":
        action = "sync_calendar"
    body: dict[str, str | list[str]] = {}
    if data.get("account"):
        body["account"] = data["account"]
    if data.get("folder"):
        body["folder"] = data["folder"]
    if action in MARKED_ONLY_ACTIONS:
        # No fallback to the cursor: these act on the marks alone, and
        # `server()` in `pony.js` sends nothing when there are none.
        body["id"] = list(ids)
    else:
        body["id"] = list(ids) if ids else _selected(page, data)
    body.update(extra)
    response = client.post(f"/action/{action}", data=body)
    outcome = response.json()
    return Dispatched(
        status=response.status_code,
        changed=int(outcome.get("changed", 0)),
        notices=tuple(
            (notice["text"], notice.get("severity", "information"))
            for notice in outcome.get("notices", ())
        ),
        confirm=outcome.get("confirm"),
    )


def dispatch_agreeing(
    client: TestClient,
    page: str,
    action: str,
    *,
    ids: Sequence[str] = (),
    **extra: str,
) -> Dispatched:
    """Run *action*, saying yes to whatever it asks first.

    An irreversible operation answers with a question instead of a
    result, and ``server()`` in ``static/pony.js`` puts it in Pony's own
    ``<dialog>`` -- Proceed or Cancel, named as ``SyncConfirmScreen``
    names them -- then re-sends the same action with the fields the
    question carries once Proceed is pressed.  This does the re-sending
    half, so a flow test walks the whole of a delete or a merge rather
    than stopping at the question; the dialog itself is pressed in a
    real browser by
    ``tests/test_web_browser.py::test_a_held_action_is_confirmed_in_ponys_own_dialog``,
    which is possible only because it is a ``<dialog>``: a native
    ``confirm`` blocks the page and the ``--dump-dom`` harness with it.
    """
    asked = dispatch(client, page, action, ids=ids, **extra)
    if asked.confirm is None:
        return asked
    fields = asked.confirm.get("fields", {})
    assert isinstance(fields, dict), fields
    answered = {str(name): str(value) for name, value in fields.items()}
    return dispatch(client, page, action, ids=ids, **extra, **answered)


def _selected(page: str, data: dict[str, str]) -> list[str]:
    """What an action applies to, as ``targets()`` in `pony.js` decides.

    Faithfully, which is the point.  This helper used to prefer
    ``data["message"]`` while the script read only the DOM, and that one
    line is why a hundred and seventy-seven passing tests sat on top of
    the bug where the action after an action hit the first row of the
    folder instead of the open message.  A simulation that is *better*
    than the code it simulates hides exactly the defect it exists to
    catch.

    So: the open message on the reader surface, else the row carrying
    ``aria-selected``, else the first row — which is where ``start()``
    puts the cursor on a page that has just loaded.
    """
    if data.get("surface") == "reader" and data.get("message"):
        return [data["message"]]
    cursor = selected_row_id(page)
    return [] if cursor is None else [cursor]


def selected_row_id(page: str) -> str | None:
    """The row the cursor sits on: ``selectedRow()`` in `pony.js`."""
    for match in re.finditer(r"<li\b([^>]*)>", page):
        attributes = _attributes(match.group(1))
        if attributes.get("aria-selected") == "true":
            return attributes.get("data-id")
    rows = row_ids(page)
    return rows[0] if rows else None


# ----------------------------------------------------------------------
# Reading the script
# ----------------------------------------------------------------------

_TABLE_START = "const clientActions = {"
_TABLE_KEY = re.compile(r"^  ([a-z_]+):")
_TABLE_ADDITION = re.compile(r"^clientActions\.([a-z_]+) =", re.MULTILINE)
_DECLARED = re.compile(
    r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
    re.MULTILINE,
)
# A name bound by a parameter list: `function finish(answer)`, `(resolve) =>`.
_PARAMS = re.compile(
    r"function\s*[A-Za-z_$\w]*\s*\(([^)]*)\)|\(([^)]*)\)\s*=>",
)
_NAME = re.compile(r"[A-Za-z_$][\w$]*")
_CALLED = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
_PATHS = re.compile(r"[\"'`](/[^\"'`\s]*)")

# Words that are followed by a bracket without being a call.
_JS_KEYWORDS = frozenset(
    {
        "catch",
        "do",
        "else",
        "for",
        "function",
        "if",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "switch",
        "this",
        "throw",
        "try",
        "typeof",
        "void",
        "while",
    },
)

# Everything the browser itself provides that this script calls.
_JS_GLOBALS = frozenset(
    {
        "Array",
        "DOMParser",
        "Date",
        "JSON",
        "Math",
        "Number",
        "Object",
        "Promise",
        "Set",
        "String",
        "URLSearchParams",
        "async",
        "encodeURIComponent",
        "fetch",
        "document",
        "window",
    },
)


def _code_only(source: str) -> str:
    """*source* with its comments and the text of its strings removed.

    A string can hold anything that looks like code — ``"Move to
    (account/folder)"`` reads as a call to ``to`` — so the text goes,
    while a template literal's ``${...}`` holes stay, because those are
    code and do call things.
    """
    kept: list[str] = []
    modes = ["code"]
    braces = [0]
    index = 0
    while index < len(source):
        mode = modes[-1]
        char = source[index]
        pair = source[index : index + 2]
        if mode == "code":
            if pair in {"//", "/*"}:
                modes.append("line" if pair == "//" else "block")
                braces.append(0)
                index += 2
            elif char in {"'", '"', "`"}:
                modes.append(char)
                braces.append(0)
                kept.append(" ")
                index += 1
            elif char == "}" and len(modes) > 1 and braces[-1] == 0:
                # The end of a `${...}` hole, back into the template.
                modes.pop()
                braces.pop()
                kept.append(" ")
                index += 1
            else:
                if len(modes) > 1 and char in {"{", "}"}:
                    braces[-1] += 1 if char == "{" else -1
                kept.append(char)
                index += 1
            continue
        if mode == "line":
            if char == "\n":
                modes.pop()
                braces.pop()
                kept.append("\n")
            index += 1
            continue
        if mode == "block":
            if pair == "*/":
                modes.pop()
                braces.pop()
                index += 2
            else:
                index += 1
            continue
        # Inside a string or a template literal.
        if char == "\\":
            index += 2
        elif mode == "`" and pair == "${":
            modes.append("code")
            braces.append(0)
            kept.append(" ")
            index += 2
        elif char == mode:
            modes.pop()
            braces.pop()
            kept.append(" ")
            index += 1
        else:
            index += 1
    return "".join(kept)


def client_actions(source: str) -> set[str]:
    """Every action name the dispatcher implements in the page itself."""
    _before, _marker, rest = source.partition(_TABLE_START)
    table, _end, after = rest.partition("\n};")
    names = {
        found.group(1)
        for line in table.splitlines()
        if (found := _TABLE_KEY.match(line)) is not None
    }
    return names | set(_TABLE_ADDITION.findall(after))


def declared_names(source: str) -> set[str]:
    """Every name the script binds: functions, consts, locals, parameters."""
    code = _code_only(source)
    bound = {name for pair in _DECLARED.findall(code) for name in pair if name}
    for params in _PARAMS.findall(code):
        for group in params:
            bound.update(_NAME.findall(group))
    return bound


def called_names(source: str) -> set[str]:
    """Every bare name the script calls, globals and keywords aside."""
    called = set(_CALLED.findall(_code_only(source)))
    return called - _JS_KEYWORDS - _JS_GLOBALS


#: Methods the script is allowed to call on ``window``, ``document`` and
#: the standard objects.  Bare calls are checked against what the script
#: declares; a *member* call is preceded by a dot and so slipped through
#: that check entirely, which is how a typo'd ``window.closeTab()`` --
#: a method no browser has -- would have passed every test there was.
BROWSER_METHODS = frozenset(
    {
        "Array.from",
        "Array.isArray",
        "JSON.parse",
        "JSON.stringify",
        "Math.max",
        "Math.min",
        "Number.toFixed",
        "Object.entries",
        "document.addEventListener",
        "document.body.append",
        "document.body.classList.toggle",
        "document.body.replaceChildren",
        "document.createElement",
        "document.getElementById",
        "document.querySelector",
        "document.querySelectorAll",
        "window.addEventListener",
        "window.close",
        "window.history.back",
        "window.localStorage.getItem",
        "window.localStorage.setItem",
        "window.sessionStorage.getItem",
        "window.sessionStorage.setItem",
        "window.location.assign",
        "window.location.reload",
        "window.open",
        "window.setTimeout",
    },
)

_MEMBER_CALL = re.compile(
    r"\b(window|document|JSON|Math|Array|Object|Number|String)"
    r"((?:\.[A-Za-z_$][\w$]*)+)\s*\(",
)


def browser_calls(source: str) -> set[str]:
    """Every method the script calls on a browser object, dotted path and all.

    ``called_names`` deliberately ignores anything preceded by a dot,
    because most of those are methods of elements the script just made
    and there is nothing to check them against.  The handful of standard
    objects *can* be checked, and must be: a misspelt one is a
    ``TypeError`` at the moment the user presses the key.
    """
    return {
        f"{found.group(1)}{found.group(2)}"
        for found in _MEMBER_CALL.finditer(_code_only(source))
    }


def fetched_paths(source: str) -> set[str]:
    """Every server path the script navigates to or fetches.

    A path built from a template literal keeps its literal head, which
    is the part a route has to match: ``/mail/rows?${params}`` is
    ``/mail/rows``.
    """
    found: set[str] = set()
    for literal in _PATHS.findall(source):
        head = literal.partition("?")[0].partition("$")[0]
        if head and head != "/":
            found.add(head.rstrip("/") or "/")
    return found


__all__ = [
    "BROWSER_METHODS",
    "JS_FILE",
    "Dispatched",
    "actions",
    "attribute_values",
    "browser_calls",
    "called_names",
    "client_actions",
    "declared_names",
    "dispatch",
    "dispatch_agreeing",
    "element_data",
    "fetched_paths",
    "form_fields",
    "form_target",
    "link_by_id",
    "link_to",
    "links",
    "page_data",
    "row_ids",
    "row_link",
    "script",
    "select_value",
    "selected_row_id",
]
