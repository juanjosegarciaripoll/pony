"""The HTML builder that stands in for a template engine.

Everything the web front end renders goes through here, so this is where
the escaping is pinned: a subject line, a folder name and an account name
are all attacker-influenced, and ``Raw`` is the only way to get an
unescaped string into a page.
"""

from __future__ import annotations

from pony.web.html import Raw, document, element, fragment, join


def test_text_children_are_escaped() -> None:
    assert element("p", "<script>alert(1)</script>") == (
        "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
    )


def test_text_children_keep_their_punctuation() -> None:
    """Quoting element content would turn every apostrophe into a code."""
    assert element("p", "Run 'pony sync' — it's \"fine\"") == (
        "<p>Run 'pony sync' — it's \"fine\"</p>"
    )


def test_attribute_values_are_quoted_and_escaped() -> None:
    assert element("a", "x", href='/f/"acct"/<INBOX>') == (
        '<a href="/f/&quot;acct&quot;/&lt;INBOX&gt;">x</a>'
    )


def test_attribute_names_become_html_spellings() -> None:
    """``class_`` and ``aria_label`` are what a Python caller can write."""
    assert element("div", "", class_="row", aria_label="Row", data_id="7") == (
        '<div class="row" aria-label="Row" data-id="7"></div>'
    )


def test_an_absent_attribute_needs_no_branch_at_the_call_site() -> None:
    assert element("span", "x", title=None, hidden=False) == "<span>x</span>"
    assert element("span", "x", hidden=True) == "<span hidden>x</span>"


def test_raw_is_the_only_way_past_the_escaper() -> None:
    inner = element("em", "safe")
    assert isinstance(inner, Raw)
    assert element("p", inner) == "<p><em>safe</em></p>"
    assert element("p", Raw("<b>trusted</b>")) == "<p><b>trusted</b></p>"


def test_absent_children_are_dropped() -> None:
    assert element("p", "a", None, "b") == "<p>ab</p>"
    assert fragment(None) == ""


def test_void_tags_take_no_closing_tag() -> None:
    assert element("meta", charset="utf-8") == '<meta charset="utf-8">'
    assert element("br") == "<br>"


def test_fragment_and_join_put_nodes_side_by_side() -> None:
    assert fragment(element("li", "a"), element("li", "b")) == "<li>a</li><li>b</li>"
    assert join(element("li", str(n)) for n in (1, 2)) == "<li>1</li><li>2</li>"


def test_a_document_carries_what_a_phone_needs() -> None:
    page = document(
        title="Pony",
        body=element("main", "hello"),
        stylesheets=("/static/pony.css",),
        scripts=("/static/pony.js",),
        data={"account": "acct", "folder": "INBOX"},
    )

    assert page.startswith("<!doctype html>")
    # Without this the layout is a zoomed-out desktop page on a phone.
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in page
    assert '<link rel="stylesheet" href="/static/pony.css">' in page
    assert '<script src="/static/pony.js" defer></script>' in page
    # The page tells its script what it is looking at, so the script
    # never has to parse the URL.
    assert '<body data-account="acct" data-folder="INBOX">' in page
    assert "<title>Pony</title>" in page


def test_a_document_title_is_escaped_like_anything_else() -> None:
    page = document(title="<b>subject</b>", body=element("main", ""))

    assert "<title>&lt;b&gt;subject&lt;/b&gt;</title>" in page
