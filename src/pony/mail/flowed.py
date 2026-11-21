"""RFC 3676 ``format=flowed``: undo the soft line breaks a sender added.

A sender that declares ``text/plain; format=flowed`` has wrapped its own
paragraphs to some width and marked every break it made with a trailing
space.  A line ending in a space continues into the next one; a line that
does not is where the paragraph really ends.  A reader that ignores the
parameter shows a paragraph broken at the sender's width rather than its
own, mid-sentence::

    Hay un error, no habrá actividad en el Observatorio a las 12:00 (por
    estar la sala ocupada por una oposición), aunque sí la habrá por la
    noche.

Thunderbird, Apple Mail and Roundcube all send this, so it is most of the
plain-text mail a person receives.

Nothing here guesses: the parameter is declared or it is not, and this
runs only when it is.
"""

from __future__ import annotations

#: The signature separator (RFC 3676 §4.3).  It ends in a space, which
#: would otherwise make it a flowed line, and it is deliberately fixed --
#: joining it to the signature below would hide where the message ends.
_SIGNATURE_SEPARATOR = "-- "


def unflow(text: str, *, delsp: bool) -> str:
    """Rejoin *text*'s flowed lines into the paragraphs they came from.

    *delsp* is the ``DelSp`` parameter.  With it the trailing space is
    the flow marker alone and is dropped when joining; without it the
    space is also content and is kept.  Getting this backwards either
    runs words together or doubles the spaces between them.

    Quote depth is part of the line, not decoration: a paragraph is
    joined only with lines at its own depth, so a reply's text never
    merges into the text it quotes.  The depth is written back out as
    ``>`` marks, because that is what the reader recognises as a quote.
    """
    paragraphs: list[tuple[int, list[str]]] = []
    depth_open: int | None = None
    pieces: list[str] = []

    for raw_line in text.split("\n"):
        depth, content = _split_line(raw_line.removesuffix("\r"))
        if depth_open is not None and depth != depth_open:
            # The quote depth changed, so whatever was still open ends
            # here however it was marked -- a flowed line cannot flow
            # into a different quoting level.
            paragraphs.append((depth_open, pieces))
            depth_open, pieces = None, []

        flowed = content.endswith(" ") and content != _SIGNATURE_SEPARATOR
        if flowed and delsp:
            content = content[:-1]

        if depth_open is None:
            depth_open, pieces = depth, [content]
        else:
            pieces.append(content)

        if not flowed:
            paragraphs.append((depth_open, pieces))
            depth_open, pieces = None, []

    if depth_open is not None:
        paragraphs.append((depth_open, pieces))

    return "\n".join(_render(depth, "".join(parts)) for depth, parts in paragraphs)


def _split_line(line: str) -> tuple[int, str]:
    """A line's quote depth and its content, unstuffed.

    Quote marks are the run of ``>`` the line opens with (§4.5), counted
    before anything else.  One leading space after them is then removed:
    a sender *stuffs* a space in front of any line that would otherwise
    begin with a space, a ``>`` or ``From ``, so that its own quoting and
    flowing stay unambiguous (§4.4).  Leaving it in place indents the
    line by one column for ever; removing more than one eats the
    sender's own alignment, which is how a right-aligned header block
    loses a column.
    """
    depth = 0
    while depth < len(line) and line[depth] == ">":
        depth += 1
    content = line[depth:]
    return depth, content.removeprefix(" ")


def _render(depth: int, text: str) -> str:
    """One paragraph, with its quote marks put back."""
    if depth == 0:
        return text
    marks = ">" * depth
    return f"{marks} {text}" if text else marks
