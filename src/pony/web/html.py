"""Building HTML without a template engine.

The web front end renders a handful of small, fixed shapes — a folder
tree, a list of rows, a header block — and a template engine would be a
third runtime dependency plus a directory of files that cannot be type
checked.  These functions build the same markup in Python, where mypy
checks the call sites and the escaping is not something a template
author can forget.

Everything that reaches :func:`element` as text is escaped.  Markup that
is deliberately raw says so by passing :class:`Raw`, which is the only
way to get an unescaped string into a document and therefore the only
place to look when auditing for injection.  A message's own HTML never
comes through here at all: it is served as a top-level page of its own
at ``/message/body``, which the reader opens in a separate tab with
``noopener``, under the policy :data:`pony.web.app.MESSAGE_HTML_CSP`
describes.  There is no frame anywhere in this front end, because mail
is hostile input and the application's page is not the place for it.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# Tags that cannot have children and take no closing tag.
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "link", "meta"})


class Raw(str):
    """Markup that is already HTML and must not be escaped again.

    A deliberate, visible opt-out: ``Raw`` in a diff is the signal to
    check where the string came from.
    """

    __slots__ = ()


Child = "str | Raw | None"


def element(
    tag: str,
    *children: str | Raw | None,
    **attributes: str | bool | None,
) -> Raw:
    """One element, with its attributes and children escaped.

    An attribute whose value is ``None`` or ``False`` is dropped, so an
    optional attribute needs no branch at the call site; ``True`` renders
    the bare name (``hidden``).  A trailing underscore is stripped from
    attribute names so Python keywords are usable (``class_``), and the
    remaining underscores become dashes (``aria_label`` →
    ``aria-label``).
    """
    rendered_attributes = "".join(
        _attribute(name, value) for name, value in attributes.items()
    )
    if tag in _VOID_TAGS:
        return Raw(f"<{tag}{rendered_attributes}>")
    body = "".join(_text(child) for child in children if child is not None)
    return Raw(f"<{tag}{rendered_attributes}>{body}</{tag}>")


def fragment(*children: str | Raw | None) -> Raw:
    """Several nodes with no element around them."""
    return Raw("".join(_text(child) for child in children if child is not None))


def join(children: Iterable[str | Raw | None]) -> Raw:
    """:func:`fragment` over an iterable."""
    return fragment(*children)


def document(
    *,
    title: str,
    body: Raw,
    stylesheets: tuple[str, ...] = (),
    scripts: tuple[str, ...] = (),
    data: Mapping[str, str] | None = None,
    head: Raw | None = None,
) -> Raw:
    """A complete page.

    *data* becomes ``data-*`` attributes on ``<body>``, which is how the
    page tells its script what it is looking at — the account and folder
    an action should apply to — without the script having to parse the
    URL.

    *head* is whatever else belongs in the head and is the chrome's to
    decide: the icon the tab is recognised by, the colour a phone
    paints its bar.  It is built where the rest of the chrome is, in
    :mod:`pony.web.layout`, rather than here.
    """
    head_elements = fragment(
        element("meta", charset="utf-8"),
        element(
            "meta",
            name="viewport",
            # The reader has to be usable on a phone, which means the
            # layout reflows rather than being zoomed out of.
            content="width=device-width, initial-scale=1",
        ),
        element("title", title),
        join(element("link", rel="stylesheet", href=href) for href in stylesheets),
        join(element("script", src=src, defer=True) for src in scripts),
        head,
    )

    body_attributes = {f"data_{key}": value for key, value in (data or {}).items()}
    return Raw(
        "<!doctype html>"
        + element(
            "html",
            element("head", head_elements),
            element("body", body, **body_attributes),
            lang="en",
        ),
    )


def _text(child: str | Raw) -> str:
    """One child node, escaped unless it is already markup.

    Element content needs only ``&``, ``<`` and ``>`` escaped; quoting
    as well would turn every apostrophe in a subject line into
    ``&#x27;``.  Attribute values go through :func:`_attribute`, which
    does quote.
    """
    if isinstance(child, Raw):
        return child
    return escape(child, quote=False)


def _attribute(name: str, value: str | bool | None) -> str:
    if value is None or value is False:
        return ""
    attribute = name.rstrip("_").replace("_", "-")
    if value is True:
        return f" {attribute}"
    return f' {attribute}="{escape(value, quote=True)}"'


__all__ = ["Child", "Raw", "document", "element", "fragment", "join"]
