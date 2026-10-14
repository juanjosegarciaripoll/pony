"""The mail reader in a browser: the same three panes, in HTML.

Folders, a message list and a reader, divided the way the terminal
divides them, each pane scrolling on its own.  Every row comes from
:mod:`pony.mail.views` and every operation is a call into
:mod:`pony.mail.mailbox_actions`, :mod:`pony.mail.delivery` or
:mod:`pony.invitation_actions`, so this module is markup, links and the
plumbing between a form field and an argument.

A message body is shown as text.  That is not a limitation of the
front end but a choice the terminal makes too: the text is what a mail
client reads to you, it carries no tracking and nothing to execute, and
``render_message`` already strips the HTML into it.  The HTML version is
one key away (``w``) and opens as a page of its own — not inside the
application — under a policy that allows it no scripts, no network and
no reach into anything else.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pony.outcome import Notice, Outcome, failure, info, warn

from ..invitation import CALENDAR_CONTENT_TYPES
from ..mail import mailbox_actions
from ..mail.authentication import MessageAuthentication
from ..mail.domain import FolderRef, MessageFlag
from ..mail.folder_utils import shows_recipient
from ..mail.mailbox_actions import folder_unread_counts
from ..mail.message_export import listed_parts
from ..mail.message_renderer import fmt_size, parse_styled_body, render_message
from ..mail.views import (
    ROW_BATCH,
    build_folder_tree,
    correspondent_label,
    message_row,
    summary_of,
)
from . import urls
from .actions import (
    Action,
    ActionContext,
    calendar_value,
    folder_value,
    parse_calendar,
    parse_target,
)
from .html import Raw, element, fragment, join
from .layout import PageSurface, Tab, columns, page, pane

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pony.calendar.domain import CalendarRef
    from pony.outcome import Notice

    from ..invitation import Invitation
    from ..mail.domain import FolderMessageSummary, IndexedMessage, MessageRef
    from ..mail.message_renderer import AttachmentInfo, RenderedMessage
    from ..mail.views import FolderTreeNode
    from .app import WebServices


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------


def mail_page(
    services: WebServices,
    *,
    folder: FolderRef,
    message: IndexedMessage | None = None,
    query: str = "",
    notices: Sequence[Notice] = (),
    offset: int = 0,
) -> str:
    """The reader: folders beside the list, with a message open or not."""
    title = (
        folder.folder_name if message is None else (message.subject or "(no subject)")
    )
    right = [
        list_pane(
            services,
            folder,
            open_id=None if message is None else message.message_ref.id,
            query=query,
            offset=offset,
        ),
    ]
    if message is not None:
        right.append(reader_pane(services, message))
    return page(
        title=f"{title} — Pony Express",
        surface=PageSurface.READER,
        tab=Tab.MAIL,
        status=_status(services, folder, query=query),
        panes=fragment(
            folder_pane(services, folder),
            element(
                "div",
                join(right),
                id="right",
                class_="reader-open" if message is not None else "",
            ),
        ),
        data={
            "account": folder.account_name,
            "folder": folder.folder_name,
            "query": query,
            "message": "" if message is None else str(message.message_ref.id),
        },
        notices=notices,
        folders=folder_options(services),
    )


def _status(services: WebServices, folder: FolderRef, *, query: str) -> str:
    """The line beside the tabs: what is open, and how much is unread."""
    if query:
        return f"Search: {query}"
    if not folder.folder_name:
        return ""
    unread = services.index.unread_counts_by_folder(
        account_name=folder.account_name,
    ).get(folder.folder_name, 0)
    label = folder_value(folder)
    return f"{label} — {unread} unread" if unread else label


def all_folders(services: WebServices) -> list[FolderRef]:
    """Every folder of every account, from everything that knows of one.

    The mirror is not the only place a folder is known: one the index
    or the sync state has a row for but no directory yet is still a
    folder, which is why the folder pane asks `folder_unread_counts`.
    Two other loops asked the mirror alone, so the move and go-to
    prompts offered a different set from the pane beside them -- the
    divergence the terminal's `action_goto_folder` had.
    """
    return [
        FolderRef(account_name=account.name, folder_name=name)
        for account in services.config.accounts
        for name in folder_unread_counts(services.mailboxes, account.name)
    ]


def folder_options(services: WebServices) -> list[str]:
    """``account/folder`` for every folder, for the move and go-to prompts."""
    return [folder_value(folder) for folder in all_folders(services)]


# ----------------------------------------------------------------------
# The folder pane
# ----------------------------------------------------------------------


def panes(
    services: WebServices,
    folder: FolderRef,
    *,
    open_id: int | None = None,
    query: str = "",
) -> Raw:
    """The list and the folder tree in one fragment.

    The unread counts beside the folders are a projection of the same
    rows the list is, so what an action changes it changes in both.
    Rendering them together is what lets the page repaint in one round
    trip rather than two, and what stops it showing a list from after
    the action beside counts from before it.
    """
    return fragment(
        rows(services, folder, open_id=open_id, query=query),
        folder_pane(services, folder),
    )


def folder_pane(services: WebServices, selected: FolderRef | None) -> Raw:
    sections: list[Raw] = []
    for account in services.config.accounts:
        mirror = services.mirrors.get(account.name)
        if mirror is None:
            continue
        # Every folder the account has, from all three places that
        # know about one -- the same question the reader's folder panel
        # asks, answered in the same place.
        counts = folder_unread_counts(services.mailboxes, account.name)
        tree = build_folder_tree(
            folder_names=list(counts),
            unread_counts=counts,
            account_name=account.name,
        )
        total = sum(counts.values())
        sections.append(
            element(
                "section",
                element(
                    "details",
                    element(
                        "summary",
                        element(
                            "h2",
                            element("span", account.name, class_="account-name"),
                            element("span", str(total), class_="unread")
                            if total
                            else None,
                        ),
                    ),
                    element(
                        "ul",
                        join(_folder_nodes(tree, selected, path=account.name)),
                        class_="tree",
                    ),
                    class_="account",
                    data_fold=f"a:{account.name}",
                    open=True,
                ),
            ),
        )
    return pane(
        name="folders",
        heading=columns(("folder-name", "Folder"), ("unread", "Unread")),
        body=join(sections) or element("p", "No folders yet.", class_="empty"),
    )


def _folder_classes(*, current: bool, quiet: bool) -> str:
    classes = ["folder"]
    if current:
        classes.append("current")
    if quiet:
        # Nothing unread anywhere below: the terminal dims these.
        classes.append("quiet")
    return " ".join(classes)


def _folder_nodes(
    nodes: tuple[FolderTreeNode, ...],
    selected: FolderRef | None,
    *,
    path: str,
) -> list[Raw]:
    """One ``<li>`` per node, nested.

    *path* is where these nodes hang -- the account's name, then a
    label per level -- which is what a fold is remembered under.  The
    labels rather than the stored folder name, because the server's
    delimiter may be a dot and a synthetic level has no stored name of
    its own at all.
    """
    items: list[Raw] = []
    for node in nodes:
        # The folder's own unread count, as the reader's tree shows it;
        # whether anything below it is unread is a separate signal, dim
        # in the terminal and a class here.
        label = element("span", node.label, class_="folder-name")
        count = (
            element("span", str(node.own_unread), class_="unread")
            if node.own_unread
            else None
        )
        quiet = node.descendant_unread() == 0
        # A synthetic level — one that exists only because a folder name
        # was hierarchical — has nothing to open.
        body = (
            element(
                "a",
                label,
                count,
                href=urls.mail_url(node.folder_ref),
                class_=_folder_classes(
                    current=node.folder_ref == selected,
                    quiet=quiet,
                ),
                data_folder=node.folder_ref.folder_name,
                data_account=node.folder_ref.account_name,
            )
            if node.folder_ref is not None
            else element(
                "span",
                label,
                count,
                class_="folder synthetic" + ("" if not quiet else " quiet"),
            )
        )
        if not node.children:
            items.append(element("li", body))
            continue
        # A folder with folders under it folds, as the reader's tree
        # folds it: a <details> whose summary is the folder's own row,
        # so a click on the name still opens the folder and a click on
        # the twisty beside it shows or hides what is below.  No script
        # is needed for that; `wireFolds` only remembers it across the
        # next navigation, the way the reader's tree stays as it was
        # left.
        items.append(
            element(
                "li",
                element(
                    "details",
                    element("summary", body),
                    element(
                        "ul",
                        join(
                            _folder_nodes(
                                node.children,
                                selected,
                                path=f"{path}/{node.label}",
                            ),
                        ),
                        class_="tree",
                    ),
                    class_="branch",
                    data_fold=f"f:{path}/{node.label}",
                    open=True,
                ),
                class_="branch",
            ),
        )
    return items


# ----------------------------------------------------------------------
# The message list
# ----------------------------------------------------------------------


def list_pane(
    services: WebServices,
    folder: FolderRef,
    *,
    open_id: int | None = None,
    query: str = "",
    offset: int = 0,
) -> Raw:
    """The list, with the column headings the terminal shows above it."""
    show_recipients = shows_recipient(services.config, folder)
    return pane(
        name="list",
        heading=fragment(
            columns(
                ("mark", ""),
                ("date", "Date"),
                ("who", correspondent_label(shows_recipient=show_recipients)),
                ("subject", "Subject"),
            ),
            element(
                "span",
                element(
                    "a",
                    "Write",
                    href=urls.compose_url(
                        mode="new",
                        account=folder.account_name,
                        folder=folder.folder_name,
                    ),
                    class_="action",
                ),
                _search_form(folder, query),
                class_="actions",
            ),
        ),
        body=rows(
            services,
            folder,
            open_id=open_id,
            query=query,
            show_recipients=show_recipients,
            offset=offset,
        ),
    )


def _search_form(folder: FolderRef, query: str) -> Raw:
    """The same search the `/` key runs, for a screen with no keyboard.

    Submitting it empty is how a search is left, which is what Escape
    does in the terminal.
    """
    return element(
        "form",
        element(
            "input",
            type="search",
            name="q",
            value=query,
            placeholder="Search this folder",
            aria_label="Search this folder",
        ),
        element("input", type="hidden", name="account", value=folder.account_name),
        element("input", type="hidden", name="folder", value=folder.folder_name),
        method="get",
        action=urls.MAIL,
        class_="search-form inline",
    )


def rows(
    services: WebServices,
    folder: FolderRef,
    *,
    open_id: int | None = None,
    query: str = "",
    show_recipients: bool | None = None,
    offset: int = 0,
) -> Raw:
    """One batch of rows, which is all an action needs to refresh.

    A batch rather than the folder, because the reader draws a batch
    too: `ROW_BATCH` rows go into the terminal's table at a time, with
    the event loop free between them, so a big folder is usable from
    the first screenful. Here the first batch is in the page and the
    rest follow -- the marker at the end of an incomplete list says
    where from, as a link for a reader with no script and as the next
    fetch for one with it.
    """
    if show_recipients is None:
        show_recipients = shows_recipient(services.config, folder)
    # One more than the batch: a full batch answers "is there more?"
    # only by asking again, and an empty answer is a round trip spent
    # saying nothing.
    summaries: Sequence[FolderMessageSummary]
    if query:
        summaries = _search(services, folder, query)
        more = False
    else:
        batch = _folder_rows(services, folder, offset=offset)
        more = len(batch) > ROW_BATCH
        summaries = batch[:ROW_BATCH]
    # The server's own zone, which is the one `format_received` renders
    # a row in: one `datetime.now(UTC)` here put every row's date back
    # into UTC, which is the drift `pony.mail.views.format_received`
    # describes.
    now = datetime.now().astimezone()
    rendered = [
        _row(
            summary,
            now=now,
            is_open=summary.message_ref.id == open_id,
            show_recipients=show_recipients,
            query=query,
        )
        for summary in summaries
    ]
    if not rendered:
        return element(
            "p",
            "Nothing found." if query else "Nothing here.",
            class_="empty",
            id="rows",
        )
    return element(
        "ol",
        join(rendered),
        _more(folder, offset + len(rendered), query=query) if more else None,
        id="rows",
        class_="rows",
        data_account=folder.account_name,
        data_folder=folder.folder_name,
        start=str(offset + 1),
    )


def _more(folder: FolderRef, offset: int, *, query: str) -> Raw:
    """Where the rest of the folder is.

    A link, so a page with no script still reaches the next batch, and
    `data-offset` so the script can ask for it without parsing one.
    """
    return element(
        "li",
        element(
            "a",
            "More",
            href=urls.mail_url(folder, query_text=query, offset=offset),
        ),
        class_="more",
        data_offset=str(offset),
    )


def _folder_rows(
    services: WebServices,
    folder: FolderRef,
    *,
    offset: int = 0,
) -> Sequence[FolderMessageSummary]:
    return services.index.list_folder_message_summaries(
        folder=folder,
        limit=ROW_BATCH + 1,
        offset=offset,
    )


def _search(
    services: WebServices,
    folder: FolderRef,
    raw_query: str,
) -> list[FolderMessageSummary]:
    """Search the open folder's account, as the reader's ``/`` does.

    The same parser the terminal uses, so ``from:ada unread`` means the
    same thing in both, and the same scoping rule: the account the open
    folder belongs to, narrowed to that folder.
    """
    from ..mail.search_parser import parse_query

    # Scoped in the query, not afterwards: `search` bounds what it
    # materialises, so filtering the result dropped the hits that were
    # past the bound -- one match in INBOX behind 500 in Archive showed
    # as "Nothing found."
    found = services.index.search(
        query=parse_query(raw_query),
        account_name=folder.account_name,
        folder_name=folder.folder_name,
    )
    return [summary_of(message) for message in found]


def _row(
    summary: FolderMessageSummary,
    *,
    now: datetime,
    is_open: bool,
    show_recipients: bool,
    query: str,
) -> Raw:
    row = message_row(summary, show_recipients=show_recipients, now=now)
    classes = ["row"]
    if row.unread:
        classes.append("unread")
    if is_open:
        classes.append("open")
    return element(
        "li",
        element(
            "a",
            element("span", row.icon, class_="mark"),
            element("span", row.date, class_="date"),
            element("span", row.correspondent, class_="who"),
            element("span", row.subject, class_="subject"),
            href=urls.message_url(summary.message_ref, q=query),
        ),
        class_=" ".join(classes),
        data_id=str(summary.message_ref.id),
        aria_selected="true" if is_open else "false",
    )


# ----------------------------------------------------------------------
# The reader
# ----------------------------------------------------------------------


def reader_pane(services: WebServices, message: IndexedMessage) -> Raw:
    """Headers, the invitation if there is one, the text, the attachments."""
    ref = message.message_ref
    raw = raw_bytes(services, message)
    if raw is None:
        return pane(
            name="reader",
            heading=element("span", message.subject or "(no subject)", class_="title"),
            body=element(
                "p",
                "The message file is missing — run a sync to fetch it again.",
                class_="empty",
            ),
        )
    rendered = render_message(raw)
    invitation = _invitation_of(raw)
    return pane(
        name="reader",
        heading=fragment(
            element("span", rendered.subject or "(no subject)", class_="title"),
            element(
                "span",
                _row_action("archive", "Archive"),
                _row_action("trash", "Trash"),
                _row_action("toggle_flagged", "Flag"),
                _row_action("mark_unread", "Unread"),
                element(
                    "a",
                    "HTML version",
                    href=urls.body_url(ref),
                    target="_blank",
                    rel="noopener noreferrer",
                    class_="action",
                    id="html-version",
                ),
                element(
                    "a",
                    "Download",
                    href=urls.raw_url(ref),
                    class_="action",
                    download=True,
                ),
                element(
                    "a",
                    "Reply",
                    href=urls.compose_url(
                        mode="reply",
                        account=ref.account_name,
                        folder=ref.folder_name,
                        message_id=ref.id,
                    ),
                    class_="action",
                ),
                class_="actions",
            ),
        ),
        body=fragment(
            _headers(rendered),
            _invitation_block(services, invitation),
            _attachments(
                services,
                ref,
                rendered,
                answerable=invitation is not None and invitation.can_reply,
            ),
            element("pre", _body(rendered, ref=ref), class_="text"),
        ),
    )


def _row_action(name: str, label: str) -> Raw:
    """A button for an action the keyboard also has.

    The dispatcher posts it over whatever the page says is selected,
    which on a message's own page is that message.
    """
    return element(
        "button",
        label,
        type="button",
        class_="action",
        data_action=name,
    )


def _sender_check(auth: MessageAuthentication) -> Raw:
    """The padlock beside ``From``, and what it means when opened.

    A ``<details>`` rather than a dialog, so the pages work with the
    script switched off; ``L`` opens it from the keyboard through the
    shared keymap, and the script closes it on a click anywhere inside.
    The headline lives in the summary, hidden by CSS until the panel is
    open, so the closed state is one character and the open state has a
    heading that is also the way out.
    """
    return element(
        "details",
        element(
            "summary",
            auth.icon,
            element("span", auth.headline(), class_="sender-headline"),
            title=auth.headline(),
        ),
        # With no report there is nothing to summarise: the headline
        # already says "not checked".
        element("p", auth.label()) if auth.worth_showing else None,
        element("p", auth.meaning()),
        element("p", auth.detail(), class_="sender-detail")
        if auth.worth_showing
        else None,
        class_=f"sender-check sender-{auth.verdict.value}",
    )


def _headers(rendered: RenderedMessage) -> Raw:
    # The verdict rides on `From`, because it is a statement about
    # `From`: whether the domain the message claims actually vouched for
    # it.  The receiving server decided that; Pony only read its answer.
    fields = (
        ("From", fragment(rendered.from_, _sender_check(rendered.authentication))),
        ("To", rendered.to),
        ("Cc", rendered.cc),
        ("Date", rendered.date),
    )
    return element(
        "dl",
        join(
            fragment(
                element("dt", label),
                element("dd", value),
            )
            for label, value in fields
            if value
        ),
        class_="headers",
    )


# The HTML an emphasis sentinel opens and closes.  The renderer decides
# *where* emphasis begins; this is the only part that is markup.
_FORMAT_TAGS = {
    "B1": "<strong>",
    "B0": "</strong>",
    "I1": "<em>",
    "I0": "</em>",
    "U1": "<u>",
    "U0": "</u>",
    "S1": "<s>",
    "S0": "</s>",
}


def _body(rendered: RenderedMessage, *, ref: MessageRef) -> Raw:
    """The message body, escaped, with its links and emphasis.

    Read from the renderer's styled body, the same projection the
    reader's panel renders: the links are the ones it found, the
    emphasis is the one it marked, and the anchor text an HTML message
    chose survives.  Finding links again with a regex of our own gave
    the browser a different set from the terminal's.

    An address goes to Pony's own composer rather than out as a
    ``mailto:``, which would hand it to whatever the operating system
    has registered -- not the mail program the message is being read
    in.  The terminal routes the same link to its own composer; this is
    a link to ``/compose``, so it works with the script off, and it
    carries the message's own folder so the draft starts on the account
    the mail arrived at.
    """
    styled = rendered.styled_body or rendered.body
    pieces: list[str | Raw] = []
    for segment in parse_styled_body(styled, rendered.links):
        if segment.kind == "text":
            pieces.append(segment.text)
        elif segment.kind == "format":
            pieces.append(Raw(_FORMAT_TAGS.get(segment.format_id, "")))
        else:
            web = segment.link_kind == "web"
            pieces.append(
                element(
                    "a",
                    segment.target,
                    href=segment.target
                    if web
                    else urls.compose_url(
                        mode="new",
                        account=ref.account_name,
                        folder=ref.folder_name,
                        message_id=ref.id,
                        to=segment.target,
                    ),
                    rel="noopener noreferrer nofollow",
                    target="_blank" if web else None,
                ),
            )
    return fragment(*pieces)


# Attachments a calendar can read.  `pony.invitation` owns the question
# -- it is the module that decides whether a message carries an
# invitation at all -- so its answer is taken rather than written out
# again here, and a spelling it recognises is therefore always offered
# as an invitation to answer rather than only as a file to import.
_CALENDAR_TYPES = CALENDAR_CONTENT_TYPES


def _is_calendar_file(attachment: AttachmentInfo) -> bool:
    return attachment.content_type.split(";")[
        0
    ].strip().lower() in _CALENDAR_TYPES or attachment.filename.lower().endswith(".ics")


def _attachments(
    services: WebServices,
    ref: MessageRef,
    rendered: RenderedMessage,
    *,
    answerable: bool = False,
) -> Raw | None:
    """The attachments, with an Import control where one helps.

    Numbered as the reader numbers them, so the number beside a file
    here is the one ``S`` takes in either front end, and the download
    names match because both ask :mod:`pony.mail.message_export` for them.
    Parts ``0`` and ``T`` -- the message as it arrived and its body as
    text -- are served and can be asked for by name; they are not
    listed, because every message has them.

    *answerable* says the page is already showing this message's
    invitation with Accept, Decline and Tentative on it.  A
    ``text/calendar`` part counts as an attachment, so the same bytes
    were being offered twice -- once as an invitation to answer and
    once as a file to import -- and importing files the event without
    answering the organizer, which is the half that matters.  The
    block's own "File only" does the filing.  An ordinary shared `.ics`
    with nobody to answer keeps its Import button.
    """
    if not rendered.attachments:
        return None
    targets = () if answerable else _calendar_targets(services)
    by_index = {attachment.index: attachment for attachment in rendered.attachments}
    return element(
        "ul",
        join(
            element(
                "li",
                element("span", f"{part.key}", class_="part-key"),
                element(
                    "a",
                    part.filename,
                    href=urls.part_url(ref, part.key),
                    download=True,
                ),
                element(
                    "span",
                    f"{part.content_type}, {fmt_size(part.size_bytes)}",
                    class_="about",
                ),
                _import_control(by_index[part.key], targets)
                if isinstance(part.key, int)
                and part.key in by_index
                and _is_calendar_file(by_index[part.key])
                else None,
            )
            for part in listed_parts(rendered)
        ),
        class_="attachments",
    )


def _calendar_targets(services: WebServices) -> tuple[CalendarRef, ...]:
    """The calendars an attachment could be filed into."""
    from .calendar import calendar_targets

    return calendar_targets(services)


def _import_control(
    attachment: AttachmentInfo,
    targets: Sequence[CalendarRef],
) -> Raw | None:
    """Put a calendar file into the calendar, rather than onto the disk.

    Downloading an `.ics` and then pasting it back is the long way round
    for the thing a calendar file is for.  An invitation keeps its own
    block with Accept and Decline -- this is for everything else a
    calendar file can be: a shared agenda, an export, a single event
    someone sent to be filed.
    """
    if not targets:
        return None
    chooser_id = f"import-calendar-{attachment.index}"
    return element(
        "span",
        calendar_chooser(
            targets,
            chooser_id=chooser_id,
            label="Calendar to import into",
        ),
        element(
            "button",
            "Import",
            type="button",
            class_="action import-ics",
            data_action="import_ics",
            data_attachment=str(attachment.index),
            data_calendar_from=chooser_id,
        ),
        class_="import-ics-control",
    )


def calendar_chooser(
    targets: Sequence[CalendarRef],
    *,
    chooser_id: str,
    label: str,
) -> Raw:
    """The ``<select>`` that names which calendar something goes into.

    Built once rather than at each of the two places that offer the
    choice -- importing a `.ics` attachment and answering an invitation
    -- so a calendar is spelled one way whichever of them sent it.
    """
    return element(
        "select",
        join(
            element("option", calendar_value(ref), value=calendar_value(ref))
            for ref in targets
        ),
        name="calendar",
        id=chooser_id,
        aria_label=label,
    )


def _invitation_of(raw: bytes) -> Invitation | None:
    from ..invitation import extract_invitation

    return extract_invitation(raw)


def _invitation_block(
    services: WebServices,
    invitation: Invitation | None,
) -> Raw | None:
    """The invitation, and the three answers, when there is a calendar."""
    if invitation is None:
        return None
    lines = join(element("p", line) for line in invitation.describe())
    calendar = services.calendar
    if calendar is None:
        return element(
            "section",
            lines,
            element(
                "p",
                "No calendar configured, so this cannot be filed.",
                class_="empty",
            ),
            class_="invitation",
        )
    targets = _calendar_targets(services)
    if not targets:
        return element(
            "section",
            lines,
            element("p", "No calendars yet — sync the calendar first.", class_="empty"),
            class_="invitation",
        )
    choices = (
        (("accept", "Accept"), ("decline", "Decline"), ("tentative", "Tentative"))
        if invitation.can_reply
        else ()
    )
    return element(
        "section",
        lines,
        element(
            "div",
            calendar_chooser(
                targets,
                chooser_id="invitation-calendar",
                label="Calendar to file this in",
            ),
            join(
                element(
                    "button",
                    label,
                    type="button",
                    class_="invitation-answer",
                    data_answer=answer,
                )
                for answer, label in choices
            ),
            element(
                "button",
                "File only",
                type="button",
                class_="invitation-answer",
                data_answer="file",
            ),
            class_="invitation-answers",
        ),
        class_="invitation",
    )


def raw_bytes(services: WebServices, message: IndexedMessage) -> bytes | None:
    """The stored bytes of *message*, or None when the file is gone."""
    mirror = services.mirrors.get(message.message_ref.account_name)
    if mirror is None:
        return None
    folder = FolderRef(
        account_name=message.message_ref.account_name,
        folder_name=message.message_ref.folder_name,
    )
    try:
        return mirror.get_message_bytes(
            folder=folder,
            storage_key=message.storage_key,
        )
    except Exception:  # noqa: BLE001 — a missing file is a 410, not a crash
        return None


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------


def _archive(context: ActionContext) -> Outcome:
    return mailbox_actions.archive_messages(
        context.services.mailboxes,
        context.messages,
        context.folder,
    )


def _trash(context: ActionContext) -> Outcome:
    return mailbox_actions.trash_messages(
        context.services.mailboxes,
        context.messages,
        now=context.now,
    )


def _toggle_flagged(context: ActionContext) -> Outcome:
    return mailbox_actions.toggle_flag(
        context.services.mailboxes,
        context.messages,
        MessageFlag.FLAGGED,
    )


def _mark_unread(context: ActionContext) -> Outcome:
    return mailbox_actions.set_flag(
        context.services.mailboxes,
        context.messages,
        MessageFlag.SEEN,
        present=False,
    )


def _mark_read(context: ActionContext) -> Outcome:
    return mailbox_actions.set_flag(
        context.services.mailboxes,
        context.messages,
        MessageFlag.SEEN,
        present=True,
    )


def _mark_all_read(context: ActionContext) -> Outcome:
    return mailbox_actions.mark_folder_read(
        context.services.mailboxes,
        context.folder,
    )


def _chosen_target(context: ActionContext) -> FolderRef | Outcome:
    """The folder the prompt named, or why it named none.

    The terminal moves and copies through a folder picker, so only a
    folder that exists can be chosen.  The browser's prompt is free
    text with a ``datalist`` beside it as a hint, and nothing checked
    the answer: ``target=acct/../../../../var/tmp/PWNED`` reported
    "Moved to ..." and left the row naming a junk folder the next sync
    would ``IMAP CREATE``.  Nothing escaped -- Maildir encodes ``/`` as
    ``.`` -- but containment came from the storage layer rather than
    from here, and a typo was just as destructive as an attack.

    So the answer is required to be one of the values the page itself
    offered, which is the same list the datalist is built from.  A
    folder that does not exist yet is created with ``new_folder``, as
    it is in the terminal.
    """
    value = context.first("target")
    target = parse_target(value)
    if target is None:
        return Outcome.refused(warn("Name the folder as account/folder."))
    if value not in folder_options(context.services):
        return Outcome.refused(
            warn(
                f"No folder {folder_value(target)}."
                " Choose one from the list, or create it first."
            )
        )
    return target


def _move(context: ActionContext) -> Outcome:
    target = _chosen_target(context)
    if isinstance(target, Outcome):
        return target
    return mailbox_actions.move_messages(
        context.services.mailboxes,
        context.messages,
        context.folder,
        target,
    )


def _copy(context: ActionContext) -> Outcome:
    target = _chosen_target(context)
    if isinstance(target, Outcome):
        return target
    return mailbox_actions.copy_messages(
        context.services.mailboxes,
        context.messages,
        context.folder,
        target,
    )


def _new_folder(context: ActionContext) -> Outcome:
    name = context.first("name").strip()
    if not name:
        return Outcome.refused(warn("Name the folder to create."))
    return mailbox_actions.create_folder(
        context.services.mailboxes,
        context.folder.account_name,
        name,
    )


def _harvest_contacts(context: ActionContext) -> Outcome:
    from ..mail.contact_actions import harvest_folder

    folder = context.folder
    return harvest_folder(
        context.services.contacts,
        context.services.index.list_folder_messages(folder=folder),
        folder.folder_name,
    )


_PARTSTATS = {
    "accept": "ACCEPTED",
    "decline": "DECLINED",
    "tentative": "TENTATIVE",
    "file": None,
}


def _invitation(context: ActionContext) -> Outcome:
    """File the open message's invitation and answer the organizer."""
    from ..invitation import extract_invitation
    from ..invitation_actions import (
        InvitationChoice,
        replying_account,
        respond_to_invitation,
    )

    services = context.services
    calendar = services.calendar
    if calendar is None:
        return Outcome.refused(
            warn("No calendar configured — add a [calendar] table to config.toml."),
        )
    messages = context.messages
    if not messages:
        return Outcome.refused(warn("Open the message first."))
    raw = raw_bytes(services, messages[0])
    invitation = None if raw is None else extract_invitation(raw)
    if invitation is None:
        return Outcome.refused(info("This message carries no invitation."))
    answer = context.first("answer", "file")
    if answer not in _PARTSTATS:
        return Outcome.refused(failure(f"{answer!r} is not an answer."))
    chosen = parse_calendar(context.first("calendar"))
    if chosen is None:
        return Outcome.refused(warn("Choose a calendar to file this in."))
    found = replying_account(
        services.config,
        arrived_on=messages[0].message_ref.account_name,
    )
    return respond_to_invitation(
        invitation,
        InvitationChoice(
            calendar=chosen,
            partstat=_PARTSTATS[answer],
        ),
        calendar=calendar,
        # A reply with nothing to send from is reported by the action,
        # which has the whole story to tell about it.
        account=None if isinstance(found, Notice) else found,
        config=services.config,
        credentials=services.credentials,
        now=context.now,
    )


MAIL_ACTIONS: dict[str, Action] = {
    "archive": _archive,
    "trash": _trash,
    "toggle_flagged": _toggle_flagged,
    "mark_unread": _mark_unread,
    "mark_read": _mark_read,
    "mark_all_read": _mark_all_read,
    "move": _move,
    "copy": _copy,
    "new_folder": _new_folder,
    "harvest_contacts": _harvest_contacts,
    "respond_invitation": _invitation,
}


__all__ = [
    "MAIL_ACTIONS",
    "calendar_chooser",
    "folder_options",
    "folder_pane",
    "list_pane",
    "mail_page",
    "panes",
    "raw_bytes",
    "reader_pane",
    "rows",
]
