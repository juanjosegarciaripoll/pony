"""Islands of HTML inside a ``text/plain`` body, read as HTML in place.

People paste HTML into plain text.  An institutional signature block is
the usual way it arrives: a layout table of logos and links, dropped into
the bottom of a message that is otherwise prose and quoted replies.  The
sender's generator writes real markup; the mail is still plain text.

Reading such a message as HTML throughout destroys it -- the stripper
normalises whitespace the way a browser does, so every newline in the
prose and every ``>`` quote marker collapses into one paragraph.  Reading
it as text throughout shows the reader raw tags and kilobytes of base64.
Neither is right, because the message is both: here it is 95 lines of
text followed by 30 of markup.

So the decision is made per island rather than per message.  Only the
markup is handed to the stripper; every byte outside an island is passed
through untouched, which is what keeps the line structure, the quoting
and the sender's own alignment.  The point is not that the test below is
infallible -- it is that when it is wrong it can only spoil the few
characters it was judging, never the message around them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: Element names recognised as markup.  A name list rather than "angle
#: brackets with something between them", because plain mail is full of
#: angle-bracketed addresses -- `<comunicacion@iqf.csic.es>` is not a
#: tag, and a header block quoting one must survive as text.
_NAMES = (
    "a|abbr|address|area|b|base|big|blockquote|body|br|caption|center|cite|code|col|"
    "colgroup|dd|del|div|dl|dt|em|embed|fieldset|font|form|h1|h2|h3|h4|h5|h6|head|"
    "hr|html|i|iframe|img|input|ins|label|legend|li|link|map|meta|o:p|object|ol|"
    "optgroup|option|p|param|pre|q|s|samp|script|select|small|source|span|strike|"
    "strong|style|sub|sup|table|tbody|td|textarea|tfoot|th|thead|title|tr|tt|u|ul|"
    "v:[a-z]+|var|w:[a-z]+|wbr"
)

#: One piece of markup: a comment (Word's conditional blocks are
#: comments), a downlevel-revealed terminator such as `<![endif]-->`, a
#: doctype, or an element.
_TOKEN = re.compile(
    rf"<!--.*?-->|<!\[[^>]*>|<![a-z][^>]*>|</?(?:{_NAMES})\b[^>]*>",
    re.IGNORECASE | re.DOTALL,
)

#: Elements that never close, so an unclosed one is not evidence of
#: anything.
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

_NAME_OF = re.compile(r"</?\s*([a-z0-9:]+)", re.IGNORECASE)
_HAS_ATTRIBUTE = re.compile(r"<[^>]*\s[a-z-]+\s*=", re.IGNORECASE)

#: A blank line ends an island.  Markup and the prose around it are
#: separated by one in every message this was written for, and merging
#: across one is what would let a signature's island swallow the
#: paragraph above it.
_BLANK_LINE = re.compile(r"\n[ \t]*\n")

#: A link sentinel, whose index is relative to the island it came from
#: and has to be renumbered against the whole body.
_SENTINEL = re.compile(r"\x00LINK:(\d+)")

#: What `message_renderer._strip_html` is: markup in, text plus the
#: links it found out.
type Stripper = Callable[[str], tuple[str, list[tuple[str, str]]]]


def strip_html_islands(
    text: str,
    strip: Stripper,
    links: list[tuple[str, str]],
) -> str:
    """*text* with its markup islands stripped and the rest left alone.

    *strip* reads one island, *links* collects what it finds there, in
    the order the islands appear, and the sentinels it left behind are
    renumbered to match.
    """
    pieces: list[str] = []
    cursor = 0
    for start, end in _islands(text):
        pieces.append(text[cursor:start])
        stripped, found = strip(text[start:end])
        pieces.append(_renumbered(stripped, offset=len(links)))
        links.extend(found)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _renumbered(stripped: str, *, offset: int) -> str:
    """One island's link sentinels, numbered against the whole body.

    ``_strip_html`` numbers what it finds from zero, and it is called
    once per island, so without this the second island's first link
    would claim the first island's target.
    """
    if offset == 0:
        return stripped
    return _SENTINEL.sub(
        lambda match: f"\x00LINK:{int(match.group(1)) + offset}",
        stripped,
    )


def looks_like_markup(text: str) -> bool:
    """Whether *text* holds any island worth reading as HTML."""
    return bool(_islands(text))


def _islands(text: str) -> list[tuple[int, int]]:
    """The spans of *text* that are markup.

    Consecutive tokens join into one island unless a blank line falls
    between them, and the result is kept only if it carries evidence of
    being markup rather than prose about it.  No count or density test:
    a sentence mentioning two tag names has two tokens as surely as a
    signature does, and the only thing that separates them is whether
    the tags behave like tags (see :func:`_is_markup`).
    """
    runs: list[tuple[int, int, list[str]]] = []
    for match in _TOKEN.finditer(text):
        token = match.group()
        if runs and not _BLANK_LINE.search(text[runs[-1][1] : match.start()]):
            start, _end, tokens = runs[-1]
            tokens.append(token)
            runs[-1] = (start, match.end(), tokens)
        else:
            runs.append((match.start(), match.end(), [token]))
    # The tokens travel with the run rather than being found again:
    # judging an island used to re-scan it, which doubled the cost on
    # exactly the markup-heavy bodies that are slowest to scan at all.
    return [(start, end) for start, end, tokens in runs if _is_markup(tokens)]


def _is_markup(tokens: list[str]) -> bool:
    """Whether an island is markup, rather than prose naming tags.

    The two are mirror images -- a signature is markup containing text,
    a sentence about HTML is text containing markup -- so counting tags
    cannot tell them apart.  ``Use the <table> element, not <div>
    soup.`` has as many tags as ``<span>Madrid</span>``.

    What separates them is whether the tags are *used*:

    - a closing tag that matches an opener, because a generator closes
      what it opens and a person naming a tag does not;
    - a stray closing tag, which prose has no reason to contain;
    - an attribute, because prose says ``<table>`` and markup says
      ``<table border="0" cellpadding="0">``;
    - a comment, which is not something prose contains by accident.
    """
    stack: list[str] = []
    attributed = False
    for token in tokens:
        if token.startswith("<!"):
            return True
        if not attributed and _HAS_ATTRIBUTE.match(token):
            attributed = True
        name_match = _NAME_OF.match(token)
        if name_match is None:  # pragma: no cover - _TOKEN implies a name
            continue
        name = name_match.group(1).lower()
        if name in _VOID:
            continue
        if token.startswith("</"):
            if name in stack:
                while stack and stack.pop() != name:
                    pass
                return True  # a tag that closes what it opened
            return True  # a stray closer, which prose has no reason to hold
        stack.append(name)
    return attributed


def island_spans(text: str) -> Sequence[tuple[int, int]]:
    """The islands of *text*, for diagnostics and tests."""
    return _islands(text)
